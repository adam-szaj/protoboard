# SPDX-License-Identifier: MIT
"""GUI-level pad-conflict resolution: checks conflicts, shows dialog, updates state."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import structlog
from protoboard.domain.layout.pad_conflict import (
    ConflictKind,
    PadConflict,
    find_conflicts,
    find_wire_conflicts,
)
from protoboard.domain.layout.placement import (
    ComponentPlacement,
    component_bbox,
    pin_world_position,
)
from protoboard.geometry import Point

if TYPE_CHECKING:
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.gui.session import EditorSession
    from protoboard.gui.tools.drag_commit_decision import DragCommitDecision

_log = structlog.get_logger(__name__)


def _round(v: float) -> float:
    return round(v, 4)


def check_and_resolve(
    session: EditorSession,
    placement: ComponentPlacement,
    defn: ComponentDefinition,
    instance_id: uuid.UUID,
    parent_widget: object = None,
) -> ComponentPlacement | None:
    """Check pad conflicts for *placement* and resolve interactively.

    Returns:
    - Updated ComponentPlacement (with shared_pins set) if placement is allowed.
    - None if placement is blocked (different-net conflict or user declined).

    Side-effects when returning non-None:
    - Updates schematic (adds unconnected pins to nets) directly on session.
    """
    # Body-overlap check: block if the incoming bbox intersects a placed component
    # AND no pin of the incoming component actually coincides with a pin of that
    # component (which would be a shared-pad case handled by pad-conflict below).
    incoming_bbox = component_bbox(defn.footprint, placement, defn.component_class)
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
        if not incoming_bbox.intersects(component_bbox(other_defn.footprint, other_pl, other_defn.component_class)):
            continue
        # Bboxes overlap — check whether any pin pair actually coincides.
        # If at least one pin coincides this is a shared-pad scenario: let
        # the pad-conflict logic below decide.  If no pin coincides at all,
        # it is a pure body-on-body overlap → block.
        has_pin_overlap = False
        for in_pin in range(1, defn.footprint.pin_count() + 1):
            in_pos = pin_world_position(defn.footprint, placement, in_pin)
            for ot_pin in range(1, other_defn.footprint.pin_count() + 1):
                ot_pos = pin_world_position(other_defn.footprint, other_pl, ot_pin)
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
        wires=session.layout.wires,
        schematic=session.schematic,
        point_to_net=point_to_net,
        already_conflicted=already_conflicted,
        pad_index=pad_index,
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
    # Attach extra wire commands as an attribute so callers can retrieve them.
    result._wire_cmds = wire_action_cmds  # type: ignore[attr-defined]
    return result


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
                session.schematic.nets[c.existing_net_id].pins.add(incoming_ref)
            elif c.incoming_net_id is not None:
                session.schematic.nets[c.incoming_net_id].pins.add(existing_ref)
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
                session.schematic.nets[c.existing_net_id].pins.add(incoming_ref)
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
        parent=parent_widget,
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
                session.schematic.nets[c.existing_net_id].pins.add(ref)
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
) -> "DragCommitDecision":
    """Classify the current (post-drag) layout state for commit.

    Pure logic — never constructs a Qt dialog.  Returns a
    :class:`protoboard.gui.tools.drag_commit_decision.DragCommitDecision`.

    The classifier inspects ``session.layout`` as it stands *now* (the live
    drag has already updated placements and wires in place).  It considers,
    in worst-of order:

    * pure body-on-body overlap with a non-moved component → HARD
    * DIFFERENT_NETS pad-on-pad with a non-moved component → HARD
    * PIN_ON_WIRE_* / ONE_UNCONNECTED / BOTH_UNCONNECTED → SOFT
    * wires now crossing alien pads (``find_wires_crossing_pads``) → SOFT
    * wires whose path became skewed after drag (``find_skewed_wires``) → SOFT
    * nothing → CLEAR
    """
    from protoboard.domain.layout.pad_conflict import (
        ConflictKind,
        find_wire_conflicts,
        find_wires_crossing_pads,
    )
    from protoboard.domain.layout.wire_utils import find_skewed_wires
    from protoboard.gui.tools.drag_commit_decision import (
        DragCommitDecision,
        DragSeverity,
    )

    hard_pads: list[Point] = []
    soft_pads: list[Point] = []
    remove_wires: set[uuid.UUID] = set()
    net_actions: list[object] = []
    descriptions: list[str] = []

    # Lazy-built PadIndex over the current (post-drag) layout.  Reused
    # across every moved component + wire-crossing check.
    _drag_pad_index: object | None = None

    # --- 1. Per-moved-component pad + body conflicts ---
    pad_conflicts_all: list[PadConflict] = []
    for cid in moved_comp_ids:
        pl = session.layout.placements.get(cid)
        inst = session.instances.get(cid)
        if pl is None or inst is None:
            continue
        defn = session.components.get(inst.definition_ref)
        if defn is None:
            continue

        # Pure body-overlap (mirrors _move_blocked / check_and_resolve preamble).
        incoming_bbox = component_bbox(
            defn.footprint, pl, defn.component_class
        )
        body_overlap_refdes: str | None = None
        for other_id, other_pl in session.layout.placements.items():
            if other_id == cid or other_id in moved_comp_ids:
                continue
            other_inst = session.instances.get(other_id)
            if other_inst is None:
                continue
            other_defn = session.components.get(other_inst.definition_ref)
            if other_defn is None:
                continue
            other_bbox = component_bbox(
                other_defn.footprint, other_pl, other_defn.component_class
            )
            if not incoming_bbox.intersects(other_bbox):
                continue
            # If any pin coincides, it's a shared-pad case (handled below).
            has_pin_overlap = False
            for in_pin in range(1, defn.footprint.pin_count() + 1):
                in_pos = pin_world_position(defn.footprint, pl, in_pin)
                for ot_pin in range(1, other_defn.footprint.pin_count() + 1):
                    ot_pos = pin_world_position(
                        other_defn.footprint, other_pl, ot_pin
                    )
                    if abs(in_pos.x - ot_pos.x) < 1e-4 and abs(in_pos.y - ot_pos.y) < 1e-4:
                        has_pin_overlap = True
                        break
                if has_pin_overlap:
                    break
            if not has_pin_overlap:
                body_overlap_refdes = other_inst.refdes
                # Use pin 1 as positional marker.
                hard_pads.append(pin_world_position(defn.footprint, pl, 1))

        if body_overlap_refdes is not None:
            descriptions.append(
                f"{inst.refdes} body overlap with {body_overlap_refdes}"
            )

        # Pad-on-pad conflicts.  We rebuild a fresh PadIndex over the
        # *current* (post-drag) layout when the session exposes a board:
        # the drag handler mutates placements/wires in-place and emits
        # ``drag_moved`` only, not ``layout_changed``, so the cached index
        # is stale w.r.t. the live drag state.  Build is ~3 ms per call
        # (memory: perf.pad_index_build_ms).  Test stubs without a ``board``
        # attribute fall back to the legacy from-layout path.
        if _drag_pad_index is None and hasattr(session, "board"):
            try:
                from protoboard.domain.board.pad_state import PadIndex
                _drag_pad_index = PadIndex(
                    board=session.board,
                    layout=session.layout,
                    schematic=session.schematic,
                    components=session.components,
                    instances=session.instances,
                )
            except Exception:  # noqa: BLE001
                _drag_pad_index = None
        pad_cs = find_conflicts(
            incoming_placement=pl,
            incoming_defn=defn,
            incoming_instance_id=cid,
            layout_placements=session.layout.placements,
            instances=session.instances,
            components=session.components,
            schematic=session.schematic,
            pad_index=_drag_pad_index,
        )
        pad_conflicts_all.extend(pad_cs)
        for c in pad_cs:
            if c.kind == ConflictKind.DIFFERENT_NETS:
                hard_pads.append(c.position)
                descriptions.append(
                    f"{inst.refdes} pin {c.incoming_pin}: different-net conflict"
                )
            elif c.kind in (
                ConflictKind.ONE_UNCONNECTED,
                ConflictKind.BOTH_UNCONNECTED,
            ):
                soft_pads.append(c.position)
                descriptions.append(
                    f"{inst.refdes} pin {c.incoming_pin}: shared pad (will join net)"
                )

        # Wire conflicts (pin on alien wire).
        point_to_net: dict[tuple[float, float], tuple[uuid.UUID, str]] = {}
        for net in session.schematic.nets.values():
            for ref in net.pins:
                p = session.get_pin_position(ref.component_id, ref.pin_number)
                if p is None:
                    continue
                point_to_net[(_round(p.x), _round(p.y))] = (net.id, net.name)
        already_conflicted = {
            (_round(c.position.x), _round(c.position.y)) for c in pad_cs
        }
        wire_cs = find_wire_conflicts(
            incoming_placement=pl,
            incoming_defn=defn,
            incoming_instance_id=cid,
            wires=session.layout.wires,
            schematic=session.schematic,
            point_to_net=point_to_net,
            already_conflicted=already_conflicted,
            pad_index=_drag_pad_index,
        )
        # Rule 6 strict: ANY wire on a pin pad is a conflict — even
        # the same-net case ("intentional contact") is physically
        # awkward (pin sterczy nad padem) and gets removed on commit.
        # The user can re-route the wire to a separate hub afterwards.
        for c in wire_cs:
            soft_pads.append(c.position)
            if c.wire_id is not None:
                remove_wires.add(c.wire_id)
            if c.kind == ConflictKind.PIN_ON_WIRE_SAME_NET:
                descriptions.append(
                    f"{inst.refdes} pin {c.incoming_pin} on same-net wire "
                    f"(will remove — re-route through a separate hub)"
                )
            else:
                descriptions.append(
                    f"{inst.refdes} pin {c.incoming_pin} on foreign wire "
                    f"(will remove)"
                )

    # --- 2. Wires crossing alien pads after the move ---
    crossing = find_wires_crossing_pads(
        wires=session.layout.wires,
        moved_comp_ids=moved_comp_ids,
        layout_placements=session.layout.placements,
        instances=session.instances,
        components=session.components,
        schematic=session.schematic,
        moved_wire_ids=set(dragged_wire_ids),
        pad_index=_drag_pad_index,
    )
    if crossing:
        remove_wires.update(crossing)
        descriptions.append(
            f"{len(crossing)} wire(s) cross alien pads (will remove)"
        )

    # --- 3. Skewed wires (only inspect ones that were dragged) ---
    skewed = find_skewed_wires(
        session.layout.wires, moved_wire_ids=set(dragged_wire_ids)
    )
    if skewed:
        remove_wires.update(skewed)
        descriptions.append(
            f"{len(skewed)} wire(s) became skewed (will remove)"
        )

    # --- 4. Determine severity ---
    if hard_pads:
        severity = DragSeverity.HARD
    elif soft_pads or remove_wires:
        severity = DragSeverity.SOFT
    else:
        severity = DragSeverity.CLEAR

    _log.debug(
        "drag.classify_commit",
        severity=severity.value,
        moved=[str(c) for c in moved_comp_ids],
        dragged_wires=[str(w) for w in dragged_wire_ids],
        auto_remove_wires=[str(w) for w in remove_wires],
        hard_pads=[(p.x, p.y) for p in hard_pads],
        soft_pads=[(p.x, p.y) for p in soft_pads],
        descriptions=descriptions,
    )

    return DragCommitDecision(
        severity=severity,
        description="; ".join(descriptions),
        auto_remove_wire_ids=tuple(remove_wires),
        auto_net_actions=tuple(net_actions),
        blocking_pad_positions=tuple(hard_pads),
        overlay_pad_positions=tuple(soft_pads),
    )
