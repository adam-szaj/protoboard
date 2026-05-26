# SPDX-License-Identifier: MIT
"""Commands for placing, moving and removing components.

All commands operate on a ComponentState protocol so that both
Layout.placements and session.instances stay in sync through undo/redo,
without creating a hard dependency on the GUI layer.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import structlog

from protoboard.domain.board.board import Board
from protoboard.domain.commands.base import Command
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import (
    ComponentPlacement,
    component_bbox,
    pin_world_position,
    validate_pins_on_pads,
)
from protoboard.domain.layout.wire import WireSegment
from protoboard.geometry import Point

_log = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from protoboard.domain.schematic.net import Net
    from protoboard.domain.schematic.schematic import Schematic


class ComponentState(Protocol):
    """Minimal state needed by all board/component commands."""

    board: Board
    layout: Layout
    instances: dict[uuid.UUID, ComponentInstance]
    components: dict[str, ComponentDefinition]


class SchematicState(ComponentState, Protocol):
    """Extended state that also exposes the schematic netlist."""

    schematic: Schematic


@dataclass
class PlaceComponentCommand(Command[ComponentState]):
    component_id: uuid.UUID
    placement: ComponentPlacement
    instance: ComponentInstance

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Place {self.instance.refdes}"

    def apply(self, state: ComponentState) -> None:
        state.layout.placements[self.component_id] = self.placement
        state.instances[self.component_id] = self.instance
        _warn_pins_off_pads(state, self.component_id, self.placement)

    def revert(self, state: ComponentState) -> None:
        del state.layout.placements[self.component_id]
        state.instances.pop(self.component_id, None)


@dataclass
class MoveComponentCommand(Command[ComponentState]):
    component_id: uuid.UUID
    new_placement: ComponentPlacement
    old_placement: ComponentPlacement
    # When True, skip the _move_blocked() check (caller already verified).
    skip_conflict_check: bool = field(default=False)
    # Wire IDs already translated by live drag — skip in _update_wires_for_move
    # to prevent double-translation when old_pin_pos == new_pin_pos of another component.
    exclude_wire_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)
    # Wires before the move — saved for undo.
    _saved_wires: list[WireSegment] = field(
        default_factory=list, init=False, repr=False
    )
    _applied: bool = field(default=False, init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Move component {self.component_id}"

    def apply(self, state: ComponentState) -> None:
        # Locked components never move — silently treat as no-op.
        if state.layout.placements.get(self.component_id, self.old_placement).locked:
            object.__setattr__(self, "_applied", False)
            return
        if not self.skip_conflict_check:
            blocked = _move_blocked(
                state, self.component_id, self.old_placement, self.new_placement
            )
            if blocked:
                # Conflict — silently keep old position; mark as no-op for undo.
                object.__setattr__(self, "_applied", False)
                state.layout.placements[self.component_id] = self.old_placement
                return
        object.__setattr__(self, "_applied", True)
        object.__setattr__(self, "_saved_wires", list(state.layout.wires))
        state.layout.placements[self.component_id] = self.new_placement
        _update_wires_for_move(
            state, self.component_id, self.old_placement, self.new_placement,
            self.exclude_wire_ids,
        )
        _warn_pins_off_pads(state, self.component_id, self.new_placement)

    def revert(self, state: ComponentState) -> None:
        if not self._applied:
            return
        state.layout.placements[self.component_id] = self.old_placement
        state.layout.wires[:] = self._saved_wires


@dataclass
class RemoveComponentCommand(Command[SchematicState]):
    """Remove a component from the layout and clean up its pins from all nets.

    Any net that becomes empty after pin removal is also deleted.
    All mutations are recorded for full undo/redo.
    """

    component_id: uuid.UUID
    _saved_placement: ComponentPlacement | None = field(
        default=None, init=False, repr=False
    )
    _saved_instance: ComponentInstance | None = field(
        default=None, init=False, repr=False
    )
    # net_id → (net_name, removed_pins) for every net touched by this remove
    _nets_before: dict[uuid.UUID, "tuple[str, set[object]]"] = field(
        default_factory=dict, init=False, repr=False
    )

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Remove component {self.component_id}"

    def apply(self, state: SchematicState) -> None:
        self._saved_placement = state.layout.placements.pop(self.component_id)
        self._saved_instance = state.instances.pop(self.component_id, None)

        nets_before: dict[uuid.UUID, tuple[str, set[object]]] = {}
        for net_id, net in list(state.schematic.nets.items()):
            removed = {p for p in net.pins if p.component_id == self.component_id}
            if not removed:
                continue
            nets_before[net_id] = (net.name, removed)
            net.pins -= removed
            if not net.pins:
                del state.schematic.nets[net_id]
        self._nets_before = nets_before

    def revert(self, state: SchematicState) -> None:
        from protoboard.domain.schematic.net import Net

        if self._saved_placement is not None:
            state.layout.placements[self.component_id] = self._saved_placement
        if self._saved_instance is not None:
            state.instances[self.component_id] = self._saved_instance
        for net_id, (net_name, removed_pins) in self._nets_before.items():
            net = state.schematic.nets.get(net_id)
            if net is not None:
                net.pins |= removed_pins  # type: ignore[arg-type]
            else:
                state.schematic.nets[net_id] = Net(
                    id=net_id, name=net_name, pins=set(removed_pins)  # type: ignore[arg-type]
                )


def _enrich_insulated_with_pad_nodes(
    wire: WireSegment, board: Board,
) -> WireSegment:
    """Insert board-pad positions into the path of an *insulated* wire.

    An insulated wire that crosses a pad geometrically between two of its
    recorded vertices has no "handle" at that pad — simplify, unweave and
    interactive edits cannot manipulate the path there.  Inserting an
    explicit pad-vertex at each crossed pad gives downstream tools the
    nodes they need.  Idempotent — pads already on the path are not
    duplicated.

    Bare wires are returned unchanged — they're produced with pad-vertices
    by their callers (RouteTool's ``insert_pad_nodes`` enrichment, A*
    step-by-step paths) and the bare-diagonal parity model relies on the
    caller-controlled vertex set.

    A new ``WireSegment`` is returned only when the path actually grows;
    otherwise the input is returned as-is.  The wire's id is preserved
    so callers comparing by identity stay consistent.
    """
    if not wire.insulated:
        return wire
    from protoboard.domain.layout.wire_utils import (
        board_pads_for_side,
        insert_pad_nodes,
    )
    # Insulated wires sit on one side; their endpoints solder to that side's
    # pads.  Pads from BOTH sides are relevant for vertex insertion because
    # the wire's geometric path may pass over a pad that physically exists
    # on either side (plated-through pads in particular).  Take the union.
    pads = board_pads_for_side(board, "top") | board_pads_for_side(board, "bottom")
    new_path = insert_pad_nodes(list(wire.path), pads)
    if len(new_path) == len(wire.path):
        return wire
    return WireSegment(
        id=wire.id,
        path=new_path,
        insulated=wire.insulated,
        side=wire.side,
        color=wire.color,
    )


@dataclass
class AddWireCommand(Command[ComponentState]):
    wire: WireSegment

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Add wire"

    def apply(self, state: ComponentState) -> None:
        # Enrich insulated wires with pad-position vertices so simplify /
        # unweave / edit tools have nodes at every pad the wire physically
        # crosses.  Idempotent — redo doesn't re-grow the path.
        self.wire = _enrich_insulated_with_pad_nodes(self.wire, state.board)
        state.layout.wires.append(self.wire)

    def revert(self, state: ComponentState) -> None:
        state.layout.wires.remove(self.wire)


@dataclass
class AddWireWithNetsCommand(Command[ComponentState]):
    """Add a wire and atomically record schematic net mutations for undo/redo.

    The resolver mutates session.schematic.nets BEFORE stack.execute() is
    called.  apply() on first invocation therefore only appends the wire.
    On redo, apply() also re-applies the net mutations recorded via
    record_net_change().  revert() always rolls back both the wire and nets.

    Typed as Command[ComponentState] so it fits the shared CommandStack, but
    requires the concrete state to also carry a `.schematic` attribute
    (satisfied at runtime by EditorSession).
    """

    wire: WireSegment
    # net_id → Net before mutation (None = net did not exist yet)
    _nets_before: dict[uuid.UUID, Net | None] = field(
        default_factory=dict, init=False, repr=False
    )
    # net_id → Net after mutation (None = net was deleted)
    _nets_after: dict[uuid.UUID, Net | None] = field(
        default_factory=dict, init=False, repr=False
    )
    _first_apply: bool = field(default=True, init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Add wire (with nets)"

    def record_net_change(
        self,
        net_id: uuid.UUID,
        before: Net | None,
        after: Net | None,
    ) -> None:
        """Called by the resolver before each schematic mutation."""
        if net_id not in self._nets_before:
            self._nets_before[net_id] = before
        self._nets_after[net_id] = after

    def apply(self, state: ComponentState) -> None:
        # Enrich insulated wires the same way AddWireCommand does — single
        # chokepoint for every wire-creation path.
        self.wire = _enrich_insulated_with_pad_nodes(self.wire, state.board)
        state.layout.wires.append(self.wire)
        if self._first_apply:
            # Net mutations already applied by resolver — nothing more to do.
            object.__setattr__(self, "_first_apply", False)
            return
        # Redo path: re-apply recorded net mutations.
        schematic = getattr(state, "schematic")  # EditorSession always has this
        for net_id, net_after in self._nets_after.items():
            if net_after is None:
                schematic.nets.pop(net_id, None)
            else:
                schematic.nets[net_id] = net_after

    def revert(self, state: ComponentState) -> None:
        state.layout.wires.remove(self.wire)
        schematic = getattr(state, "schematic")
        for net_id, net_before in self._nets_before.items():
            if net_before is None:
                schematic.nets.pop(net_id, None)
            else:
                schematic.nets[net_id] = net_before


@dataclass
class RemoveWireCommand(Command[ComponentState]):
    wire_id: uuid.UUID
    _saved_wire: WireSegment | None = field(default=None, init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Remove wire"

    def apply(self, state: ComponentState) -> None:
        for wire in state.layout.wires:
            if wire.id == self.wire_id:
                object.__setattr__(self, "_saved_wire", wire)
                state.layout.wires.remove(wire)
                return
        raise KeyError(f"Wire {self.wire_id} not found")

    def revert(self, state: ComponentState) -> None:
        if self._saved_wire is not None:
            state.layout.wires.append(self._saved_wire)


@dataclass
class ReplaceWireSegmentCommand(Command[ComponentState]):
    """Replace the path of an existing wire (for segment editing).

    If *old_path* is provided it is used directly for undo — callers that
    mutate the wire in-place before executing the command (e.g. drag-move)
    must supply the original path here so that apply() does not read the
    already-overwritten state.
    """

    wire_id: uuid.UUID
    new_path: list[Point]
    # Caller-supplied pre-mutation path; if empty, apply() reads from state.
    old_path: list[Point] = field(default_factory=list)
    _old_path: list[Point] = field(default_factory=list, init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Edit wire segment"

    def apply(self, state: ComponentState) -> None:
        for wire in state.layout.wires:
            if wire.id == self.wire_id:
                self._old_path = self.old_path if self.old_path else list(wire.path)
                # Enrich insulated wires with pad-position vertices (same
                # invariant as AddWireCommand / load_project migration).
                # Idempotent — already-enriched paths grow no further.
                # Skip if state lacks a board (lightweight test stubs).
                board = getattr(state, "board", None)
                if wire.insulated and board is not None:
                    from protoboard.domain.layout.wire_utils import (
                        board_pads_for_side, insert_pad_nodes,
                    )
                    pads = (
                        board_pads_for_side(board, "top")
                        | board_pads_for_side(board, "bottom")
                    )
                    self.new_path = insert_pad_nodes(self.new_path, pads)
                wire.path = self.new_path
                return
        raise KeyError(f"Wire {self.wire_id} not found")

    def revert(self, state: ComponentState) -> None:
        for wire in state.layout.wires:
            if wire.id == self.wire_id:
                wire.path = self._old_path
                return


@dataclass
class SetWireInsulatedCommand(Command[ComponentState]):
    """Toggle the insulated flag of a wire."""

    wire_id: uuid.UUID
    insulated: bool
    _old_insulated: bool = field(default=True, init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Set wire insulated" if self.insulated else "Set wire bare"

    def apply(self, state: ComponentState) -> None:
        for wire in state.layout.wires:
            if wire.id == self.wire_id:
                self._old_insulated = wire.insulated
                wire.insulated = self.insulated
                return
        raise KeyError(f"Wire {self.wire_id} not found")

    def revert(self, state: ComponentState) -> None:
        for wire in state.layout.wires:
            if wire.id == self.wire_id:
                wire.insulated = self._old_insulated
                return


@dataclass
class SetWireSideCommand(Command[ComponentState]):
    """Move a wire to the top or bottom layer."""

    wire_id: uuid.UUID
    side: str  # "top" or "bottom"
    _old_side: str = field(default="top", init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Move wire to {self.side}"

    def apply(self, state: ComponentState) -> None:
        for wire in state.layout.wires:
            if wire.id == self.wire_id:
                self._old_side = wire.side
                wire.side = self.side  # type: ignore[assignment]
                return
        raise KeyError(f"Wire {self.wire_id} not found")

    def revert(self, state: ComponentState) -> None:
        for wire in state.layout.wires:
            if wire.id == self.wire_id:
                wire.side = self._old_side  # type: ignore[assignment]
                return


@dataclass
class SetPinVariantCommand(Command[ComponentState]):
    """Change the pin-name variant of a component instance."""

    component_id: uuid.UUID
    new_variant: str | None
    _old_variant: str | None = field(default=None, init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Set pin variant to {self.new_variant or 'default'}"

    def apply(self, state: ComponentState) -> None:
        inst = state.instances[self.component_id]
        self._old_variant = inst.pin_variant
        import dataclasses as _dc
        state.instances[self.component_id] = _dc.replace(inst, pin_variant=self.new_variant)

    def revert(self, state: ComponentState) -> None:
        inst = state.instances[self.component_id]
        import dataclasses as _dc
        state.instances[self.component_id] = _dc.replace(inst, pin_variant=self._old_variant)


@dataclass
class FlipWireFragmentCommand(Command[ComponentState]):
    """Flip a contiguous fragment of a wire to the opposite layer.

    The fragment is identified by *start_idx* and *end_idx* (inclusive) in
    the wire's current path.  If the fragment spans the whole wire, the wire's
    ``side`` attribute is toggled and no splitting occurs.  Otherwise the
    original wire is replaced by up to three new wires:

    - prefix  : path[:start_idx+1]  on the original side  (omitted if start==0)
    - middle  : path[start_idx:end_idx+1]  on the target side
    - suffix  : path[end_idx:]  on the original side  (omitted if end==last)

    Undo restores the original single wire.
    """

    wire_id: uuid.UUID
    start_idx: int
    end_idx: int
    target_side: str  # "top" or "bottom"
    # Filled on first apply for undo:
    _original_wire: object = field(default=None, init=False, repr=False)
    _added_ids: list[uuid.UUID] = field(default_factory=list, init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Flip wire fragment to {self.target_side}"

    def apply(self, state: ComponentState) -> None:
        from protoboard.domain.layout.wire import WireSegment

        wire: WireSegment | None = next(
            (w for w in state.layout.wires if w.id == self.wire_id), None
        )
        if wire is None:
            raise KeyError(f"Wire {self.wire_id} not found")

        self._original_wire = wire
        path = wire.path
        start, end = self.start_idx, self.end_idx
        original_side = wire.side

        # Remove the original wire
        state.layout.wires = [w for w in state.layout.wires if w.id != self.wire_id]
        self._added_ids.clear()

        if start == 0 and end == len(path) - 1:
            # Whole wire — just change side, no split
            new_wire = WireSegment(
                id=self.wire_id,
                path=list(path),
                insulated=wire.insulated,
                side=self.target_side,  # type: ignore[arg-type]
                color=wire.color,
            )
            state.layout.wires.append(new_wire)
            self._added_ids.append(new_wire.id)
            return

        # Split into up to 3 segments
        segments: list[tuple[list, str]] = []
        if start > 0:
            segments.append((path[:start + 1], original_side))
        segments.append((path[start:end + 1], self.target_side))
        if end < len(path) - 1:
            segments.append((path[end:], original_side))

        for seg_path, seg_side in segments:
            if len(seg_path) < 2:
                continue
            new_wire = WireSegment(
                id=uuid.uuid4(),
                path=list(seg_path),
                insulated=wire.insulated,
                side=seg_side,  # type: ignore[arg-type]
                color=wire.color,
            )
            state.layout.wires.append(new_wire)
            self._added_ids.append(new_wire.id)

    def revert(self, state: ComponentState) -> None:
        from protoboard.domain.layout.wire import WireSegment

        # Remove added wires
        added = set(self._added_ids)
        state.layout.wires = [w for w in state.layout.wires if w.id not in added]
        # Restore original
        if self._original_wire is not None:
            state.layout.wires.append(self._original_wire)  # type: ignore[arg-type]


@dataclass
class DisconnectPinCommand(Command[SchematicState]):
    """Remove a pin from its net. Deletes the net if it becomes empty."""

    component_id: uuid.UUID
    pin_number: int
    _saved_net_id: uuid.UUID | None = field(default=None, init=False, repr=False)
    _saved_net_name: str = field(default="", init=False, repr=False)
    _saved_pins: object = field(default=None, init=False, repr=False)  # set[PinRef]|None

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Disconnect pin {self.pin_number}"

    def apply(self, state: SchematicState) -> None:
        from protoboard.domain.schematic.net import PinRef

        ref = PinRef(component_id=self.component_id, pin_number=self.pin_number)
        net = state.schematic.net_for_pin(ref)
        if net is None:
            return
        self._saved_net_id = net.id
        self._saved_net_name = net.name
        self._saved_pins = net.pins.copy()
        net.pins.discard(ref)
        if not net.pins:
            del state.schematic.nets[net.id]

    def revert(self, state: SchematicState) -> None:
        from protoboard.domain.schematic.net import Net, PinRef

        if self._saved_net_id is None or self._saved_pins is None:
            return
        ref = PinRef(component_id=self.component_id, pin_number=self.pin_number)
        net = state.schematic.nets.get(self._saved_net_id)
        if net is not None:
            net.pins.add(ref)
        else:
            # Net was deleted (became empty) — restore it.
            saved = self._saved_pins
            assert isinstance(saved, set)
            state.schematic.nets[self._saved_net_id] = Net(
                id=self._saved_net_id,
                name=self._saved_net_name,
                pins=saved,
            )


@dataclass
class LockComponentCommand(Command[ComponentState]):
    """Toggle the locked flag on a component placement."""

    component_id: uuid.UUID
    locked: bool
    _old_locked: bool = field(default=False, init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Lock component" if self.locked else "Unlock component"

    def apply(self, state: ComponentState) -> None:
        pl = state.layout.placements.get(self.component_id)
        if pl is None:
            return
        self._old_locked = pl.locked
        pl.locked = self.locked

    def revert(self, state: ComponentState) -> None:
        pl = state.layout.placements.get(self.component_id)
        if pl is None:
            return
        pl.locked = self._old_locked


# ---------------------------------------------------------------------------
# v2 — Wire / Knot / Fragment commands (operate on Layout.wires_v2 + knots)
# ---------------------------------------------------------------------------


from protoboard.domain.layout.knot import JointKnot as _JointKnot
from protoboard.domain.layout.knot import Knot as _Knot
from protoboard.domain.layout.wire import Fragment as _Fragment
from protoboard.domain.layout.wire import Wire as _Wire


@dataclass
class AddWireV2Command(Command[ComponentState]):
    """Add a v2 ``Wire`` to ``Layout.wires_v2`` along with any knots it needs.

    Knots already present in ``Layout.knots`` are reused; missing knots are
    expected to be supplied in ``extra_knots`` (typically fresh JointKnots
    created by the caller before issuing the command).  PinKnots for the
    endpoints should be looked up via ``Layout.get_or_create_pin_knot``
    before constructing the command.
    """

    wire: _Wire
    extra_knots: list[_Knot] = field(default_factory=list)

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Add wire (v2)"

    def apply(self, state: ComponentState) -> None:
        for k in self.extra_knots:
            state.layout.knots.setdefault(k.id, k)
        state.layout.add_wire_v2(self.wire)

    def revert(self, state: ComponentState) -> None:
        state.layout.remove_wire_v2(self.wire.id)
        for k in self.extra_knots:
            if isinstance(k, _JointKnot):
                state.layout.knots.pop(k.id, None)


@dataclass
class RemoveWireV2Command(Command[ComponentState]):
    """Remove a v2 ``Wire`` from ``Layout.wires_v2``; orphan JointKnots removed.

    Stores the original ``Wire`` so ``revert`` can restore it; also records
    the orphan knots that were dropped so undo restores them.
    """

    wire_id: uuid.UUID
    _saved_wire: _Wire | None = field(default=None, init=False, repr=False)
    _saved_orphan_knots: list[_Knot] = field(default_factory=list, init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Remove wire (v2)"

    def apply(self, state: ComponentState) -> None:
        wire = state.layout.wires_v2.get(self.wire_id)
        if wire is None:
            return
        orphans: list[_Knot] = []
        for ref in (wire.a, wire.b):
            knot = state.layout.knots.get(ref.id)
            if isinstance(knot, _JointKnot) and knot.wire_ids == frozenset({wire.id}):
                orphans.append(knot)
        self._saved_wire = wire
        self._saved_orphan_knots = orphans
        state.layout.remove_wire_v2(self.wire_id)

    def revert(self, state: ComponentState) -> None:
        if self._saved_wire is None:
            return
        for k in self._saved_orphan_knots:
            state.layout.knots.setdefault(k.id, k)
        state.layout.add_wire_v2(self._saved_wire)


@dataclass
class ReplaceWireFragmentsCommand(Command[ComponentState]):
    """Replace ``Wire.fragments`` in place — preserving ``Wire.id``.

    This is the explicit v2 counterpart of the path-edit operations
    (simplify, unweave, nudge, …) that today mutate WireSegment.path or
    mint fresh segments.  Identity stays stable; highlight, recording and
    undo follow.
    """

    wire_id: uuid.UUID
    new_fragments: tuple[_Fragment, ...]
    _saved_fragments: tuple[_Fragment, ...] = field(default=(), init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Replace wire fragments"

    def apply(self, state: ComponentState) -> None:
        wire = state.layout.wires_v2.get(self.wire_id)
        if wire is None:
            return
        self._saved_fragments = wire.fragments
        state.layout.replace_wire_fragments(self.wire_id, self.new_fragments)

    def revert(self, state: ComponentState) -> None:
        if not self._saved_fragments:
            return
        state.layout.replace_wire_fragments(self.wire_id, self._saved_fragments)


# ---------------------------------------------------------------------------
# Wire-update helper (used by MoveComponentCommand)
# ---------------------------------------------------------------------------

_EPS = 1e-6  # tolerance for point equality (raster units)


def _pts_equal(a: Point, b: Point) -> bool:
    return abs(a.x - b.x) < _EPS and abs(a.y - b.y) < _EPS


def _warn_pins_off_pads(
    state: ComponentState,
    comp_id: uuid.UUID,
    placement: ComponentPlacement,
) -> None:
    """Log a warning if any pin of the component does not land on a board pad."""
    inst = state.instances.get(comp_id)
    if inst is None:
        return
    defn = state.components.get(inst.definition_ref)
    if defn is None:
        return
    from protoboard.domain.board.boxes import GapAtom, StripAtom
    pad_set: frozenset[tuple[float, float]] = frozenset(
        (center.x, center.y)
        for atom, center in state.board.atoms()
        if not isinstance(atom, GapAtom)
    )
    misses = validate_pins_on_pads(defn.footprint, placement, pad_set)
    if misses:
        _log.warning(
            "placement.pins_off_pads",
            comp_id=str(comp_id),
            refdes=inst.refdes,
            placement_pos=f"({placement.position.x:.4f}, {placement.position.y:.4f})",
            rotation=placement.rotation,
            misses=[
                {"pin": pin_num, "pos": f"({wp.x:.4f}, {wp.y:.4f})"}
                for pin_num, wp in misses
            ],
        )


def _move_blocked(
    state: ComponentState,
    comp_id: uuid.UUID,
    old_pl: ComponentPlacement,
    new_pl: ComponentPlacement,
) -> bool:
    """Return True if the move would cause a pin overlap or body overlap."""
    inst = state.instances.get(comp_id)
    if inst is None:
        return False
    defn = state.components.get(inst.definition_ref)
    if defn is None:
        return False

    new_bbox = component_bbox(defn.footprint, new_pl, defn.component_class)

    n_pins = defn.footprint.pin_count()
    new_pin_positions: set[tuple[float, float]] = set()
    for pin_num in range(1, n_pins + 1):
        p = pin_world_position(defn.footprint, new_pl, pin_num)
        new_pin_positions.add((round(p.x, 4), round(p.y, 4)))

    for other_id, other_pl in state.layout.placements.items():
        if other_id == comp_id:
            continue
        other_inst = state.instances.get(other_id)
        if other_inst is None:
            continue
        other_defn = state.components.get(other_inst.definition_ref)
        if other_defn is None:
            continue

        other_bbox = component_bbox(other_defn.footprint, other_pl, other_defn.component_class)
        if new_bbox.intersects(other_bbox):
            # Only block when no pin of the moved component coincides with a pin
            # of the other — that would be a shared-pad scenario which the GUI
            # conflict resolver handles separately.
            other_pin_positions: set[tuple[float, float]] = set()
            for pin_num in range(1, other_defn.footprint.pin_count() + 1):
                p = pin_world_position(other_defn.footprint, other_pl, pin_num)
                other_pin_positions.add((round(p.x, 4), round(p.y, 4)))
            if new_pin_positions.isdisjoint(other_pin_positions):
                return True

    return False


def _update_wires_for_move(
    state: ComponentState,
    comp_id: uuid.UUID,
    old_pl: ComponentPlacement,
    new_pl: ComponentPlacement,
    exclude_ids: frozenset[uuid.UUID] = frozenset(),
) -> None:
    """Adjust wire endpoints that coincide with pins of the moved component.

    For each pin of the component, if a wire endpoint was at the old pin
    position, move it to the new pin position.  If the resulting path
    collapses (start == end) or has fewer than 2 distinct points, the
    wire is removed.

    exclude_ids: wire IDs already translated by live drag — skipped here to
    avoid double-translation when old_pin_pos of this component coincides with
    the (already-shifted) position of another component's pin.
    """
    inst = state.instances.get(comp_id)
    if inst is None:
        return
    defn = state.components.get(inst.definition_ref)
    if defn is None:
        return

    n_pins = defn.footprint.pin_count()
    # Build map: old_pin_pos → new_pin_pos
    pin_map: dict[tuple[float, float], Point] = {}
    for pin_num in range(1, n_pins + 1):
        old_pos = pin_world_position(defn.footprint, old_pl, pin_num)
        new_pos = pin_world_position(defn.footprint, new_pl, pin_num)
        if not _pts_equal(old_pos, new_pos):
            pin_map[(round(old_pos.x, 6), round(old_pos.y, 6))] = new_pos

    if not pin_map:
        return

    from protoboard.domain.layout.wire_utils import wire_has_skewed_segment

    new_wires: list[WireSegment] = []
    for wire in state.layout.wires:
        if wire.id in exclude_ids:
            # Even excluded (already live-translated) wires may have ended up
            # with a skewed segment if the component's pin offsets stretched
            # the path unevenly. Drop those.
            if wire_has_skewed_segment(wire):
                continue
            new_wires.append(wire)
            continue
        new_path: list[Point] = []
        for pt in wire.path:
            key = (round(pt.x, 6), round(pt.y, 6))
            new_path.append(pin_map.get(key, pt))
        # Drop wire if it collapsed (all points identical or < 2 distinct).
        distinct = {(round(p.x, 6), round(p.y, 6)) for p in new_path}
        if len(distinct) < 2:
            continue
        if any(
            not _pts_equal(new_path[i], wire.path[i])
            for i in range(len(new_path))
        ):
            wire.path = new_path
        # Drop wire if any segment became skewed (neither orthogonal nor
        # diagonal) after the endpoint translation.  Cannot be represented
        # by the routing/cost model and is almost always a visual artifact
        # of repeated drag.
        if wire_has_skewed_segment(wire):
            continue
        new_wires.append(wire)

    state.layout.wires[:] = new_wires
