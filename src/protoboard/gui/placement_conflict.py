# SPDX-License-Identifier: MIT
"""GUI-level pad-conflict resolution: checks conflicts, shows dialog, updates state."""

from __future__ import annotations

import dataclasses
import uuid
from typing import TYPE_CHECKING

from protoboard.domain.layout.pad_conflict import (
    ConflictKind,
    PadConflict,
    find_conflicts,
)
from protoboard.domain.layout.placement import (
    ComponentPlacement,
    component_bbox,
    pin_world_position,
)

if TYPE_CHECKING:
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.gui.session import EditorSession


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
    incoming_bbox = component_bbox(defn.footprint, placement)
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
        if not incoming_bbox.intersects(component_bbox(other_defn.footprint, other_pl)):
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
            return None

    conflicts = find_conflicts(
        incoming_placement=placement,
        incoming_defn=defn,
        incoming_instance_id=instance_id,
        layout_placements=session.layout.placements,
        instances=session.instances,
        components=session.components,
        schematic=session.schematic,
    )

    if not conflicts:
        return placement

    # Check for hard blocks (different nets).
    blocked = [c for c in conflicts if c.kind == ConflictKind.DIFFERENT_NETS]
    auto_ok = [c for c in conflicts if c.kind == ConflictKind.SAME_NET]
    needs_decision = [
        c for c in conflicts
        if c.kind in (ConflictKind.ONE_UNCONNECTED, ConflictKind.BOTH_UNCONNECTED)
    ]

    if blocked:
        # Show blocking dialog (no accept button).
        _show_dialog(session, conflicts, placement, defn, instance_id, parent_widget)
        return None

    # All conflicts are resolvable — show dialog only if user input needed.
    if needs_decision:
        accepted = _show_dialog(
            session, conflicts, placement, defn, instance_id, parent_widget
        )
        if not accepted:
            return None

    # Build updated placement with shared_pins.
    resolvable = auto_ok + needs_decision
    new_shared = set(placement.shared_pins)
    for c in resolvable:
        new_shared.add(c.incoming_pin)

    # Update schematic: connect unconnected pins to existing/new nets.
    if needs_decision:
        _update_schematic(session, conflicts, placement, defn, instance_id)

    return dataclasses.replace(placement, shared_pins=frozenset(new_shared))


def _show_dialog(
    session: EditorSession,
    conflicts: list[PadConflict],
    placement: ComponentPlacement,
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
    placement: ComponentPlacement,
    defn: ComponentDefinition,
    instance_id: uuid.UUID,
) -> None:
    """Add unconnected pins to nets after user accepted sharing."""
    import uuid as _uuid

    from protoboard.domain.schematic.net import Net, PinRef

    for c in conflicts:
        if c.kind not in (ConflictKind.ONE_UNCONNECTED, ConflictKind.BOTH_UNCONNECTED):
            continue

        incoming_ref = PinRef(component_id=instance_id, pin_number=c.incoming_pin)
        existing_ref = PinRef(
            component_id=c.existing_comp_id, pin_number=c.existing_pin
        )

        if c.existing_net_id is not None:
            # Add incoming pin to existing net.
            net = session.schematic.nets[c.existing_net_id]
            session.schematic.nets[c.existing_net_id] = Net(
                id=net.id, name=net.name, pins=net.pins | {incoming_ref}
            )
        elif c.incoming_net_id is not None:
            # Add existing pin to incoming's net.
            net = session.schematic.nets[c.incoming_net_id]
            session.schematic.nets[c.incoming_net_id] = Net(
                id=net.id, name=net.name, pins=net.pins | {existing_ref}
            )
        else:
            # Both unconnected — create new net.
            inst = session.instances.get(instance_id)
            name = f"Net-({inst.refdes if inst else '?'}-{c.incoming_pin})"
            new_net_id = _uuid.uuid4()
            session.schematic.nets[new_net_id] = Net(
                id=new_net_id,
                name=name,
                pins={incoming_ref, existing_ref},
            )
