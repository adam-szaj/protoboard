# SPDX-License-Identifier: MIT
"""Tests for DisconnectPinCommand."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from protoboard.domain.commands.component_cmds import DisconnectPinCommand
from protoboard.domain.layout.layout import Layout
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic


@dataclass
class _State:
    layout: Layout = field(default_factory=Layout)
    schematic: Schematic = field(
        default_factory=lambda: Schematic(nets={})
    )


def _net(pins: set[PinRef], name: str = "N") -> Net:
    return Net(id=uuid.uuid4(), name=name, pins=pins)


def _ref(pin: int = 1) -> tuple[uuid.UUID, PinRef]:
    cid = uuid.uuid4()
    return cid, PinRef(component_id=cid, pin_number=pin)


class TestDisconnectPinCommand:
    def test_removes_pin_from_net(self) -> None:
        cid, ref = _ref()
        other_cid, other_ref = _ref()
        net = _net({ref, other_ref})
        state = _State(schematic=Schematic(nets={net.id: net}))
        cmd = DisconnectPinCommand(component_id=cid, pin_number=1)
        cmd.apply(state)
        remaining = state.schematic.nets[net.id]
        assert ref not in remaining.pins
        assert other_ref in remaining.pins

    def test_deletes_net_when_empty(self) -> None:
        cid, ref = _ref()
        net = _net({ref})
        state = _State(schematic=Schematic(nets={net.id: net}))
        cmd = DisconnectPinCommand(component_id=cid, pin_number=1)
        cmd.apply(state)
        assert net.id not in state.schematic.nets

    def test_noop_when_pin_has_no_net(self) -> None:
        cid = uuid.uuid4()
        state = _State(schematic=Schematic(nets={}))
        cmd = DisconnectPinCommand(component_id=cid, pin_number=1)
        cmd.apply(state)  # must not raise
        assert state.schematic.nets == {}

    def test_revert_restores_net_with_pin(self) -> None:
        cid, ref = _ref()
        other_cid, other_ref = _ref()
        net = _net({ref, other_ref})
        state = _State(schematic=Schematic(nets={net.id: net}))
        cmd = DisconnectPinCommand(component_id=cid, pin_number=1)
        cmd.apply(state)
        cmd.revert(state)
        restored = state.schematic.nets[net.id]
        assert ref in restored.pins
        assert other_ref in restored.pins

    def test_revert_restores_deleted_net(self) -> None:
        cid, ref = _ref()
        net = _net({ref}, name="SOLO")
        state = _State(schematic=Schematic(nets={net.id: net}))
        cmd = DisconnectPinCommand(component_id=cid, pin_number=1)
        cmd.apply(state)
        assert net.id not in state.schematic.nets
        cmd.revert(state)
        assert net.id in state.schematic.nets
        assert state.schematic.nets[net.id].name == "SOLO"

    def test_undo_redo_cycle(self) -> None:
        cid, ref = _ref()
        net = _net({ref})
        state = _State(schematic=Schematic(nets={net.id: net}))
        cmd = DisconnectPinCommand(component_id=cid, pin_number=1)
        cmd.apply(state)
        assert net.id not in state.schematic.nets
        cmd.revert(state)
        assert net.id in state.schematic.nets
        cmd.apply(state)
        assert net.id not in state.schematic.nets
