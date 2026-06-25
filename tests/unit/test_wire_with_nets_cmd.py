# SPDX-License-Identifier: MIT
"""Tests for AddWireWithNetsCommand — undo/redo with schematic mutations."""

from __future__ import annotations

import uuid
from typing import cast

from protoboard.domain.commands.base import Command, CommandStack
from protoboard.domain.commands.component_cmds import AddWireWithNetsCommand
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.wire import Wire, WireCollection
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point

# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------


def _wire() -> Wire:
    return Wire(
        id=uuid.uuid4(),
        path=[Point(0.5, 0.5), Point(1.5, 0.5)],
        insulated=True,
        side="top",
    )


class _FakeSchematicState:
    """Minimal SchematicState stub — no Qt, no board needed."""

    def __init__(self) -> None:
        from protoboard.domain.board.board import Board
        from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
        from protoboard.domain.board.pads import PadLayers

        self.board = Board(root=BoardGrid(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            cell=PadAtom(
                width_policy=SizePolicy.HUG,
                height_policy=SizePolicy.HUG,
                layers=PadLayers(),
            ),
            rows=1, cols=1, row_gap=0.0, col_gap=0.0,
        ))
        self.layout = Layout(placements={}, wires=WireCollection())
        self.instances: dict[uuid.UUID, ComponentInstance] = {}
        self.components: dict[str, ComponentDefinition] = {}
        self.schematic = Schematic(nets={})


# ---------------------------------------------------------------------------
# Tests — create (BOTH_UNCONNECTED scenario)
# ---------------------------------------------------------------------------


class TestAddWireWithNetsCreate:
    def test_apply_adds_wire(self) -> None:
        state = _FakeSchematicState()
        wire = _wire()
        cmd = AddWireWithNetsCommand(wire=wire)
        cmd.apply(state)
        assert wire in state.layout.wires

    def test_apply_with_new_net(self) -> None:
        state = _FakeSchematicState()
        wire = _wire()
        net_id = uuid.uuid4()
        net = Net(id=net_id, name="Net-(R1-2)", pins=set())
        cmd = AddWireWithNetsCommand(wire=wire)
        # Simulate resolver: mutate schematic, then record
        state.schematic.nets[net_id] = net
        cmd.record_net_change(net_id, before=None, after=net)

        cmd.apply(state)  # first apply — wire only
        assert wire in state.layout.wires
        assert net_id in state.schematic.nets

    def test_revert_removes_wire_and_net(self) -> None:
        state = _FakeSchematicState()
        wire = _wire()
        net_id = uuid.uuid4()
        net = Net(id=net_id, name="Net-(R1-2)", pins=set())
        cmd = AddWireWithNetsCommand(wire=wire)
        state.schematic.nets[net_id] = net
        cmd.record_net_change(net_id, before=None, after=net)

        cmd.apply(state)
        assert net_id in state.schematic.nets
        cmd.revert(state)
        assert wire not in state.layout.wires
        assert net_id not in state.schematic.nets

    def test_redo_restores_wire_and_net(self) -> None:
        state = _FakeSchematicState()
        wire = _wire()
        net_id = uuid.uuid4()
        net = Net(id=net_id, name="Net-(R1-2)", pins=set())
        cmd = AddWireWithNetsCommand(wire=wire)
        state.schematic.nets[net_id] = net
        cmd.record_net_change(net_id, before=None, after=net)

        cmd.apply(state)   # apply (first time)
        cmd.revert(state)  # undo
        assert net_id not in state.schematic.nets
        cmd.apply(state)   # redo
        assert wire in state.layout.wires
        assert net_id in state.schematic.nets
        assert state.schematic.nets[net_id].name == "Net-(R1-2)"


# ---------------------------------------------------------------------------
# Tests — extend (ONE_UNCONNECTED scenario)
# ---------------------------------------------------------------------------


class TestAddWireWithNetsExtend:
    def test_revert_restores_old_net(self) -> None:
        state = _FakeSchematicState()
        wire = _wire()
        r1_id = uuid.uuid4()
        r2_id = uuid.uuid4()

        # Existing net has only R1-pin2
        old_net = Net(
            id=uuid.uuid4(),
            name="VCC",
            pins={PinRef(component_id=r1_id, pin_number=2)},
        )
        new_net = Net(
            id=old_net.id,
            name="VCC",
            pins={
                PinRef(component_id=r1_id, pin_number=2),
                PinRef(component_id=r2_id, pin_number=1),
            },
        )
        state.schematic.nets[old_net.id] = new_net  # resolver already updated
        cmd = AddWireWithNetsCommand(wire=wire)
        cmd.record_net_change(old_net.id, before=old_net, after=new_net)

        cmd.apply(state)
        cmd.revert(state)
        assert state.schematic.nets[old_net.id].pins == old_net.pins


# ---------------------------------------------------------------------------
# Tests — merge (DIFFERENT_NETS scenario)
# ---------------------------------------------------------------------------


class TestAddWireWithNetsMerge:
    def test_revert_restores_both_nets(self) -> None:
        state = _FakeSchematicState()
        wire = _wire()
        r1_id = uuid.uuid4()
        r2_id = uuid.uuid4()

        net_a = Net(
            id=uuid.uuid4(),
            name="VCC",
            pins={PinRef(component_id=r1_id, pin_number=1)},
        )
        net_b = Net(
            id=uuid.uuid4(),
            name="GND",
            pins={PinRef(component_id=r2_id, pin_number=2)},
        )
        merged = Net(
            id=net_a.id,
            name="VCC",
            pins=net_a.pins | net_b.pins,
        )

        # Simulate resolver: net_a → merged, net_b deleted
        state.schematic.nets[net_a.id] = merged
        # net_b already deleted by resolver
        cmd = AddWireWithNetsCommand(wire=wire)
        cmd.record_net_change(net_a.id, before=net_a, after=merged)
        cmd.record_net_change(net_b.id, before=net_b, after=None)

        cmd.apply(state)
        cmd.revert(state)

        # net_a back to original, net_b restored
        assert state.schematic.nets[net_a.id].pins == net_a.pins
        assert state.schematic.nets[net_a.id].name == "VCC"
        assert net_b.id in state.schematic.nets
        assert wire not in state.layout.wires

    def test_redo_reapplies_merge(self) -> None:
        state = _FakeSchematicState()
        wire = _wire()
        r1_id = uuid.uuid4()
        r2_id = uuid.uuid4()

        net_a = Net(id=uuid.uuid4(), name="A", pins={PinRef(r1_id, 1)})
        net_b = Net(id=uuid.uuid4(), name="B", pins={PinRef(r2_id, 1)})
        merged = Net(id=net_a.id, name="A", pins=net_a.pins | net_b.pins)

        state.schematic.nets[net_a.id] = merged
        cmd = AddWireWithNetsCommand(wire=wire)
        cmd.record_net_change(net_a.id, before=net_a, after=merged)
        cmd.record_net_change(net_b.id, before=net_b, after=None)

        cmd.apply(state)   # apply
        cmd.revert(state)  # undo — net_b restored
        assert net_b.id in state.schematic.nets

        cmd.apply(state)   # redo — net_b deleted again
        assert net_b.id not in state.schematic.nets
        assert state.schematic.nets[net_a.id].pins == merged.pins


# ---------------------------------------------------------------------------
# Integration via CommandStack
# ---------------------------------------------------------------------------


class TestWireDrawPropagatesSameName:
    """Wiring one pin of a same-name group (via route tool → resolve_wire_nets)
    pulls in the siblings — the path the user actually uses, not just the
    explicit AssignPinToNetCommand."""

    def _ic_two_gnd_state(self) -> tuple[_FakeSchematicState, uuid.UUID]:
        from protoboard.domain.components.footprints import DIL

        defn = ComponentDefinition(
            name="IC", footprint=DIL(n=8),
            pin_names={1: "A", 2: "B", 3: "C", 4: "GND",
                       5: "E", 6: "F", 7: "G", 8: "GND"},
        )
        inst = ComponentInstance.new("IC", "U1", "")  # empty identities — built on demand
        state = _FakeSchematicState()
        state.instances = {inst.id: inst}
        state.components = {"IC": defn}
        return state, inst.id

    def test_extend_net_adds_sibling(self) -> None:
        from protoboard.domain.schematic.wire_net_ops import (
            WireNetClassification,
            WireNetKind,
        )
        from protoboard.gui.wire_net_resolver import _extend_net

        state, comp_id = self._ic_two_gnd_state()
        # Pre-existing GND net (some other pin already on it).
        other = uuid.uuid4()
        gnd = Net(id=uuid.uuid4(), name="GND",
                  pins={PinRef(component_id=other, pin_number=1)})
        state.schematic.nets[gnd.id] = gnd
        cmd = AddWireWithNetsCommand(wire=_wire())
        # Wire connects net GND (pin_a side) to unconnected IC pin 4 (GND).
        cl = WireNetClassification(
            kind=WireNetKind.ONE_UNCONNECTED,
            pin_a=None, pin_b=(comp_id, 4),
            net_a=gnd, net_b=None,
        )
        _extend_net(state, cmd, cl)  # type: ignore[arg-type]
        pins = {(r.component_id, r.pin_number) for r in state.schematic.nets[gnd.id].pins}
        # Both IC GND pins (4 and 8) joined, plus the pre-existing other pin.
        assert (comp_id, 4) in pins
        assert (comp_id, 8) in pins, "same-name sibling must come along"
        assert (other, 1) in pins

    def test_create_new_net_adds_siblings_of_both_ends(self) -> None:
        from protoboard.domain.schematic.wire_net_ops import (
            WireNetClassification,
            WireNetKind,
        )
        from protoboard.gui.wire_net_resolver import _create_new_net

        state, comp_id = self._ic_two_gnd_state()
        # Wire between IC pin 4 (GND) and IC pin 2 (B, ungrouped).
        cmd = AddWireWithNetsCommand(wire=_wire())
        cl = WireNetClassification(
            kind=WireNetKind.BOTH_UNCONNECTED,
            pin_a=(comp_id, 4), pin_b=(comp_id, 2),
            net_a=None, net_b=None,
        )
        _create_new_net(state, cmd, cl)  # type: ignore[arg-type]
        new_net = next(iter(state.schematic.nets.values()))
        pins = {(r.component_id, r.pin_number) for r in new_net.pins}
        assert (comp_id, 4) in pins
        assert (comp_id, 8) in pins, "GND sibling of pin 4 must join"
        assert (comp_id, 2) in pins  # the other end (ungrouped, only itself)


class TestAddWireWithNetsViaStack:
    def test_stack_undo_redo_cycle(self) -> None:
        state = _FakeSchematicState()
        wire = _wire()
        net_id = uuid.uuid4()
        net = Net(id=net_id, name="Net-(R1-1)", pins=set())

        stack: CommandStack[_FakeSchematicState] = CommandStack()
        state.schematic.nets[net_id] = net
        cmd = AddWireWithNetsCommand(wire=wire)
        cmd.record_net_change(net_id, before=None, after=net)

        stack.execute(cast("Command[_FakeSchematicState]", cmd), state)
        assert wire in state.layout.wires
        assert net_id in state.schematic.nets

        stack.undo(state)
        assert wire not in state.layout.wires
        assert net_id not in state.schematic.nets

        stack.redo(state)
        assert wire in state.layout.wires
        assert net_id in state.schematic.nets
