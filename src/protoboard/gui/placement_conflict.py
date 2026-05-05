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

if TYPE_CHECKING:
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.gui.session import EditorSession

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

    pad_conflicts = find_conflicts(
        incoming_placement=placement,
        incoming_defn=defn,
        incoming_instance_id=instance_id,
        layout_placements=session.layout.placements,
        instances=session.instances,
        components=session.components,
        schematic=session.schematic,
    )

    # Build point→net map for wire-conflict detection.
    # Maps rounded (x, y) of any placed pin that has a net → (net_id, net_name).
    point_to_net: dict[tuple[float, float], tuple[uuid.UUID, str]] = {}
    for net in session.schematic.nets.values():
        for ref in net.pins:
            ref_pl = session.layout.placements.get(ref.component_id)
            ref_inst = session.instances.get(ref.component_id)
            if ref_pl is None or ref_inst is None:
                continue
            ref_defn = session.components.get(ref_inst.definition_ref)
            if ref_defn is None:
                continue
            p = pin_world_position(ref_defn.footprint, ref_pl, ref.pin_number)
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
    )

    conflicts = pad_conflicts + wire_conflicts

    if not conflicts:
        return placement

    # Check for hard blocks (different nets).
    _BLOCKED_KINDS = {
        ConflictKind.DIFFERENT_NETS, ConflictKind.PIN_ON_WIRE_DIFFERENT_NET
    }
    _AUTO_OK_KINDS = {ConflictKind.SAME_NET, ConflictKind.PIN_ON_WIRE_SAME_NET}
    _AUTO_CREATE_KINDS = {
        ConflictKind.PIN_ON_WIRE_UNCONNECTED, ConflictKind.PIN_ON_WIRE_WITH_NET
    }

    blocked = [c for c in conflicts if c.kind in _BLOCKED_KINDS]
    auto_ok = [c for c in conflicts if c.kind in _AUTO_OK_KINDS]
    needs_decision = [
        c for c in conflicts
        if c.kind in (ConflictKind.ONE_UNCONNECTED, ConflictKind.BOTH_UNCONNECTED)
    ]
    auto_wire = [c for c in conflicts if c.kind in _AUTO_CREATE_KINDS]

    if blocked:
        _log.info("conflict.blocked", kinds=[c.kind.name for c in blocked], incoming=str(instance_id))
        # Show blocking dialog (no accept button).
        _show_dialog(session, conflicts, placement, defn, instance_id, parent_widget)
        return None

    # Conflicts that need explicit user confirmation (pad-on-pad with unconnected pins).
    if needs_decision:
        accepted = _show_dialog(
            session, conflicts, placement, defn, instance_id, parent_widget
        )
        if not accepted:
            return None

    # Build updated placement with shared_pins (pad-on-pad only).
    resolvable_pads = auto_ok + needs_decision
    new_shared = set(placement.shared_pins)
    for c in resolvable_pads:
        new_shared.add(c.incoming_pin)

    # Update schematic for all resolved conflicts.
    all_resolved = needs_decision + auto_wire
    if all_resolved:
        _update_schematic(session, all_resolved, placement, defn, instance_id)

    result = placement.copy()
    result.shared_pins = frozenset(new_shared)
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
