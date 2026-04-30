# SPDX-License-Identifier: MIT
"""GUI-level wire-to-net resolver: classifies endpoints, mutates schematic, records
changes into the command for undo/redo support."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.wire_net_ops import (
    WireNetKind,
    classify_wire_nets,
)

if TYPE_CHECKING:
    from protoboard.domain.commands.component_cmds import AddWireWithNetsCommand
    from protoboard.domain.layout.wire import WireSegment
    from protoboard.domain.schematic.wire_net_ops import WireNetClassification
    from protoboard.gui.session import EditorSession


def resolve_wire_nets(
    session: EditorSession,
    wire: WireSegment,
    cmd: AddWireWithNetsCommand,
    parent_widget: object = None,
) -> bool:
    """Classify wire endpoints and update session.schematic accordingly.

    Mutates session.schematic.nets and records every change into *cmd* so
    that undo/redo work correctly.

    Returns True if the wire should be committed, False if the user cancelled
    (only possible for DIFFERENT_NETS when the merge dialog is rejected).
    """
    cl = classify_wire_nets(wire, session)

    if cl.kind == WireNetKind.NO_PINS:
        return True

    if cl.kind == WireNetKind.SAME_NET:
        return True

    if cl.kind == WireNetKind.BOTH_UNCONNECTED:
        _create_new_net(session, cmd, cl)
        return True

    if cl.kind == WireNetKind.ONE_UNCONNECTED:
        _extend_net(session, cmd, cl)
        return True

    # DIFFERENT_NETS — show merge dialog
    assert cl.kind == WireNetKind.DIFFERENT_NETS
    assert cl.net_a is not None and cl.net_b is not None
    return _merge_nets(session, cmd, cl.net_a, cl.net_b, parent_widget)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _create_new_net(
    session: EditorSession,
    cmd: AddWireWithNetsCommand,
    cl: WireNetClassification,
) -> None:
    assert cl.pin_a is not None and cl.pin_b is not None
    inst = session.instances.get(cl.pin_a[0])
    refdes = inst.refdes if inst else "?"
    name = f"Net-({refdes}-{cl.pin_a[1]})"
    new_id = uuid.uuid4()
    new_net = Net(
        id=new_id,
        name=name,
        pins={
            PinRef(component_id=cl.pin_a[0], pin_number=cl.pin_a[1]),
            PinRef(component_id=cl.pin_b[0], pin_number=cl.pin_b[1]),
        },
    )
    cmd.record_net_change(new_id, before=None, after=new_net)
    session.schematic.nets[new_id] = new_net


def _extend_net(
    session: EditorSession,
    cmd: AddWireWithNetsCommand,
    cl: WireNetClassification,
) -> None:
    assert cl.pin_a is not None and cl.pin_b is not None
    # Identify which side has the net and which is unconnected.
    if cl.net_a is not None:
        net = cl.net_a
        unconnected = cl.pin_b
    else:
        assert cl.net_b is not None
        net = cl.net_b
        unconnected = cl.pin_a
    new_ref = PinRef(component_id=unconnected[0], pin_number=unconnected[1])
    updated = Net(id=net.id, name=net.name, pins=net.pins | {new_ref})
    cmd.record_net_change(net.id, before=net, after=updated)
    session.schematic.nets[net.id] = updated


def _merge_nets(
    session: EditorSession,
    cmd: AddWireWithNetsCommand,
    net_a: Net,
    net_b: Net,
    parent_widget: object,
) -> bool:
    from PySide6.QtWidgets import QDialog

    from protoboard.gui.dialogs.net_merge_dialog import NetMergeDialog

    dlg = NetMergeDialog(net_a=net_a, net_b=net_b, parent=parent_widget)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return False

    chosen_name = dlg.chosen_name or net_a.name
    merged = Net(
        id=net_a.id,
        name=chosen_name,
        pins=net_a.pins | net_b.pins,
    )
    # net_a survives (with merged pins + chosen name); net_b is deleted.
    cmd.record_net_change(net_a.id, before=net_a, after=merged)
    cmd.record_net_change(net_b.id, before=net_b, after=None)
    session.schematic.nets[net_a.id] = merged
    del session.schematic.nets[net_b.id]
    return True
