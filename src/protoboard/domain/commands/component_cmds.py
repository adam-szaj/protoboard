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

from protoboard.domain.board.board import Board
from protoboard.domain.commands.base import Command
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import (
    ComponentPlacement,
    component_bbox,
    pin_world_position,
)
from protoboard.domain.layout.wire import WireSegment
from protoboard.geometry import Point

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

    def revert(self, state: ComponentState) -> None:
        del state.layout.placements[self.component_id]
        state.instances.pop(self.component_id, None)


@dataclass
class MoveComponentCommand(Command[ComponentState]):
    component_id: uuid.UUID
    new_placement: ComponentPlacement
    old_placement: ComponentPlacement
    # Wires before the move — saved for undo.
    _saved_wires: list[WireSegment] = field(
        default_factory=list, init=False, repr=False
    )
    _applied: bool = field(default=False, init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Move component {self.component_id}"

    def apply(self, state: ComponentState) -> None:
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
            state, self.component_id, self.old_placement, self.new_placement
        )

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
    # net_id → Net before mutation (None = net did not exist)
    _nets_before: dict[uuid.UUID, "Net | None"] = field(
        default_factory=dict, init=False, repr=False
    )
    # net_id → Net after mutation (None = net was deleted)
    _nets_after: dict[uuid.UUID, "Net | None"] = field(
        default_factory=dict, init=False, repr=False
    )

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Remove component {self.component_id}"

    def apply(self, state: SchematicState) -> None:
        from protoboard.domain.schematic.net import Net, PinRef

        saved = state.layout.placements.pop(self.component_id)
        object.__setattr__(self, "_saved_placement", saved)
        inst = state.instances.pop(self.component_id, None)
        object.__setattr__(self, "_saved_instance", inst)

        # Remove all pins of this component from every net they appear in.
        nets_before: dict[uuid.UUID, Net | None] = {}
        nets_after: dict[uuid.UUID, Net | None] = {}
        for net_id, net in list(state.schematic.nets.items()):
            new_pins = {
                p for p in net.pins if p.component_id != self.component_id
            }
            if len(new_pins) == len(net.pins):
                continue  # this net had no pins for this component
            nets_before[net_id] = net
            if not new_pins:
                del state.schematic.nets[net_id]
                nets_after[net_id] = None
            else:
                updated = Net(id=net.id, name=net.name, pins=new_pins)
                state.schematic.nets[net_id] = updated
                nets_after[net_id] = updated
        object.__setattr__(self, "_nets_before", nets_before)
        object.__setattr__(self, "_nets_after", nets_after)

    def revert(self, state: SchematicState) -> None:
        if self._saved_placement is not None:
            state.layout.placements[self.component_id] = self._saved_placement
        if self._saved_instance is not None:
            state.instances[self.component_id] = self._saved_instance
        for net_id, net_before in self._nets_before.items():
            if net_before is None:
                state.schematic.nets.pop(net_id, None)
            else:
                state.schematic.nets[net_id] = net_before


@dataclass
class AddWireCommand(Command[ComponentState]):
    wire: WireSegment

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Add wire"

    def apply(self, state: ComponentState) -> None:
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
        from dataclasses import replace as _replace

        for i, wire in enumerate(state.layout.wires):
            if wire.id == self.wire_id:
                # Use caller-supplied old_path when available (drag case),
                # otherwise snapshot the current state (edit-commit case).
                saved = self.old_path if self.old_path else list(wire.path)
                object.__setattr__(self, "_old_path", saved)
                state.layout.wires[i] = _replace(wire, path=self.new_path)
                return
        raise KeyError(f"Wire {self.wire_id} not found")

    def revert(self, state: ComponentState) -> None:
        from dataclasses import replace as _replace

        for i, wire in enumerate(state.layout.wires):
            if wire.id == self.wire_id:
                state.layout.wires[i] = _replace(wire, path=self._old_path)
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
        from dataclasses import replace as _replace

        for i, wire in enumerate(state.layout.wires):
            if wire.id == self.wire_id:
                object.__setattr__(self, "_old_insulated", wire.insulated)
                state.layout.wires[i] = _replace(wire, insulated=self.insulated)
                return
        raise KeyError(f"Wire {self.wire_id} not found")

    def revert(self, state: ComponentState) -> None:
        from dataclasses import replace as _replace

        for i, wire in enumerate(state.layout.wires):
            if wire.id == self.wire_id:
                state.layout.wires[i] = _replace(wire, insulated=self._old_insulated)
                return


@dataclass
class DisconnectPinCommand(Command[SchematicState]):
    """Remove a pin from its net. Deletes the net if it becomes empty."""

    component_id: uuid.UUID
    pin_number: int
    _saved_net_id: uuid.UUID | None = field(default=None, init=False, repr=False)
    _saved_net: object = field(default=None, init=False, repr=False)  # Net | None

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Disconnect pin {self.pin_number}"

    def apply(self, state: SchematicState) -> None:
        from protoboard.domain.schematic.net import Net, PinRef

        ref = PinRef(component_id=self.component_id, pin_number=self.pin_number)
        net = state.schematic.net_for_pin(ref)
        if net is None:
            return
        object.__setattr__(self, "_saved_net_id", net.id)
        object.__setattr__(self, "_saved_net", net)
        new_pins = net.pins - {ref}
        if not new_pins:
            # Net becomes empty — remove it entirely.
            del state.schematic.nets[net.id]
        else:
            state.schematic.nets[net.id] = Net(
                id=net.id, name=net.name, pins=new_pins
            )

    def revert(self, state: SchematicState) -> None:
        from protoboard.domain.schematic.net import Net

        if self._saved_net_id is None or self._saved_net is None:
            return
        saved = self._saved_net
        assert isinstance(saved, Net)
        state.schematic.nets[self._saved_net_id] = saved


# ---------------------------------------------------------------------------
# Wire-update helper (used by MoveComponentCommand)
# ---------------------------------------------------------------------------

_EPS = 1e-6  # tolerance for point equality (raster units)


def _pts_equal(a: Point, b: Point) -> bool:
    return abs(a.x - b.x) < _EPS and abs(a.y - b.y) < _EPS


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

    new_bbox = component_bbox(defn.footprint, new_pl)

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

        other_bbox = component_bbox(other_defn.footprint, other_pl)
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
) -> None:
    """Adjust wire endpoints that coincide with pins of the moved component.

    For each pin of the component, if a wire endpoint was at the old pin
    position, move it to the new pin position.  If the resulting path
    collapses (start == end) or has fewer than 2 distinct points, the
    wire is removed.
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

    new_wires: list[WireSegment] = []
    for wire in state.layout.wires:
        new_path: list[Point] = []
        for pt in wire.path:
            key = (round(pt.x, 6), round(pt.y, 6))
            new_path.append(pin_map.get(key, pt))
        # Drop wire if it collapsed (all points identical or < 2 distinct).
        distinct = {(round(p.x, 6), round(p.y, 6)) for p in new_path}
        if len(distinct) >= 2:
            if any(
                not _pts_equal(new_path[i], wire.path[i])
                for i in range(len(new_path))
            ):
                from dataclasses import replace as _replace
                wire = _replace(wire, path=new_path)
        new_wires.append(wire)

    state.layout.wires[:] = new_wires
