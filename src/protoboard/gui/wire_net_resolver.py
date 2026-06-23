# SPDX-License-Identifier: MIT
"""GUI-level wire-to-net resolver: classifies endpoints, mutates schematic, records
changes into the command for undo/redo support."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, cast

import structlog
from protoboard.domain.schematic.net import Net
from protoboard.domain.schematic.pin_siblings import same_name_refs
from protoboard.domain.schematic.wire_net_ops import (
    WireNetKind,
    classify_wire_nets,
)

if TYPE_CHECKING:
    from protoboard.domain.commands.component_cmds import AddWireWithNetsCommand
    from protoboard.domain.layout.wire import Wire
    from protoboard.domain.schematic.wire_net_ops import (
        WireNetClassification,
        _SessionLike,
    )
    from protoboard.gui.session import EditorSession

_log = structlog.get_logger(__name__)


def resolve_wire_nets(
    session: EditorSession,
    wire: Wire,
    cmd: AddWireWithNetsCommand,
    parent_widget: object = None,
) -> bool:
    """Classify wire endpoints and update session.schematic accordingly.

    Mutates session.schematic.nets and records every change into *cmd* so
    that undo/redo work correctly.

    Returns True if the wire should be committed, False if the user cancelled
    (only possible for DIFFERENT_NETS when the merge dialog is rejected).
    """
    # EditorSession satisfies the _SessionLike protocol structurally, but its
    # ``layout`` is a read/write property rather than a plain attribute, so
    # Pyright rejects it against the protocol's invariant (mutable) ``layout``
    # field.  The value is genuinely the right shape — cast across the gap.
    cl = classify_wire_nets(wire, cast("_SessionLike", session))

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
    # Both endpoints plus the same-name siblings of each (one node per group).
    insts, comps = session.instances, session.components
    pins = set(same_name_refs(cl.pin_a[0], cl.pin_a[1], insts, comps))
    pins |= set(same_name_refs(cl.pin_b[0], cl.pin_b[1], insts, comps))
    new_net = Net(id=new_id, name=name, pins=pins)
    cmd.record_net_change(new_id, before=None, after=_snapshot(new_net))
    session.schematic.nets[new_id] = new_net
    _log.info("wire_net.created", net_name=name)


def _extend_net(
    session: EditorSession,
    cmd: AddWireWithNetsCommand,
    cl: WireNetClassification,
) -> None:
    net = cl.net_a if cl.net_a is not None else cl.net_b
    assert net is not None
    # The "unconnected" endpoint may have a pin (classical case — add
    # the pin to the net) or may not (the wire-touch fallback —
    # nothing to add to the pin list; the new wire joins the net via
    # geometric contact and the connectivity graph picks it up).
    unconnected_pin = cl.pin_b if cl.net_a is not None else cl.pin_a
    if unconnected_pin is None:
        # Pure wire-to-wire join: no pin to register.
        _log.info("wire_net.joined_via_wire_touch", net=net.name)
        return
    cmd.record_net_change(net.id, before=_snapshot(net), after=None)
    # Add the wired pin plus its same-name siblings (one electrical node).
    for ref in same_name_refs(
        unconnected_pin[0], unconnected_pin[1], session.instances, session.components
    ):
        session.schematic.add_pin_to_net(ref, net.id)
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
    session.schematic.merge_nets(net_a.id, net_b.id, new_name=chosen_name)
    cmd._nets_after[net_a.id] = _snapshot(net_a)
    cmd._nets_after[net_b.id] = None
    _log.info("wire_net.merged", survivor=net_a.name, merged=net_b.name)
    return True
