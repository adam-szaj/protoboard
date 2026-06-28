# SPDX-License-Identifier: MIT
"""GUI-level pad-conflict resolution: checks conflicts, shows dialog, updates state."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, cast

import structlog
from protoboard.domain.layout.pad_conflict import (
    ConflictKind,
    PadConflict,
    find_conflicts,
    find_wire_conflicts,
    footprint_of as _footprint_of,
)
from protoboard.domain.layout.placement import (
    ComponentPlacement,
    component_bbox,
    pin_world_position,
)
from protoboard.geometry import Point

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

    from protoboard.domain.board.pad_state import PadIndex
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.gui.session import EditorSession
    from protoboard.gui.tools.drag_commit_decision import DragCommitDecision

_log = structlog.get_logger(__name__)

# Module-level alias keeping the legacy ``_round`` callsites within
# this file working — single implementation now lives in geometry.py.
from protoboard.geometry import round_coord as _round  # noqa: E402

from dataclasses import dataclass  # noqa: E402


@dataclass(frozen=True)
class PlacementResolution:
    """Result of :func:`check_and_resolve`: the resolved placement plus any
    follow-up wire commands the resolution produced.

    Replaces the previous pattern of monkey-attaching ``_wire_cmds`` onto the
    (otherwise immutable) :class:`ComponentPlacement` — an explicit result type
    keeps the placement clean and the wire commands typed.
    """

    placement: ComponentPlacement
    wire_cmds: tuple[object, ...] = ()


def check_and_resolve(
    session: EditorSession,
    placement: ComponentPlacement,
    defn: ComponentDefinition,
    instance_id: uuid.UUID,
    parent_widget: object = None,
    instance: "ComponentInstance | None" = None,
) -> PlacementResolution | None:
    """Check pad conflicts for *placement* and resolve interactively.

    Returns:
    - Updated ComponentPlacement (with shared_pins set) if placement is allowed.
    - None if placement is blocked (different-net conflict or user declined).

    Side-effects when returning non-None:
    - Updates schematic (adds unconnected pins to nets) directly on session.

    *instance* is the (preferably bound) incoming instance. At placement time it
    is not yet in ``session.instances``, so the caller must pass it — otherwise
    the incoming geometry falls back to the definition default footprint (wrong
    pin spacing for a chosen variant). When omitted, it is looked up by id (the
    move/re-check path, where the instance is already registered).
    """
    # Body-overlap check: block if the incoming bbox intersects a placed component
    # AND no pin of the incoming component actually coincides with a pin of that
    # component (which would be a shared-pad case handled by pad-conflict below).
    #
    # Geometry comes from each component's *resolved variant* footprint (the body
    # the user actually chose), not the definition default — otherwise the bbox
    # and pin cloud use the wrong pin spacing and the overlap/coincidence test is
    # wrong (e.g. an SMD-0805 part checked against axial 3.2-hole pitch).
    incoming_instance = instance or session.instances.get(instance_id)
    in_footprint = _footprint_of(defn, incoming_instance)
    incoming_bbox = component_bbox(in_footprint, placement, defn.component_class)
    _EPS = 1e-4
    for other_id, other_pl in session.layout.placements.items():
        if other_id == instance_id:
            continue
        other_inst = session.instances.get(other_id)
        if other_inst is None:
            continue
        other_defn = session.components.get(other_inst.definition_ref)
        if other_defn is None:
            continue
        other_footprint = _footprint_of(other_defn, other_inst)
        if not incoming_bbox.intersects(component_bbox(other_footprint, other_pl, other_defn.component_class)):
            continue
        # Bboxes overlap — check whether any pin pair actually coincides.
        # If at least one pin coincides this is a shared-pad scenario: let
        # the pad-conflict logic below decide.  If no pin coincides at all,
        # it is a pure body-on-body overlap → block.
        has_pin_overlap = False
        for in_pin in range(1, in_footprint.pin_count() + 1):
            in_pos = pin_world_position(in_footprint, placement, in_pin)
            for ot_pin in range(1, other_footprint.pin_count() + 1):
                ot_pos = pin_world_position(other_footprint, other_pl, ot_pin)
                if abs(in_pos.x - ot_pos.x) < _EPS and abs(in_pos.y - ot_pos.y) < _EPS:
                    has_pin_overlap = True
                    break
            if has_pin_overlap:
                break
        if not has_pin_overlap:
            _log.debug("conflict.body_overlap_blocked", incoming=str(instance_id))
            return None

    # PadIndex is the authoritative pad-state source.  For single-component
    # placement we can use the session's index directly: pins of `instance_id`
    # at their *old* positions are excluded by id when computing conflicts,
    # and the new pin positions are looked up against pads that other
    # components already occupy.  Test stubs without a ``pad_index`` method
    # fall back to the legacy from-layout path.
    pad_index = session.pad_index() if hasattr(session, "pad_index") else None

    pad_conflicts = find_conflicts(
        incoming_placement=placement,
        incoming_defn=defn,
        incoming_instance_id=instance_id,
        layout_placements=session.layout.placements,
        instances=session.instances,
        components=session.components,
        schematic=session.schematic,
        pad_index=pad_index,
        incoming_instance=incoming_instance,
    )

    # Build point→net map for wire-conflict detection.
    # Maps rounded (x, y) of any placed pin that has a net → (net_id, net_name).
    point_to_net: dict[tuple[float, float], tuple[uuid.UUID, str]] = {}
    for net in session.schematic.nets.values():
        for ref in net.pins:
            p = session.get_pin_position(ref.component_id, ref.pin_number)
            if p is None:
                continue
            point_to_net[(_round(p.x), _round(p.y))] = (net.id, net.name)

    already_conflicted = {
        (_round(c.position.x), _round(c.position.y)) for c in pad_conflicts
    }
    wire_conflicts = find_wire_conflicts(
        incoming_placement=placement,
        incoming_defn=defn,
        incoming_instance_id=instance_id,
        wires=session.layout.wires.values(),
        schematic=session.schematic,
        point_to_net=point_to_net,
        already_conflicted=already_conflicted,
        pad_index=pad_index,
        incoming_instance=incoming_instance,
    )

    # --- Separate pad conflicts from wire conflicts ---
    _WIRE_KINDS = {
        ConflictKind.PIN_ON_WIRE_DIFFERENT_NET,
        ConflictKind.PIN_ON_WIRE_SAME_NET,
        ConflictKind.PIN_ON_WIRE_WITH_NET,
        ConflictKind.PIN_ON_WIRE_UNCONNECTED,
    }
    pure_pad = [c for c in pad_conflicts if c.kind not in _WIRE_KINDS]
    wire_only = wire_conflicts  # all wire_conflicts are already wire-kind

    # --- Pad-on-pad conflict resolution (unchanged logic) ---
    _PAD_BLOCKED = {ConflictKind.DIFFERENT_NETS}
    _PAD_AUTO_OK = {ConflictKind.SAME_NET}
    _PAD_DECISION = {ConflictKind.ONE_UNCONNECTED, ConflictKind.BOTH_UNCONNECTED}

    pad_blocked = [c for c in pure_pad if c.kind in _PAD_BLOCKED]
    pad_auto_ok = [c for c in pure_pad if c.kind in _PAD_AUTO_OK]
    pad_decision = [c for c in pure_pad if c.kind in _PAD_DECISION]

    if pad_blocked:
        _log.info("conflict.pad_blocked", kinds=[c.kind.name for c in pad_blocked])
        _show_dialog(session, pad_blocked + pad_decision, placement, defn, instance_id, parent_widget)
        return None

    if pad_decision:
        accepted = _show_dialog(
            session, pure_pad, placement, defn, instance_id, parent_widget
        )
        if not accepted:
            return None

    new_shared = set(placement.shared_pins)
    for c in pad_auto_ok + pad_decision:
        new_shared.add(c.incoming_pin)
    if pad_decision:
        _update_schematic(session, pad_decision, placement, defn, instance_id)

    # --- Wire conflict resolution (new: dialog with choices) ---
    wire_action_cmds: list[object] = []
    if wire_only:
        _wire_same = [c for c in wire_only if c.kind == ConflictKind.PIN_ON_WIRE_SAME_NET]
        _wire_needs = [c for c in wire_only if c.kind != ConflictKind.PIN_ON_WIRE_SAME_NET]

        if _wire_needs:
            result_pl, wire_action_cmds = _resolve_wire_conflicts(
                session=session,
                wire_conflicts=_wire_needs,
                placement=placement,
                defn=defn,
                instance_id=instance_id,
                parent_widget=parent_widget,
            )
            if result_pl is None:
                # Revert shared_pins already applied to pad conflicts.
                return None
            placement = result_pl

    result = placement.copy()
    result.shared_pins = frozenset(new_shared)
    return PlacementResolution(
        placement=result, wire_cmds=tuple(wire_action_cmds)
    )


def _show_dialog(
    session: EditorSession,
    conflicts: list[PadConflict],
    placement: ComponentPlacement,  # noqa: ARG001
    defn: ComponentDefinition,
    instance_id: uuid.UUID,
    parent_widget: object,
) -> bool:
    """Show the conflict dialog. Returns True if user accepted."""
    from PySide6.QtWidgets import QDialog

    from protoboard.gui.dialogs.pad_conflict_dialog import PadConflictDialog

    # Build refdes map: conflict.existing_comp_id → refdes string.
    existing_refdes: dict[int, str] = {}
    for c in conflicts:
        inst = session.instances.get(c.existing_comp_id)
        existing_refdes[id(c)] = inst.refdes if inst else "?"

    inst = session.instances.get(instance_id)
    incoming_refdes = inst.refdes if inst else defn.name

    dlg = PadConflictDialog(
        conflicts=conflicts,
        incoming_refdes=incoming_refdes,
        existing_refdes_map=existing_refdes,
        parent=parent_widget,
    )

    # Same-net-only: auto-accept without showing dialog.
    if all(c.kind == ConflictKind.SAME_NET for c in conflicts):
        return True

    return dlg.exec() == QDialog.DialogCode.Accepted


def _update_schematic(
    session: EditorSession,
    conflicts: list[PadConflict],
    placement: ComponentPlacement,  # noqa: ARG001
    defn: ComponentDefinition,  # noqa: ARG001
    instance_id: uuid.UUID,
) -> None:
    """Add unconnected pins to nets after conflicts are resolved."""
    import uuid as _uuid

    from protoboard.domain.schematic.net import Net, PinRef

    _PAD_KINDS = {ConflictKind.ONE_UNCONNECTED, ConflictKind.BOTH_UNCONNECTED}
    _WIRE_KINDS = {
        ConflictKind.PIN_ON_WIRE_UNCONNECTED, ConflictKind.PIN_ON_WIRE_WITH_NET
    }

    for c in conflicts:
        incoming_ref = PinRef(component_id=instance_id, pin_number=c.incoming_pin)

        if c.kind in _PAD_KINDS:
            existing_ref = PinRef(
                component_id=c.existing_comp_id, pin_number=c.existing_pin
            )
            if c.existing_net_id is not None:
                session.schematic.add_pin_to_net(incoming_ref, c.existing_net_id)
            elif c.incoming_net_id is not None:
                session.schematic.add_pin_to_net(existing_ref, c.incoming_net_id)
            else:
                inst = session.instances.get(instance_id)
                new_net_id = _uuid.uuid4()
                net_name = f"Net-({inst.refdes if inst else '?'}-{c.incoming_pin})"
                session.schematic.nets[new_net_id] = Net(
                    id=new_net_id,
                    name=net_name,
                    pins={incoming_ref, existing_ref},
                )
                _log.info("conflict.net_created", net_name=net_name, kind=c.kind.name)

        elif c.kind in _WIRE_KINDS:
            if c.existing_net_id is not None:
                session.schematic.add_pin_to_net(incoming_ref, c.existing_net_id)
            else:
                inst = session.instances.get(instance_id)
                new_net_id = _uuid.uuid4()
                session.schematic.nets[new_net_id] = Net(
                    id=new_net_id,
                    name=f"Net-({inst.refdes if inst else '?'}-{c.incoming_pin})",
                    pins={incoming_ref},
                )


def _resolve_wire_conflicts(
    session: "EditorSession",
    wire_conflicts: list[PadConflict],
    placement: ComponentPlacement,
    defn: "ComponentDefinition",
    instance_id: uuid.UUID,
    parent_widget: object,
) -> tuple[ComponentPlacement | None, list[object]]:
    """Show WireConflictDialog and build commands for chosen action.

    Returns (resolved_placement, extra_commands) or (None, []) on cancel.
    """
    import uuid as _uuid
    from protoboard.domain.schematic.net import Net, PinRef
    from protoboard.gui.dialogs.wire_conflict_dialog import (
        WireConflictAction,
        WireConflictDialog,
    )
    from PySide6.QtWidgets import QDialog

    inst = session.instances.get(instance_id)
    incoming_refdes = inst.refdes if inst else defn.name

    dlg = WireConflictDialog(
        conflicts=wire_conflicts,
        incoming_refdes=incoming_refdes,
        parent=cast("QWidget | None", parent_widget),
    )
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None, []

    action = dlg.chosen_action()
    cmds: list[object] = []

    if action == WireConflictAction.CANCEL:
        return None, []

    if action == WireConflictAction.REMOVE_AND_REROUTE:
        # Collect unique wire IDs to remove.
        from protoboard.domain.commands.component_cmds import RemoveWireCommand
        seen: set[uuid.UUID] = set()
        for c in wire_conflicts:
            if c.wire_id is not None and c.wire_id not in seen:
                seen.add(c.wire_id)
                wire_obj = next(
                    (w for w in session.layout.wires if w.id == c.wire_id), None
                )
                cmds.append(RemoveWireCommand(wire_id=c.wire_id))
                if wire_obj is not None:
                    reroute_cmd = _try_reroute_wire(session, wire_obj, placement)
                    if reroute_cmd is not None:
                        cmds.append(reroute_cmd)
        return placement, cmds

    if action == WireConflictAction.ADD_TO_NET:
        for c in wire_conflicts:
            ref = PinRef(component_id=instance_id, pin_number=c.incoming_pin)
            if c.existing_net_id is not None:
                session.schematic.add_pin_to_net(ref, c.existing_net_id)
        return placement, []

    if action == WireConflictAction.CREATE_NET:
        for c in wire_conflicts:
            ref = PinRef(component_id=instance_id, pin_number=c.incoming_pin)
            new_net_id = _uuid.uuid4()
            net_name = f"Net-({incoming_refdes}-{c.incoming_pin})"
            session.schematic.nets[new_net_id] = Net(
                id=new_net_id, name=net_name, pins={ref}
            )
        return placement, []

    return None, []


def _try_reroute_wire(
    session: "EditorSession",
    wire: object,
    blocker_placement: ComponentPlacement,
) -> object | None:
    """Attempt A* reroute for *wire* after its removal.

    Returns an AddWireWithNetsCommand if a path is found, else None.
    """
    from protoboard.domain.layout.wire import Wire
    from protoboard.services.routing.astar import find_paths
    from protoboard.services.routing.cost_model import RoutingCostModel

    if not isinstance(wire, Wire) or len(wire.path) < 2:
        return None

    source = wire.path[0]
    target = wire.path[-1]

    graph = session.connectivity_graph()
    results = find_paths(
        graph=graph,
        source=source,
        targets=[target],
        side=wire.side,
        cost_model=RoutingCostModel(),
        k=1,
        insulated=wire.insulated,
    )
    if not results:
        return None

    best = results[0]
    from protoboard.domain.layout.wire import Wire as WS
    import uuid as _uuid
    new_wire = WS(
        id=_uuid.uuid4(),
        path=[pt for pt, _side in best.path],
        insulated=wire.insulated,
        side=wire.side,  # type: ignore[arg-type]
    )
    from protoboard.domain.commands.component_cmds import AddWireCommand
    return AddWireCommand(wire=new_wire)


# ---------------------------------------------------------------------------
# Drag-commit classifier (Qt-free) — drives floating-overlay UX in SelectTool
# ---------------------------------------------------------------------------

def build_wire_removal_cmds(
    wire_ids: "set[uuid.UUID] | frozenset[uuid.UUID]",
) -> list[object]:
    """Build RemoveWireCommand instances for *wire_ids*.

    Extracted from ``_resolve_wire_conflicts`` so the drag pipeline can
    auto-remove wires without going through the dialog.  Reroute is *not*
    attempted here — the user-confirmed policy for drag commits is "remove
    silently with toast + Ctrl+Z".  Callers wanting reroute can build
    `_try_reroute_wire` follow-ups separately.
    """
    from protoboard.domain.commands.component_cmds import RemoveWireCommand

    cmds: list[object] = []
    seen: set[uuid.UUID] = set()
    for wid in wire_ids:
        if wid in seen:
            continue
        seen.add(wid)
        cmds.append(RemoveWireCommand(wire_id=wid))
    return cmds


def classify_drag_commit(
    session: "EditorSession",
    moved_comp_ids: "set[uuid.UUID]",
    dragged_wire_ids: "set[uuid.UUID] | frozenset[uuid.UUID]",
    drag_set_members: "frozenset[uuid.UUID] | None" = None,
) -> "DragCommitDecision":
    """Classify the current (post-drag) layout state for commit.

    Thin wrapper around :class:`DragDetector`. Builds a
    :class:`DragSnapshot` from *session* (including a fresh PadIndex
    when ``session.board`` is available) and delegates classification
    to the detector configured with a :class:`DRCEnforcingStrategy`.

    ``drag_set_members`` (optional, unified drag model 2026-06-02): the
    full set of components moving atomically by one delta — single
    component, multi-selection, or recursive group membership.  When
    supplied, the detector emits a
    :class:`DragWireClassification` and partitions removable wires
    into free / stationary-group / cross-group-disconnect buckets.
    When None (legacy callers), the detector falls back to the
    original single-bucket auto-remove path.

    Pure logic — never constructs a Qt dialog.
    """
    from protoboard.domain.layout.group_tree import GroupTree
    from protoboard.services.drag_detector import DragDetector, DragSnapshot
    from protoboard.services.pad_strategy import DRCEnforcingStrategy

    pad_index = _build_drag_pad_index(session)
    drag_set = drag_set_members or frozenset()

    # Resolve member_groups / stationary_locked_groups from the live
    # session layout.  member_groups overlap drag_set; locked groups
    # are the rest with lock_internal_wires=True.
    member_groups: list[object] = []
    locked_groups: list[object] = []
    layout_groups = getattr(session.layout, "groups", {})
    _tree = GroupTree(layout_groups)
    for gid, group in layout_groups.items():
        gcids = _tree.all_component_ids(gid)
        if gcids & drag_set:
            member_groups.append(group)
        elif getattr(group, "lock_internal_wires", False):
            locked_groups.append(group)

    wire_net_map: dict[uuid.UUID, uuid.UUID] = {}
    if drag_set:
        # Best-effort: session may not expose the helper in test stubs.
        getter = getattr(session, "cached_wire_net_map", None)
        if callable(getter):
            try:
                wire_net_map = dict(getter())
            except Exception:  # noqa: BLE001
                wire_net_map = {}

    snapshot = DragSnapshot(
        pad_index=pad_index,
        placements=session.layout.placements,
        instances=session.instances,
        components=session.components,
        schematic=session.schematic,
        wires=list(session.layout.wires),
        drag_set_members=drag_set,
        wire_net_map=wire_net_map,
        knots=dict(session.layout.knots) if drag_set else {},
        member_groups=member_groups,  # type: ignore[arg-type]
        stationary_locked_groups=locked_groups,  # type: ignore[arg-type]
    )
    detector = DragDetector(DRCEnforcingStrategy())
    return detector.classify(
        snapshot,
        moved_comp_ids=set(moved_comp_ids),
        dragged_wire_ids=frozenset(dragged_wire_ids),
    )


def _build_drag_pad_index(session: "EditorSession") -> "PadIndex | None":
    """Build a fresh PadIndex reflecting the live (post-drag) layout.

    Returns None when *session* lacks a ``board`` (test stubs) or
    construction fails — callers fall back to legacy from-layout paths.
    """
    if not hasattr(session, "board"):
        return None
    try:
        from protoboard.domain.board.pad_state import PadIndex
        return PadIndex(
            board=session.board,
            layout=session.layout,
            schematic=session.schematic,
            components=session.components,
            instances=session.instances,
        )
    except Exception:  # noqa: BLE001
        return None
