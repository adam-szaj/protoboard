# SPDX-License-Identifier: MIT
"""GUI-level wire-to-net resolver: classifies endpoints, mutates schematic, records
changes into the command for undo/redo support."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import structlog
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

_log = structlog.get_logger(__name__)


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
    _log.debug("wire_net.classified", kind=cl.kind.name)

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


def _snapshot(net: Net) -> Net:
    """Return a deep copy of *net* for use as an undo snapshot."""
    return Net(id=net.id, name=net.name, pins=net.pins.copy())


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
    cmd.record_net_change(new_id, before=None, after=_snapshot(new_net))
    session.schematic.nets[new_id] = new_net
    _log.info("wire_net.created", net_name=name)


def _extend_net(
    session: EditorSession,
    cmd: AddWireWithNetsCommand,
    cl: WireNetClassification,
) -> None:
    assert cl.pin_a is not None and cl.pin_b is not None
    net = cl.net_a if cl.net_a is not None else cl.net_b
    assert net is not None
    unconnected = cl.pin_b if cl.net_a is not None else cl.pin_a
    new_ref = PinRef(component_id=unconnected[0], pin_number=unconnected[1])
    cmd.record_net_change(net.id, before=_snapshot(net), after=None)
    net.pins.add(new_ref)
    cmd._nets_after[net.id] = _snapshot(net)
    _log.info("wire_net.extended", net=net.name)


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
        _log.debug("wire_net.merge_cancelled", net_a=net_a.name, net_b=net_b.name)
        return False

    chosen_name = dlg.chosen_name or net_a.name
    # Record snapshots before mutating.
    cmd.record_net_change(net_a.id, before=_snapshot(net_a), after=None)
    cmd.record_net_change(net_b.id, before=_snapshot(net_b), after=None)
    # Merge net_b into net_a in-place, then delete net_b.
    net_a.name = chosen_name
    net_a.pins |= net_b.pins
    del session.schematic.nets[net_b.id]
    cmd._nets_after[net_a.id] = _snapshot(net_a)
    cmd._nets_after[net_b.id] = None
    _log.info("wire_net.merged", survivor=net_a.name, merged=net_b.name)
    return True
