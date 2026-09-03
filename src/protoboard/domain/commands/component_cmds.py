# SPDX-License-Identifier: MIT
"""Commands for placing, moving and removing components.

All commands operate on a ComponentState protocol so that both
Layout.placements and session.instances stay in sync through undo/redo,
without creating a hard dependency on the GUI layer.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

import structlog

from protoboard.domain.commands.base import (
    Command,
    ComponentState,
    ConditionalCommand,
    SchematicState,
)
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.resolution import resolve_instance_definition
from protoboard.domain.layout.knot import JointKnot, WirePinKnot
from protoboard.domain.layout.placement import (
    ComponentPlacement,
)
from protoboard.domain.layout.placement import (
    move_blocked as _move_blocked,
)
from protoboard.domain.layout.placement import (
    warn_pins_off_pads as _warn_pins_off_pads,
)
from protoboard.domain.layout.wire import (
    Fragment,
    KnotRef,
    Wire,
    rebuild_fragments,
)

# Wire-path helpers that operate on Wire/Layout (not command state) live in
# wire_utils next to their collaborators; imported under the historic private
# names so the command call-sites read the same.
from protoboard.domain.layout.wire_utils import (
    CollapseRecord as _CollapseRecord,
)
from protoboard.domain.layout.wire_utils import (
    ColocatedFuseRecord as _CoLocatedFuseRecord,
)
from protoboard.domain.layout.wire_utils import (
    SplitRecord as _SplitRecord,
)
from protoboard.domain.layout.wire_utils import (
    _wire_net_id as _wire_net_id,
)
from protoboard.domain.layout.wire_utils import (
    collapse_endpoint_joints as _collapse_endpoint_joints,
)
from protoboard.domain.layout.wire_utils import (
    enrich_insulated_fragments_with_pad_nodes as _enrich_insulated_fragments_with_pad_nodes,  # noqa: E501
)
from protoboard.domain.layout.wire_utils import (
    enrich_insulated_wire_with_pad_nodes as _enrich_insulated_with_pad_nodes,
)
from protoboard.domain.layout.wire_utils import (
    merge_colocated_joints_capturing as _merge_colocated_joints_capturing,
)
from protoboard.domain.layout.wire_utils import (
    plated_through_pads as _plated_through_pads,
)
from protoboard.domain.layout.wire_utils import (
    remove_wire_capturing_orphans as _remove_wire_capturing_orphans,
)
from protoboard.domain.layout.wire_utils import (
    resolve_endpoint_knot as _resolve_endpoint_knot,
)
from protoboard.domain.layout.wire_utils import (
    restore_collapse as _restore_collapse,
)
from protoboard.domain.layout.wire_utils import (
    restore_colocated_fuse as _restore_colocated_fuse,
)
from protoboard.domain.layout.wire_utils import (
    restore_split as _restore_split,
)
from protoboard.domain.layout.wire_utils import (
    restore_wire_geometry as _restore_wire_geometry,
)
from protoboard.domain.layout.wire_utils import (
    restore_wire_with_orphans as _restore_wire_with_orphans,
)
from protoboard.domain.layout.wire_utils import (
    snapshot_wire as _snapshot_wire,
)
from protoboard.domain.layout.wire_utils import (
    snapshot_wire_geometry as _snapshot_wire_geometry,
)
from protoboard.domain.layout.wire_utils import (
    split_existing_wires_at_endpoints as _split_existing_wires_at_endpoints,
)
from protoboard.domain.layout.wire_utils import (
    update_wires_for_move as _update_wires_for_move,
)
from protoboard.domain.schematic.net import PinRef
from protoboard.domain.schematic.pin_siblings import same_name_refs
from protoboard.geometry import Point

_log = structlog.get_logger(__name__)


def _dbg_enabled() -> bool:
    """Cheap gate for hot-path debug logs (see ``select_tool._dbg_enabled``)."""
    return logging.getLogger(__name__).isEnabledFor(logging.DEBUG)


if TYPE_CHECKING:
    from protoboard.domain.board.pad_state import PadIndex
    from protoboard.domain.components.appearance import Appearance
    from protoboard.domain.layout.pad_strategy import PadStrategy
    from protoboard.domain.schematic.net import Net
    from protoboard.domain.schematic.schematic import PinGroupUndo


def _strategy_from_state(state: ComponentState) -> PadStrategy | None:
    """Pull the DRC strategy off the host session, if any.

    ``ComponentState`` is a Protocol — the runtime object is usually a
    full ``EditorSession`` but unit tests can pass a minimal stand-in
    that has none of the session machinery.  Returns ``None`` when no
    strategy is reachable; callers must treat that as "skip the gate"
    so legacy tests keep working.
    """
    fn = getattr(state, "strategy_for", None)
    if fn is None:
        return None
    try:
        return fn("drc")  # type: ignore[no-any-return]
    except Exception:
        return None


def _pad_index_from_state(state: ComponentState) -> PadIndex | None:
    """Pull the cached :class:`PadIndex` off the host session, if any.

    Same defensiveness as :func:`_strategy_from_state` — when the
    state has no ``pad_index`` method (legacy unit tests, command
    replay), the strategy gates degrade to the historic behaviour
    encoded inline in ``wire_utils.find_joint_at``.
    """
    fn = getattr(state, "pad_index", None)
    if fn is None:
        return None
    try:
        return fn()  # type: ignore[no-any-return]
    except Exception:
        return None


def build_pin_identities_for(state: ComponentState, comp_id: uuid.UUID) -> None:
    """Bind a placed instance to its definition and seed its pin identities.

    Call this whenever an instance enters the project or its pin-variant
    changes. Two things must happen here, not lazily, because both must persist
    across layout-change rebuilds:

    * the same-name :class:`PinIdentity` groups (and the ``net_id`` they carry);
    * the live ``definition`` + ``package_families`` back-reference, so the
      instance can resolve its variant-aware footprint geometry on its own
      (``inst.resolved_footprint()``) without callers threading the catalogue.

    Binding returns a fresh frozen copy, so the bound instance is stored back
    into ``state.instances``. No-op if the instance or its definition is
    missing.
    """
    resolved = resolve_instance_definition(comp_id, state.instances, state.components)
    if resolved is None:
        return
    inst, defn = resolved
    bound = inst.bind(defn, getattr(state, "package_families", None))
    state.instances[comp_id] = bound
    bound.build_pin_identities(defn)


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
        build_pin_identities_for(state, self.component_id)
        _warn_pins_off_pads(
            self.component_id,
            self.placement,
            instances=state.instances,
            components=state.components,
            board=state.board,
        )

    def revert(self, state: ComponentState) -> None:
        del state.layout.placements[self.component_id]
        state.instances.pop(self.component_id, None)


@dataclass
class MoveComponentCommand(ConditionalCommand[ComponentState]):
    component_id: uuid.UUID
    new_placement: ComponentPlacement
    old_placement: ComponentPlacement
    # When True, skip the _move_blocked() check (caller already verified).
    skip_conflict_check: bool = field(default=False)
    # Wire IDs already translated by live drag — skip in _update_wires_for_move
    # to prevent double-translation when old_pin_pos == new_pin_pos of another component.  # noqa: E501
    exclude_wire_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)
    # When True, the command mutates only ``layout.placements`` and leaves
    # all wires alone. The drag-commit pipeline uses this in conjunction
    # with a preceding ``TranslateAnchoredWiresCommand`` so that wire
    # translation is a first-class, undo-distinct step instead of an
    # invisible side effect of the move.
    placement_only: bool = field(default=False)
    # Wires before the move — saved for undo.
    _saved_wires: list[Wire] = field(default_factory=list, init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Move component {self.component_id}"

    def apply(self, state: ComponentState) -> None:
        # Locked components never move — silently treat as no-op.
        if state.layout.placements.get(self.component_id, self.old_placement).locked:
            self.mark_applied(False)
            return
        if not self.skip_conflict_check:
            blocked = _move_blocked(
                self.component_id,
                self.new_placement,
                instances=state.instances,
                components=state.components,
                layout=state.layout,
            )
            if blocked:
                # Conflict — silently keep old position; mark as no-op for undo.
                self.mark_applied(False)
                state.layout.placements[self.component_id] = self.old_placement
                return
        self.mark_applied(True)
        if not self.placement_only:
            self._saved_wires = list(state.layout.wires)
        state.layout.placements[self.component_id] = self.new_placement
        if not self.placement_only:
            _update_wires_for_move(
                state.layout.wires,
                layout=state.layout,
                instances=state.instances,
                components=state.components,
                comp_id=self.component_id,
                old_pl=self.old_placement,
                new_pl=self.new_placement,
                exclude_ids=self.exclude_wire_ids,
            )
        _warn_pins_off_pads(
            self.component_id,
            self.new_placement,
            instances=state.instances,
            components=state.components,
            board=state.board,
        )
        # No group bookkeeping: the stored group origin is gone — the bbox
        # top-left is computed on demand (GroupTree.group_origin) and the
        # rotate pivot derives from the group transform, so a member move
        # needs no per-group sync or undo state.

    def _do_revert(self, state: ComponentState) -> None:
        state.layout.placements[self.component_id] = self.old_placement
        if not self.placement_only:
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
    _nets_before: dict[uuid.UUID, tuple[str, set[PinRef]]] = field(
        default_factory=dict, init=False, repr=False
    )

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Remove component {self.component_id}"

    def apply(self, state: SchematicState) -> None:
        self._saved_placement = state.layout.placements.pop(self.component_id)
        self._saved_instance = state.instances.pop(self.component_id, None)
        self._nets_before = state.schematic.remove_component_pins(self.component_id)

    def revert(self, state: SchematicState) -> None:
        if self._saved_placement is not None:
            state.layout.placements[self.component_id] = self._saved_placement
        if self._saved_instance is not None:
            state.instances[self.component_id] = self._saved_instance
        for net_id, (net_name, removed_pins) in self._nets_before.items():
            state.schematic.restore_pins(net_id, net_name, removed_pins)


def _wire_intended_net(
    state: ComponentState,
    wire: Wire,
) -> uuid.UUID | None:
    """Resolve *wire* to a schematic net id via its endpoint pins.

    Returns None when the wire touches no pin (orphan / pure
    knot-to-knot junction) or when the state has no schematic.  Used by
    AddWireCommand to gate joint reuse so an autoroute pass cannot
    merge wires of different nets into a phantom shared hub.

    Passes ``state.board`` so cross-side pin resolution through
    plated-through pads works — the autoroute common case where a wire
    is on the bottom layer but the component pin lives on top.
    """
    schematic = getattr(state, "schematic", None)
    if schematic is None:
        return None
    return _wire_net_id(
        wire,
        schematic,
        state.layout,
        state.instances,
        state.components,
        board=state.board,
    )


@dataclass
class AddWireCommand(Command[ComponentState]):
    wire: Wire
    # Joint knots created on first apply; restored on redo, removed on revert.
    _created_joints: list[JointKnot] = field(
        default_factory=list, init=False, repr=False
    )
    # True once apply() has resolved knots and switched to v2 storage.
    _knots_resolved: bool = field(default=False, init=False, repr=False)
    # Split records — one per endpoint that produced a T-junction by
    # cutting an existing wire at the meeting pad.  Empty when no
    # interior-vertex collision was detected.
    _splits: list[_SplitRecord] = field(default_factory=list, init=False, repr=False)
    # Collapse records — one per endpoint that fused with a neighbouring
    # wire of matching ``insulated``.  Empty when no fusion happened.
    _collapses: list[_CollapseRecord] = field(
        default_factory=list, init=False, repr=False
    )

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Add wire"

    def apply(self, state: ComponentState) -> None:
        # Enrich insulated wires with pad-position vertices so simplify /
        # unweave / edit tools have nodes at every pad the wire physically
        # crosses.  Idempotent — redo doesn't re-grow the path.
        self.wire = _enrich_insulated_with_pad_nodes(self.wire, state.board)
        if not self._knots_resolved:
            if self.wire.a is None or self.wire.b is None:
                start = self.wire.path[0]
                end = self.wire.path[-1]
                # Constrain joint reuse to compatible existing joints so
                # an autoroute pass cannot fuse wires of different nets
                # into a phantom degree-N hub at an off-pin pad (the
                # reference_big_project bug).
                wire_net_id = _wire_intended_net(state, self.wire)
                strategy = _strategy_from_state(state)
                pad_index = _pad_index_from_state(state)
                a_ref, a_created = _resolve_endpoint_knot(
                    start,
                    layout=state.layout,
                    instances=state.instances,
                    components=state.components,
                    board=state.board,
                    schematic=getattr(state, "schematic", None),
                    side=self.wire.side,
                    net_id=wire_net_id,
                    strategy=strategy,
                    pad_index=pad_index,
                )
                b_ref, b_created = _resolve_endpoint_knot(
                    end,
                    layout=state.layout,
                    instances=state.instances,
                    components=state.components,
                    board=state.board,
                    schematic=getattr(state, "schematic", None),
                    side=self.wire.side,
                    net_id=wire_net_id,
                    strategy=strategy,
                    pad_index=pad_index,
                )
                self.wire.a = a_ref
                self.wire.b = b_ref
                if a_created is not None:
                    self._created_joints.append(a_created)
                if b_created is not None:
                    self._created_joints.append(b_created)
            self._knots_resolved = True
        else:
            # Redo: re-register joints we created and dropped on revert.
            for jk in self._created_joints:
                state.layout.knots.setdefault(jk.id, jk)
        # add_wire is a no-op on redo when the previous revert restored
        # ``self.wire`` from a collapse snapshot — it's already in layout.
        if self.wire.id not in state.layout.wires:
            state.layout.add_wire(self.wire)
        # Interior-vertex split: when an endpoint of ``self.wire`` lands
        # on the interior vertex of an existing wire, atomically split
        # that wire and re-anchor our endpoint to the shared hub
        # (T-junction with degree-3 at the meeting pad).  ``make_t_junction``
        # mutates ``self.wire``'s ``a``/``b`` knot refs in place.
        new_splits = _split_existing_wires_at_endpoints(
            self.wire,
            layout=state.layout,
            board=state.board,
            schematic=getattr(state, "schematic", None),
            instances=state.instances,
            components=state.components,
        )
        if new_splits and not self._splits:
            self._splits = new_splits
        # Endpoint-merge: when this wire's endpoint sits on an off-pin pad
        # already used by exactly one other wire and they share
        # ``insulated``, the two wires represent one solderable artifact —
        # fuse them through ``try_collapse_joint``.  Pin endpoints are
        # never fused (pins are physical hubs).  We record each collapse
        # so revert() can undo it on demand.  Recompute on every apply
        # (including redo) so the layout state matches what the first
        # apply produced — the recorded snapshots are only used by revert.
        new_collapses = _collapse_endpoint_joints(
            self.wire,
            layout=state.layout,
            board=state.board,
        )
        if new_collapses and not self._collapses:
            self._collapses = new_collapses

    def revert(self, state: ComponentState) -> None:
        if self._collapses:
            # Undo collapses in reverse order — ``_restore_collapse``
            # reinstates BOTH source wires (including ``self.wire`` in its
            # pre-collapse form, so we don't remove it again here).
            for collapse_record in reversed(self._collapses):
                _restore_collapse(state.layout, collapse_record)
            # Now drop just ``self.wire`` so the layout reaches the
            # pre-apply state (one wire = the other source = neighbour
            # that existed before this AddWireCommand fired).
            if self.wire.id in state.layout.wires:
                state.layout.remove_wire(self.wire.id)
        else:
            if self.wire.id in state.layout.wires:
                state.layout.remove_wire(self.wire.id)
        # Undo any T-junction splits we created so the wires we cut
        # are restored to their pre-split single-piece form.  Run after
        # we removed ``self.wire`` so the hub becomes orphan-eligible.
        for split_record in reversed(self._splits):
            _restore_split(state.layout, self.wire, split_record)
        # Joints we created may still hold back-refs to other wires (shared
        # joint).  ``remove_wire`` already prunes orphans; anything still
        # in layout.knots is a non-orphan we must leave alone.
        for jk in self._created_joints:
            if jk.id in state.layout.knots:
                continue  # not orphan after remove_wire; keep
            # Already removed by remove_wire as orphan — nothing to do.


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

    wire: Wire
    # net_id → Net before mutation (None = net did not exist yet)
    _nets_before: dict[uuid.UUID, Net | None] = field(
        default_factory=dict, init=False, repr=False
    )
    # net_id → Net after mutation (None = net was deleted)
    _nets_after: dict[uuid.UUID, Net | None] = field(
        default_factory=dict, init=False, repr=False
    )
    _first_apply: bool = field(default=True, init=False, repr=False)
    _created_joints: list[JointKnot] = field(
        default_factory=list, init=False, repr=False
    )
    _knots_resolved: bool = field(default=False, init=False, repr=False)
    _splits: list[_SplitRecord] = field(default_factory=list, init=False, repr=False)
    _collapses: list[_CollapseRecord] = field(
        default_factory=list, init=False, repr=False
    )

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
        if not self._knots_resolved:
            if self.wire.a is None or self.wire.b is None:
                start = self.wire.path[0]
                end = self.wire.path[-1]
                strategy = _strategy_from_state(state)
                pad_index = _pad_index_from_state(state)
                a_ref, a_created = _resolve_endpoint_knot(
                    start,
                    layout=state.layout,
                    instances=state.instances,
                    components=state.components,
                    board=state.board,
                    schematic=getattr(state, "schematic", None),
                    strategy=strategy,
                    pad_index=pad_index,
                )
                b_ref, b_created = _resolve_endpoint_knot(
                    end,
                    layout=state.layout,
                    instances=state.instances,
                    components=state.components,
                    board=state.board,
                    schematic=getattr(state, "schematic", None),
                    strategy=strategy,
                    pad_index=pad_index,
                )
                self.wire.a = a_ref
                self.wire.b = b_ref
                if a_created is not None:
                    self._created_joints.append(a_created)
                if b_created is not None:
                    self._created_joints.append(b_created)
            self._knots_resolved = True
        else:
            for jk in self._created_joints:
                state.layout.knots.setdefault(jk.id, jk)
        if self.wire.id not in state.layout.wires:
            state.layout.add_wire(self.wire)
        # Interior-vertex split + endpoint-merge — see AddWireCommand.apply
        # for the full rationale.  Same ordering matters: split first so
        # the hub exists at degree-3 before merge looks for degree-2
        # endpoint joints to collapse.
        new_splits = _split_existing_wires_at_endpoints(
            self.wire,
            layout=state.layout,
            board=state.board,
            schematic=getattr(state, "schematic", None),
            instances=state.instances,
            components=state.components,
        )
        if new_splits and not self._splits:
            self._splits = new_splits
        new_collapses = _collapse_endpoint_joints(
            self.wire,
            layout=state.layout,
            board=state.board,
        )
        if new_collapses and not self._collapses:
            self._collapses = new_collapses
        if self._first_apply:
            # Net mutations already applied by resolver — nothing more to do.
            self._first_apply = False
            return
        # Redo path: re-apply recorded net mutations.
        schematic = getattr(state, "schematic")  # EditorSession always has this
        for net_id, net_after in self._nets_after.items():
            if net_after is None:
                schematic.nets.pop(net_id, None)
            else:
                schematic.nets[net_id] = net_after

    def revert(self, state: ComponentState) -> None:
        for collapse_record in reversed(self._collapses):
            _restore_collapse(state.layout, collapse_record)
        if self.wire.id in state.layout.wires:
            state.layout.remove_wire(self.wire.id)
        for split_record in reversed(self._splits):
            _restore_split(state.layout, self.wire, split_record)
        schematic = getattr(state, "schematic")
        for net_id, net_before in self._nets_before.items():
            if net_before is None:
                schematic.nets.pop(net_id, None)
            else:
                schematic.nets[net_id] = net_before


@dataclass
class RemoveWireCommand(Command[ComponentState]):
    wire_id: uuid.UUID
    _saved_wire: Wire | None = field(default=None, init=False, repr=False)
    # JointKnots that became orphan when the wire was removed; restored on revert.
    _saved_orphan_joints: list[JointKnot | WirePinKnot] = field(
        default_factory=list, init=False, repr=False
    )

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Remove wire"

    def apply(self, state: ComponentState) -> None:
        wire, orphans = _remove_wire_capturing_orphans(
            state.layout,
            self.wire_id,
        )
        if wire is None:
            # Macro race: a prior command in the same macro already
            # dropped this wire (e.g. ``_update_wires_for_move`` dropped
            # it as ``skewed`` after translating a PinKnot endpoint by
            # the drag delta).  The wire would have been removed
            # anyway, so treat this as a successful no-op rather than
            # crashing the macro.  Revert is a no-op too — when undo
            # walks back through the macro, the sibling command that
            # actually removed the wire will restore it.
            self._saved_wire = None
            self._saved_orphan_joints = []
            _log.debug(
                "remove_wire.skipped_already_gone",
                wire_id=str(self.wire_id),
            )
            return
        self._saved_wire = wire
        self._saved_orphan_joints = orphans

    def revert(self, state: ComponentState) -> None:
        if self._saved_wire is None:
            return
        _restore_wire_with_orphans(
            state.layout,
            self._saved_wire,
            self._saved_orphan_joints,
        )


@dataclass
class TranslateAnchoredWiresCommand(ConditionalCommand[ComponentState]):
    """Translate wires anchored to a moved component's pins.

    Spun out of ``MoveComponentCommand`` so the drag-commit pipeline
    can sequence wire translation as a distinct, undo-able step. The
    underlying helper is shared with the legacy in-Move path —
    behavior is identical, but observability and rollback are not.

    Anchoring is decided by ``_update_wires_for_move``'s PinKnot
    check: only wires whose endpoints reference this component's pins
    are translated; foreign wires whose path happens to pass through
    the old pin position are left alone (this is what fixes the
    "foreign wire glued to moved pin" bug ``eae4cd4``).

    Undo snapshots both the wires list *and* each wire's path/fragments
    so a translated-in-place wire can be restored even if the list
    itself is unchanged. Pure list snapshot (the pattern
    ``MoveComponentCommand`` uses) restores only the list identity but
    leaves mutated wires mutated.
    """

    component_id: uuid.UUID
    new_placement: ComponentPlacement
    old_placement: ComponentPlacement
    exclude_wire_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)
    _saved_wires: list[Wire] = field(
        default_factory=list,
        init=False,
        repr=False,
    )
    _saved_geometry: dict[uuid.UUID, tuple[list[Point], list[Fragment]]] = field(
        default_factory=dict, init=False, repr=False
    )

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Translate wires anchored to component {self.component_id}"

    def apply(self, state: ComponentState) -> None:
        self._saved_wires = list(state.layout.wires)
        self._saved_geometry = _snapshot_wire_geometry(state.layout)
        self.mark_applied(True)
        wires_before = {w.id: list(w.path) for w in state.layout.wires}
        _update_wires_for_move(
            state.layout.wires,
            layout=state.layout,
            instances=state.instances,
            components=state.components,
            comp_id=self.component_id,
            old_pl=self.old_placement,
            new_pl=self.new_placement,
            exclude_ids=self.exclude_wire_ids,
        )
        # Per-component diagnostic: how many wires changed path, were
        # dropped (skewed/collapsed), or were left alone.  Group-floating
        # commit chains many of these — without per-cmd stats it's
        # impossible to tell which component's translation lost a wire.
        if _dbg_enabled():
            after_ids = {w.id for w in state.layout.wires}
            dropped = [str(wid) for wid in wires_before if wid not in after_ids]
            changed = []
            for w in state.layout.wires:
                old = wires_before.get(w.id)
                if old is None:
                    continue
                if old != w.path:
                    changed.append(str(w.id)[:8])
            _log.debug(
                "translate_anchored_wires.summary",
                comp_id=str(self.component_id)[:8],
                old_pos=(
                    self.old_placement.position.x,
                    self.old_placement.position.y,
                ),
                new_pos=(
                    self.new_placement.position.x,
                    self.new_placement.position.y,
                ),
                delta=(
                    self.new_placement.position.x - self.old_placement.position.x,
                    self.new_placement.position.y - self.old_placement.position.y,
                ),
                n_excluded=len(self.exclude_wire_ids),
                n_wires_before=len(wires_before),
                n_wires_after=len(state.layout.wires),
                n_dropped=len(dropped),
                n_changed=len(changed),
                dropped=dropped[:10],  # cap to keep log readable
                changed=changed[:10],
            )

    def _do_revert(self, state: ComponentState) -> None:
        state.layout.wires[:] = self._saved_wires
        _restore_wire_geometry(state.layout, self._saved_geometry)


@dataclass
class DropWiresCommand(Command[ComponentState]):
    """Remove a set of wires atomically.

    Equivalent to executing N ``RemoveWireCommand`` instances but
    snapshotted as a single undo step — used by the drag-commit
    pipeline when the detector decides several wires must go (skewed,
    crossing alien pads, pin-on-wire). Saves the affected wires plus
    any orphan joints so revert restores both.
    """

    wire_ids: frozenset[uuid.UUID]
    _saved_wires: list[Wire] = field(
        default_factory=list,
        init=False,
        repr=False,
    )
    _saved_orphan_joints: list[JointKnot | WirePinKnot] = field(
        default_factory=list,
        init=False,
        repr=False,
    )

    @property
    def description(self) -> str:  # type: ignore[override]
        n = len(self.wire_ids)
        return f"Drop {n} wire{'s' if n != 1 else ''}"

    def apply(self, state: ComponentState) -> None:
        saved: list[Wire] = []
        orphans: list[JointKnot | WirePinKnot] = []
        for wid in self.wire_ids:
            wire, w_orphans = _remove_wire_capturing_orphans(state.layout, wid)
            if wire is None:
                continue
            saved.append(wire)
            orphans.extend(w_orphans)
        self._saved_wires = saved
        self._saved_orphan_joints = orphans

    def revert(self, state: ComponentState) -> None:
        for wire in self._saved_wires:
            _restore_wire_with_orphans(
                state.layout,
                wire,
                self._saved_orphan_joints,
            )


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
    """Move a wire to the top or bottom layer.

    Mutates both ``wire.side`` and every fragment's ``side`` — the
    on-disk format keys side off the fragment list, so updating just
    ``wire.side`` would round-trip incorrectly (the loader rebuilds
    ``wire.side`` from ``fragments[0].side``).
    """

    wire_id: uuid.UUID
    side: str  # "top" or "bottom"
    _old_side: str = field(default="top", init=False, repr=False)
    _old_fragment_sides: list[str] = field(
        default_factory=list,
        init=False,
        repr=False,
    )

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Move wire to {self.side}"

    def apply(self, state: ComponentState) -> None:
        for wire in state.layout.wires:
            if wire.id == self.wire_id:
                self._old_side = wire.side
                self._old_fragment_sides = [f.side for f in wire.fragments]
                wire.side = self.side  # type: ignore[assignment]
                wire.fragments = rebuild_fragments(
                    wire.fragments,
                    [self.side] * len(wire.fragments),  # type: ignore[list-item]
                )
                return
        raise KeyError(f"Wire {self.wire_id} not found")

    def revert(self, state: ComponentState) -> None:
        for wire in state.layout.wires:
            if wire.id == self.wire_id:
                wire.side = self._old_side  # type: ignore[assignment]
                if self._old_fragment_sides:
                    wire.fragments = rebuild_fragments(
                        wire.fragments,
                        self._old_fragment_sides,  # type: ignore[arg-type]
                    )
                return


@dataclass
class FlipWireSideCommand(Command[ComponentState]):
    """Flip a whole wire by toggling EACH fragment's side independently.

    A mixed wire ``[top, bottom, top]`` becomes ``[bottom, top, bottom]`` — it
    stays mixed, inverted. Distinct from :class:`SetWireSideCommand`, which
    flattens every fragment to one side ("Move to <side>"). Reads no direction
    off ``wire.side`` (it always inverts), so it cannot flatten a mixed wire the
    way the old ``side=None`` toggle did (see docs/todo.md, commit cd01742).
    """

    wire_id: uuid.UUID
    _old_fragment_sides: list[str] = field(
        default_factory=list,
        init=False,
        repr=False,
    )

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Flip wire"

    def apply(self, state: ComponentState) -> None:
        wire = state.layout.wires.get_or_raise(self.wire_id)
        self._old_fragment_sides = [f.side for f in wire.fragments]
        flipped = ["bottom" if s == "top" else "top" for s in self._old_fragment_sides]
        wire.fragments = rebuild_fragments(wire.fragments, flipped)  # type: ignore[arg-type]
        wire.side = wire.fragments[0].side

    def revert(self, state: ComponentState) -> None:
        if not self._old_fragment_sides:
            return
        wire = state.layout.wires.get_or_raise(self.wire_id)
        wire.fragments = rebuild_fragments(
            wire.fragments,
            self._old_fragment_sides,  # type: ignore[arg-type]
        )
        wire.side = wire.fragments[0].side


@dataclass
class FuseTouchingWiresCommand(Command[ComponentState]):
    """Collapse every pair of wires that share a 2-degree JointKnot.

    Walks every JointKnot in the layout (optionally filtered to a
    caller-supplied wire-id scope), and for each joint whose
    ``wire_ids`` size is 2 and whose two incident wires share
    ``insulated``, runs :meth:`Layout.try_collapse_joint` to fuse the
    pair into a single wire.

    Runs repeatedly until no more collapses succeed — a chain of N
    touching wires needs N-1 passes.  Each successful collapse is
    snapshotted as a :class:`_CollapseRecord` so ``revert`` can split
    every fused pair back into its two source wires (in reverse
    order, mirroring AddWireCommand's collapse-undo path).

    Two distinct wires meeting on an off-pin pad are an artifact of
    autoroute / drag / cut intermediate states — physically they
    represent one solderable conductor.  The save-time pass already
    fuses them (``project.endpoint_merge_on_save``) but the runtime
    layout keeps them as separate Wire instances until either the
    file is reopened or this command runs.
    """

    wire_ids: frozenset[uuid.UUID] | None = None  # None → whole layout
    _records: list[_CollapseRecord] = field(
        default_factory=list,
        init=False,
        repr=False,
    )
    _colocated_records: list[_CoLocatedFuseRecord] = field(
        default_factory=list,
        init=False,
        repr=False,
    )

    @property
    def description(self) -> str:  # type: ignore[override]
        scope = "all" if self.wire_ids is None else f"{len(self.wire_ids)} wire(s)"
        return f"Fuse touching wires ({scope})"

    def _phase0_merge_colocated_joints(
        self,
        state: ComponentState,
    ) -> None:
        """Pre-pass: merge degree-1 JointKnot pairs sitting on the same pad.

        Legacy projects (and pre-fix flip-fragment edits) can leave two
        wires whose endpoints sit on the same pad but reference distinct
        degree-1 JointKnots.  Physically they are one solder joint, but
        the main collapse loop only fuses knots that already share a
        degree-2 joint — so it would emit nothing.  This pre-pass fuses
        the co-located pairs (scoped by ``self.wire_ids``) so the main
        loop sees a degree-2 hub, recording each merge for revert.

        Delegates to ``wire_utils.merge_colocated_joints_capturing``.
        """
        self._colocated_records.extend(
            _merge_colocated_joints_capturing(
                state.layout,
                scope_wire_ids=self.wire_ids,
            )
        )

    def apply(self, state: ComponentState) -> None:
        pt_pads = frozenset(_plated_through_pads(state.board))
        records: list[_CollapseRecord] = []
        # Phase 0 — pre-merge co-located degree-1 JointKnots so the main
        # collapse loop can see them as a single degree-2 hub.  Without
        # this, legacy artefacts from pre-fix flip-fragment edits leave
        # the user unable to wire-merge their own wires.
        self._colocated_records = []
        self._phase0_merge_colocated_joints(state)
        # Stats — surfaced via wire.fuse.summary at the end so we can
        # see why nothing fused without firing a log line per joint.
        stat = {
            "n_joint_knots_total": 0,
            "skip_not_jointknot": 0,
            "skip_wire_count_neq_2": 0,
            "skip_wire_missing": 0,
            "skip_insulated_mismatch": 0,
            "skip_scope_filter": 0,
            "try_collapse_called": 0,
            "try_collapse_failed": 0,
            "try_collapse_ok": 0,
            "passes": 0,
            "phase0_colocated_merged": len(self._colocated_records),
        }
        # Iterate until no more collapses fire; one pass is not enough
        # for a chain of three or more touching wires.
        progress = True
        while progress:
            stat["passes"] += 1
            progress = False
            for knot_id in list(state.layout.knots.keys()):
                knot = state.layout.knots.get(knot_id)
                if not isinstance(knot, JointKnot):
                    stat["skip_not_jointknot"] += 1
                    continue
                stat["n_joint_knots_total"] += 1
                if len(knot.wire_ids) != 2:
                    stat["skip_wire_count_neq_2"] += 1
                    _log.debug(
                        "wire.fuse.skip_degree",
                        knot_id=str(knot_id)[:8],
                        pos=(knot.position.x, knot.position.y),
                        degree=len(knot.wire_ids),
                    )
                    continue
                w_ids = tuple(knot.wire_ids)
                w1 = state.layout.wires.get(w_ids[0])
                w2 = state.layout.wires.get(w_ids[1])
                if w1 is None or w2 is None:
                    stat["skip_wire_missing"] += 1
                    continue
                if w1.insulated != w2.insulated:
                    stat["skip_insulated_mismatch"] += 1
                    _log.debug(
                        "wire.fuse.skip_insulation",
                        knot_id=str(knot_id)[:8],
                        w1=str(w1.id)[:8],
                        w1_ins=w1.insulated,
                        w2=str(w2.id)[:8],
                        w2_ins=w2.insulated,
                    )
                    continue
                # Scope filter: when the caller restricted us to a
                # wire-id set, only collapse joints whose BOTH incident
                # wires are in the set.  Otherwise a "merge selected"
                # action might silently pull in unrelated wires.
                if self.wire_ids is not None and not (
                    w1.id in self.wire_ids and w2.id in self.wire_ids
                ):
                    stat["skip_scope_filter"] += 1
                    _log.debug(
                        "wire.fuse.skip_scope",
                        knot_id=str(knot_id)[:8],
                        w1_in=(w1.id in self.wire_ids),
                        w2_in=(w2.id in self.wire_ids),
                    )
                    continue
                snapshot = _CollapseRecord(
                    joint_id=knot_id,
                    joint_pos=knot.position,
                    w1_snapshot=_snapshot_wire(w1, state.layout),
                    w2_snapshot=_snapshot_wire(w2, state.layout),
                )
                stat["try_collapse_called"] += 1
                merged = state.layout.try_collapse_joint(
                    knot_id,
                    plated_through_pads=pt_pads,
                )
                if merged is None:
                    stat["try_collapse_failed"] += 1
                    _log.info(
                        "wire.fuse.try_collapse_failed",
                        knot_id=str(knot_id)[:8],
                        pos=(knot.position.x, knot.position.y),
                        w1=str(w1.id)[:8],
                        w2=str(w2.id)[:8],
                        w1_side=w1.fragments[0].side if w1.fragments else None,
                        w2_side=w2.fragments[0].side if w2.fragments else None,
                        joint_is_plated_through=(knot.position in pt_pads),
                    )
                    continue
                stat["try_collapse_ok"] += 1
                _log.info(
                    "wire.fuse.merged",
                    knot_id=str(knot_id)[:8],
                    merged_id=str(merged.id)[:8],
                    n_fragments=len(merged.fragments),
                )
                snapshot.merged_wire_id = merged.id
                records.append(snapshot)
                progress = True
        _log.info(
            "wire.fuse.summary",
            scope_size=(None if self.wire_ids is None else len(self.wire_ids)),
            **stat,
        )
        # Record only on first apply — redo reuses the recorded
        # snapshots' joint ids (recompute would give a different
        # result if the layout state already differs from the
        # post-apply snapshot, which can't happen here because we
        # roll back fully).
        if records and not self._records:
            self._records = records

    def revert(self, state: ComponentState) -> None:
        # Undo collapses in reverse order — _restore_collapse reinstates
        # both source wires.  Each iteration drops the merged wire and
        # re-inserts the two pre-fuse wires.
        for rec in reversed(self._records):
            _restore_collapse(state.layout, rec)
        # Phase-0 co-located fuses run BEFORE the main collapse loop in
        # apply, so their inverses must run AFTER the collapse-restore
        # walk to land in the right model state.
        for crec in reversed(self._colocated_records):
            _restore_colocated_fuse(state.layout, crec)


# ---------------------------------------------------------------------------
# Wire-pin conversion (docs/archive/wire-pin-implementation.md §3.6)
# ---------------------------------------------------------------------------


@dataclass
class ConvertEndpointToWirePinCommand(Command[ComponentState]):
    """Promote a half-orphan wire endpoint to a persistent wire-pin.

    Replaces the wire's named endpoint (``"a"`` or ``"b"``) — currently a
    :class:`JointKnot` — with a :class:`WirePinKnot` at the same id and
    position.  ID-preserving replacement keeps the wire's ``KnotRef``
    intact; no wire path, fragment, or back-reference needs to change.

    Preconditions checked at apply-time (raise ``ValueError``):

    * the named endpoint exists,
    * the endpoint knot is a ``JointKnot`` (not already a wire-pin or a
      component-pin anchor),
    * the joint has exactly one incident wire, this one (i.e. it is a
      half-orphan endpoint, not a t-junction hub).

    Pad-coincidence and plated-through enforcement live in DRC
    (``WIRE_PIN_OFF_PLATED_THROUGH``), not here — the GUI guards
    against creating wire-pins on disqualified pads at menu-build
    time.
    """

    wire_id: uuid.UUID
    endpoint: str  # "a" or "b"
    net_id: uuid.UUID | None  # snapshot of the wire's current net at conversion
    _old_knot: JointKnot | None = field(default=None, init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Convert endpoint to wire-pin"

    def _ref(self, wire: Wire) -> KnotRef | None:
        return wire.a if self.endpoint == "a" else wire.b

    def apply(self, state: ComponentState) -> None:
        if self.endpoint not in ("a", "b"):
            raise ValueError(f"endpoint must be 'a' or 'b'; got {self.endpoint!r}")
        wire = state.layout.wires.get(self.wire_id)
        if wire is None:
            raise KeyError(f"Wire {self.wire_id} not found")
        ref = self._ref(wire)
        if ref is None:
            raise ValueError(
                f"Wire {self.wire_id} has no {self.endpoint!r} endpoint to convert"
            )
        knot = state.layout.knots.get(ref.id)
        if not isinstance(knot, JointKnot):
            raise ValueError(
                f"endpoint {self.endpoint!r} of wire {self.wire_id} is "
                f"{type(knot).__name__ if knot else 'missing'}, expected JointKnot"
            )
        if knot.wire_ids != frozenset({self.wire_id}):
            raise ValueError(
                f"joint {ref.id} is not a sole-owner half-orphan "
                f"(wire_ids={knot.wire_ids}); cannot convert to wire-pin"
            )
        self._old_knot = knot
        state.layout.knots[ref.id] = WirePinKnot(
            id=knot.id,
            position=knot.position,
            net_id=self.net_id,
            wire_ids=knot.wire_ids,
        )

    def revert(self, state: ComponentState) -> None:
        if self._old_knot is None:
            return
        wire = state.layout.wires.get(self.wire_id)
        if wire is None:
            return
        ref = self._ref(wire)
        if ref is None:
            return
        state.layout.knots[ref.id] = self._old_knot


@dataclass
class ConvertWirePinToEndpointCommand(Command[ComponentState]):
    """Inverse of :class:`ConvertEndpointToWirePinCommand`.

    Replaces the named :class:`WirePinKnot` with a :class:`JointKnot` at
    the same id and position.  The wire's ``KnotRef`` keeps pointing at
    the same id, so no wire-side edits are required.
    """

    wire_pin_id: uuid.UUID
    _old_knot: WirePinKnot | None = field(default=None, init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Convert wire-pin back to free endpoint"

    def apply(self, state: ComponentState) -> None:
        knot = state.layout.knots.get(self.wire_pin_id)
        if not isinstance(knot, WirePinKnot):
            raise ValueError(
                f"knot {self.wire_pin_id} is "
                f"{type(knot).__name__ if knot else 'missing'}, expected WirePinKnot"
            )
        self._old_knot = knot
        state.layout.knots[self.wire_pin_id] = JointKnot(
            id=knot.id,
            position=knot.position,
            wire_ids=knot.wire_ids,
        )

    def revert(self, state: ComponentState) -> None:
        if self._old_knot is None:
            return
        state.layout.knots[self.wire_pin_id] = self._old_knot


@dataclass
class SetComponentPackageVariantCommand(Command[ComponentState]):
    """Change the package-family variant of a component instance.

    ``new_variant`` is a ``"family/variant_id"`` string referencing the
    package-family catalogue, or ``None`` to clear the per-instance override
    (falling back to the definition's default ``PackageOption`` / inline package).
    Validation is intentionally minimal; resolution raises when a selected
    address is not present in the package-family catalogue.
    """

    component_id: uuid.UUID
    new_variant: str | None
    _old_variant: str | None = field(default=None, init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Set package variant to {self.new_variant or 'default'}"

    def apply(self, state: ComponentState) -> None:
        inst = state.instances[self.component_id]
        self._old_variant = inst.package_variant

        state.instances[self.component_id] = replace(
            inst,
            package_variant=self.new_variant,
        )

    def revert(self, state: ComponentState) -> None:
        inst = state.instances[self.component_id]

        state.instances[self.component_id] = replace(
            inst,
            package_variant=self._old_variant,
        )


@dataclass
class SetComponentAppearanceCommand(Command[ComponentState]):
    """Set or clear the per-instance visual style override.

    ``new_appearance`` is an :class:`~protoboard.domain.components.appearance.
    Appearance` (fill colour and/or corner radius), or ``None`` to clear the
    override (falling back to the option/definition style, then the
    package/theme defaults). Visual only — geometry, DRC and routing are
    unaffected.
    """

    component_id: uuid.UUID
    new_appearance: Appearance | None
    _old: Appearance | None = field(default=None, init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return (
            "Set appearance" if self.new_appearance is not None else "Clear appearance"
        )

    def apply(self, state: ComponentState) -> None:
        inst = state.instances[self.component_id]
        self._old = inst.appearance

        state.instances[self.component_id] = replace(
            inst,
            appearance=self.new_appearance,
        )

    def revert(self, state: ComponentState) -> None:
        inst = state.instances[self.component_id]

        state.instances[self.component_id] = replace(
            inst,
            appearance=self._old,
        )


@dataclass
class FlipWireFragmentCommand(Command[ComponentState]):
    """Flip a contiguous fragment of a wire to the opposite layer.

    The fragment is identified by *start_idx* and *end_idx* (inclusive)
    in the wire's current path.  Side flips inside a wire are represented
    natively as a multi-fragment Wire — each ``Fragment`` records its
    own ``side``, and consecutive fragments share their boundary vertex
    (which must be a plated-through pad, i.e. a via).  This matches the
    "one artifact = one Wire" contract: the conductor stays a single
    Wire, retains its id and endpoint knots, and only its per-fragment
    side composition changes.

    The earlier implementation split the original wire into 2-3 separate
    Wire instances via ``layout.wires.append`` without going through
    ``add_wire`` / ``ensure_wire_knots``.  Two flips in sequence on
    adjacent fragments produced four wires with co-located stale joint
    knots — see ``memory/project_wire_id_substitution_followup.md``.

    Whole-wire flips (``start == 0`` and ``end == len(path) - 1``) re-side
    every fragment in place; sub-fragment flips rebuild the fragment list
    around the via boundaries.
    """

    wire_id: uuid.UUID
    start_idx: int
    end_idx: int
    target_side: str  # "top" or "bottom"
    # Undo state — the wire's pre-apply fragment list, restored verbatim.
    _old_fragments: list[Fragment] = field(
        default_factory=list,
        init=False,
        repr=False,
    )

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Flip wire fragment to {self.target_side}"

    def _build_fragments_for_flip(
        self,
        wire: Wire,
    ) -> list[Fragment]:
        """Build the new fragment list for ``wire`` with the requested
        sub-range flipped to ``self.target_side``.

        The result preserves the wire's path verbatim; only the
        per-fragment ``side`` composition changes.  Re-coalesces adjacent
        same-side runs so a flip that lands next to an already-flipped
        fragment yields a clean (≤3-fragment) shape.

        Algorithm: assign a side to each *segment* (a vertex pair
        ``path[k] → path[k+1]``) based on the original fragment the
        segment belongs to, then overwrite the flipped sub-range, then
        group consecutive same-side segments into Fragments.  Each
        Fragment's path is the concatenated vertices of its runs (so
        consecutive Fragments share their boundary vertex — the via).
        """
        path = list(wire.path)
        n_seg = len(path) - 1
        start, end = self.start_idx, self.end_idx
        # 1) Build the per-segment side list from existing fragments.
        seg_side: list[str] = [""] * n_seg
        cursor = 0
        for frag in wire.fragments:
            # Fragment frag contributes len(frag.path) - 1 segments.
            for k in range(len(frag.path) - 1):
                if cursor + k < n_seg:
                    seg_side[cursor + k] = frag.side
            cursor += len(frag.path) - 1

        # 2) Overwrite the flipped sub-range.  ``start..end`` is given in
        # vertex coordinates; segment ``k`` connects vertex k to k+1.
        # Flipping vertices [start..end] means flipping segments
        # [start..end-1].
        for k in range(start, end):
            if 0 <= k < n_seg:
                seg_side[k] = self.target_side

        # 3) Group consecutive same-side segments into runs.  Each run
        # spans segments [run_start..run_end]; its vertices are
        # path[run_start..run_end+1].
        new_fragments: list[Fragment] = []
        i = 0
        while i < n_seg:
            run_side = seg_side[i]
            j = i
            while j + 1 < n_seg and seg_side[j + 1] == run_side:
                j += 1
            frag_path = path[i : j + 2]
            new_fragments.append(Fragment(path=list(frag_path), side=run_side))  # type: ignore[arg-type]
            i = j + 1

        return new_fragments

    def apply(self, state: ComponentState) -> None:
        wire = state.layout.wires.get(self.wire_id)
        if wire is None:
            raise KeyError(f"Wire {self.wire_id} not found")
        # Snapshot the pre-apply fragment list for revert.  ``Wire.fragments``
        # is always populated (``Wire.__post_init__`` derives it from path if
        # absent).  Fragments are mutable, so deep-copy each (path + side) —
        # ``rebuild_fragments`` with the same sides is exactly that copy.
        self._old_fragments = rebuild_fragments(
            wire.fragments,
            [f.side for f in wire.fragments],
        )

        new_fragments = self._build_fragments_for_flip(wire)
        # Single source of truth: replace_wire_fragments preserves wire
        # id + a/b knot refs, and rebuilds wire.path from the new
        # fragments.  No new JointKnots are minted — the wire stays a
        # single artifact with its original endpoint hubs.
        state.layout.replace_wire_fragments(self.wire_id, new_fragments)

    def revert(self, state: ComponentState) -> None:
        if not self._old_fragments:
            return
        state.layout.replace_wire_fragments(self.wire_id, self._old_fragments)


@dataclass
class DisconnectPinCommand(Command[SchematicState]):
    """Remove a pin from its net. Deletes the net if it becomes empty.

    Acts on the whole same-name sibling group — disconnecting one ``GND``
    disconnects every ``GND`` of the component (they are one electrical node),
    so no sibling is left coloured on a net the pin no longer belongs to.
    Sibling expansion and revert both live in the shared schematic layer.
    """

    component_id: uuid.UUID
    pin_number: int
    _undo: PinGroupUndo | None = field(default=None, init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Disconnect pin {self.pin_number}"

    def apply(self, state: SchematicState) -> None:
        refs = same_name_refs(
            self.component_id, self.pin_number, state.instances, state.components
        )
        self._undo = state.schematic.unassign_pins(refs)

    def revert(self, state: SchematicState) -> None:
        if self._undo is not None:
            state.schematic.restore(self._undo)


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
# Knot/Fragment-aware path-edit command
# ---------------------------------------------------------------------------


@dataclass
class ReplaceWireFragmentsCommand(Command[ComponentState]):
    """Replace ``Wire.fragments`` in place — preserving ``Wire.id``.

    This is the explicit v2 counterpart of the path-edit operations
    (simplify, unweave, nudge, …) that today mutate Wire.path or
    mint fresh segments.  Identity stays stable; highlight, recording and
    undo follow.
    """

    wire_id: uuid.UUID
    new_fragments: tuple[Fragment, ...]
    # Caller-supplied pre-mutation fragments; if empty, apply() reads from state.
    # Mirrors the old_path escape hatch on ReplaceWireSegmentCommand for callers
    # (drag-commit) that mutate the wire in place before issuing the command.
    old_fragments: tuple[Fragment, ...] = field(default=())
    _saved_fragments: tuple[Fragment, ...] = field(default=(), init=False, repr=False)
    # Pre-apply positions of any free (degree-1) JointKnot endpoints, so
    # revert can slide them back to the original spot even when an
    # interleaved RemoveWireCommand later restored a sibling wire onto
    # the same joint (which would otherwise push degree above 1 and
    # cause ``replace_wire_fragments`` to refuse the slide).
    _saved_a_pos: Point | None = field(default=None, init=False, repr=False)
    _saved_b_pos: Point | None = field(default=None, init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Replace wire fragments"

    def apply(self, state: ComponentState) -> None:
        wire = state.layout.wires.get(self.wire_id)
        if wire is None:
            return
        if self.old_fragments:
            self._saved_fragments = self.old_fragments
        else:
            self._saved_fragments = tuple(wire.fragments)
        # Snapshot pre-apply JointKnot positions so revert can restore
        # them irrespective of how many wires the joint ends up holding
        # at revert time (see _saved_a_pos / _saved_b_pos docstring).
        if wire.a is not None:
            ka = state.layout.knots.get(wire.a.id)
            if isinstance(ka, JointKnot):
                self._saved_a_pos = ka.position
        if wire.b is not None:
            kb = state.layout.knots.get(wire.b.id)
            if isinstance(kb, JointKnot):
                self._saved_b_pos = kb.position
        # Pad-node enrichment for insulated wires — matches AddWireCommand
        # and load_project so PadIndex sees the same InsulatedWireCrossing
        # set whether the path arrived from autoroute, the YAML loader,
        # or a hand edit committed via [E].  Idempotent — already-enriched
        # fragments are returned unchanged.  Cache _saved_fragments BEFORE
        # the rewrite so revert restores the user's exact pre-apply state,
        # not the enriched form.
        new_fragments = _enrich_insulated_fragments_with_pad_nodes(
            self.new_fragments,
            wire.insulated,
            state.board,
        )
        state.layout.replace_wire_fragments(self.wire_id, new_fragments)

    def revert(self, state: ComponentState) -> None:
        if not self._saved_fragments:
            return
        # Restore JointKnot positions FIRST, so the subsequent
        # ``replace_wire_fragments`` call sees endpoints that match the
        # saved fragments and skips the slide-validation that would
        # otherwise reject a degree-2+ joint (the joint may have grown
        # incident-wire count between apply and revert when an
        # interleaved ``RemoveWireCommand`` was reverted before us).
        wire = state.layout.wires.get(self.wire_id)
        if wire is not None:
            if wire.a is not None and self._saved_a_pos is not None:
                ka = state.layout.knots.get(wire.a.id)
                if isinstance(ka, JointKnot):
                    state.layout.knots[ka.id] = ka.moved_to(self._saved_a_pos)
            if wire.b is not None and self._saved_b_pos is not None:
                kb = state.layout.knots.get(wire.b.id)
                if isinstance(kb, JointKnot):
                    state.layout.knots[kb.id] = kb.moved_to(self._saved_b_pos)
        # PinKnot positions resolve through ``layout._pin_resolver`` from
        # the current placement, which a sibling ``MoveComponentCommand``
        # in the same macro may not have reverted yet.  Skip PinKnot
        # endpoint validation: Move.revert later in this revert pass
        # will bring the pin back to the position our saved fragments
        # expect.  See replace_wire_fragments docstring.
        state.layout.replace_wire_fragments(
            self.wire_id,
            self._saved_fragments,
            skip_pin_knot_validation=True,
        )
