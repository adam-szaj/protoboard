# SPDX-License-Identifier: MIT
"""Tests for the full-netlist autorouter.

Pin positions with SingleRow(n=2):
  rotation=0  → vertical:   pin1=(x,   y), pin2=(x,   y+1)
  rotation=90 → horizontal: pin1=(x,   y), pin2=(x+1, y)
"""

from __future__ import annotations

import uuid

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SingleRow
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import WireSegment
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from protoboard.services.routing.autorouter import autoroute


def _perfboard(rows: int, cols: int) -> Board:
    grid = BoardGrid(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        ),
        rows=rows,
        cols=cols,
        row_gap=0.0,
        col_gap=0.0,
    )
    return Board(root=grid)


def _sil2_def(name: str = "COMP") -> ComponentDefinition:
    return ComponentDefinition(
        name=name,
        footprint=SingleRow(n=2),
        pin_names={1: "A", 2: "B"},
    )


def _place(cid: uuid.UUID, x: float, y: float, rotation: int = 90) -> ComponentPlacement:
    """rotation=90 → horizontal pins: pin1=(x,y), pin2=(x+1,y)."""
    return ComponentPlacement(
        component_id=cid, position=Point(x, y), rotation=rotation, side="top"
    )


class TestAutorouter:
    def test_routes_simple_two_pin_net(self) -> None:
        """Two components on a 5×5 board, one net connecting their pins.

        R1 (rot=90) at (0.5,0.5): pin1=(0.5,0.5), pin2=(1.5,0.5)
        R2 (rot=90) at (3.5,0.5): pin1=(3.5,0.5), pin2=(4.5,0.5)
        Net: R1-pin2 → R2-pin1  (1.5,0.5) → (3.5,0.5)
        """
        board = _perfboard(5, 5)
        defn = _sil2_def()
        cid_a = uuid.uuid4()
        cid_b = uuid.uuid4()

        inst_a = ComponentInstance(
            id=cid_a, definition_ref="COMP", refdes="R1", value="1k"
        )
        inst_b = ComponentInstance(
            id=cid_b, definition_ref="COMP", refdes="R2", value="1k"
        )
        layout = Layout(
            placements={
                cid_a: _place(cid_a, 0.5, 0.5),  # pin1=(0.5,0.5) pin2=(1.5,0.5)
                cid_b: _place(cid_b, 3.5, 0.5),  # pin1=(3.5,0.5) pin2=(4.5,0.5)
            }
        )
        ref_a2 = PinRef(component_id=cid_a, pin_number=2)  # (1.5, 0.5)
        ref_b1 = PinRef(component_id=cid_b, pin_number=1)  # (3.5, 0.5)
        net = Net(id=uuid.uuid4(), name="N1", pins={ref_a2, ref_b1})
        schematic = Schematic(nets={net.id: net})
        components = {"COMP": defn}
        instances = {cid_a: inst_a, cid_b: inst_b}

        result = autoroute(board, layout, schematic, components, instances)

        assert result.unrouted_nets == []
        assert len(result.wires) == 1
        wire = result.wires[0]
        endpoints = {wire.path[0], wire.path[-1]}
        assert Point(1.5, 0.5) in endpoints
        assert Point(3.5, 0.5) in endpoints

    def test_empty_schematic_returns_no_wires(self) -> None:
        board = _perfboard(5, 5)
        layout = Layout(placements={})
        schematic = Schematic(nets={})
        result = autoroute(board, layout, schematic, {}, {})
        assert result.wires == []
        assert result.unrouted_nets == []

    def test_single_pin_net_skipped(self) -> None:
        board = _perfboard(5, 5)
        cid = uuid.uuid4()
        defn = _sil2_def()
        inst = ComponentInstance(id=cid, definition_ref="COMP", refdes="R1", value="1k")
        layout = Layout(placements={cid: _place(cid, 0.5, 0.5)})
        ref = PinRef(component_id=cid, pin_number=1)
        net = Net(id=uuid.uuid4(), name="SOLO", pins={ref})
        schematic = Schematic(nets={net.id: net})
        result = autoroute(board, layout, schematic, {"COMP": defn}, {cid: inst})
        assert result.wires == []
        assert result.unrouted_nets == []

    def test_router_avoids_pads_of_other_nets(self) -> None:
        """Wire path must not pass through pads that belong to a different net.

        Board 3×7:
          NET1: R1-pin2=(1.5,0.5) → R2-pin1=(5.5,0.5)   [need to route]
          NET2: R3-pin1=(3.5,0.5), R3-pin2=(4.5,0.5)    [blocker in middle]
        Router must detour around row 0 via row 1 or report unrouted.
        """
        board = _perfboard(3, 7)
        defn = _sil2_def()
        cid_a, cid_b, cid_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

        inst_a = ComponentInstance(id=cid_a, definition_ref="COMP", refdes="R1", value="")
        inst_b = ComponentInstance(id=cid_b, definition_ref="COMP", refdes="R2", value="")
        inst_c = ComponentInstance(id=cid_c, definition_ref="COMP", refdes="R3", value="")

        layout = Layout(placements={
            cid_a: _place(cid_a, 0.5, 0.5),  # pin1=(0.5,0.5) pin2=(1.5,0.5)
            cid_b: _place(cid_b, 5.5, 0.5),  # pin1=(5.5,0.5) pin2=(6.5,0.5)
            cid_c: _place(cid_c, 3.5, 0.5),  # pin1=(3.5,0.5) pin2=(4.5,0.5) — blocker
        })

        ref_a2 = PinRef(component_id=cid_a, pin_number=2)  # (1.5, 0.5)
        ref_b1 = PinRef(component_id=cid_b, pin_number=1)  # (5.5, 0.5)
        ref_c1 = PinRef(component_id=cid_c, pin_number=1)  # (3.5, 0.5)
        ref_c2 = PinRef(component_id=cid_c, pin_number=2)  # (4.5, 0.5)

        net1 = Net(id=uuid.uuid4(), name="NET1", pins={ref_a2, ref_b1})
        net2 = Net(id=uuid.uuid4(), name="NET2", pins={ref_c1, ref_c2})
        schematic = Schematic(nets={net1.id: net1, net2.id: net2})
        components = {"COMP": defn}
        instances = {cid_a: inst_a, cid_b: inst_b, cid_c: inst_c}

        result = autoroute(board, layout, schematic, components, instances)

        # All wires produced for NET1 must not pass through NET2 pads.
        net2_pts = {Point(3.5, 0.5), Point(4.5, 0.5)}
        for wire in result.wires:
            interior = wire.path[1:-1]  # endpoints may coincide with own net pads
            for pt in interior:
                assert pt not in net2_pts, (
                    f"Wire passes through NET2 pad {pt}: {wire.path}"
                )

    def test_skips_already_connected_pairs(self) -> None:
        """If a wire already connects two pins of a net, no duplicate is added.

        R1 (rot=90) at (0.5,0.5): pin1=(0.5,0.5), pin2=(1.5,0.5)
        R2 (rot=90) at (2.5,0.5): pin1=(2.5,0.5), pin2=(3.5,0.5)
        Existing wire: (1.5,0.5) → (2.5,0.5) already connects R1-pin2 to R2-pin1.
        """
        board = _perfboard(5, 5)
        defn = _sil2_def()
        cid_a = uuid.uuid4()
        cid_b = uuid.uuid4()
        inst_a = ComponentInstance(id=cid_a, definition_ref="COMP", refdes="R1", value="")
        inst_b = ComponentInstance(id=cid_b, definition_ref="COMP", refdes="R2", value="")

        existing_wire = WireSegment(
            id=uuid.uuid4(),
            path=[Point(1.5, 0.5), Point(2.5, 0.5)],
            insulated=True,
            side="top",
        )
        layout = Layout(
            placements={
                cid_a: _place(cid_a, 0.5, 0.5),  # pin1=(0.5,0.5) pin2=(1.5,0.5)
                cid_b: _place(cid_b, 2.5, 0.5),  # pin1=(2.5,0.5) pin2=(3.5,0.5)
            },
            wires=[existing_wire],
        )
        ref_a2 = PinRef(component_id=cid_a, pin_number=2)  # (1.5, 0.5)
        ref_b1 = PinRef(component_id=cid_b, pin_number=1)  # (2.5, 0.5)
        net = Net(id=uuid.uuid4(), name="N1", pins={ref_a2, ref_b1})
        schematic = Schematic(nets={net.id: net})
        components = {"COMP": defn}
        instances = {cid_a: inst_a, cid_b: inst_b}

        result = autoroute(board, layout, schematic, components, instances)

        assert result.unrouted_nets == []
        # No new wires — the connection already exists.
        assert result.wires == []

    def test_router_avoids_floating_component_pins(self) -> None:
        """Wire must not pass through pins of a component not in any net.

        R1(rot=90)@(0.5,0.5): pin2=(1.5,0.5) → R2(rot=90)@(3.5,0.5): pin1=(3.5,0.5)
        R3(rot=90)@(2.5,0.5): pin1=(2.5,0.5), pin2=(3.5,0.5) — floating, blocks row 0.
        Router must detour through row 1.
        """
        board = _perfboard(2, 5)
        defn = _sil2_def()
        cid_a, cid_b, cid_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        inst_a = ComponentInstance(id=cid_a, definition_ref="COMP", refdes="R1", value="")
        inst_b = ComponentInstance(id=cid_b, definition_ref="COMP", refdes="R2", value="")
        inst_c = ComponentInstance(id=cid_c, definition_ref="COMP", refdes="R3", value="")
        layout = Layout(placements={
            cid_a: _place(cid_a, 0.5, 0.5),   # pin1=(0.5,0.5) pin2=(1.5,0.5)
            cid_b: _place(cid_b, 3.5, 0.5),   # pin1=(3.5,0.5) pin2=(4.5,0.5)
            cid_c: _place(cid_c, 2.5, 0.5),   # pin1=(2.5,0.5) pin2=(3.5,0.5) — blocker
        })
        ref_a2 = PinRef(component_id=cid_a, pin_number=2)
        ref_b1 = PinRef(component_id=cid_b, pin_number=1)
        net = Net(id=uuid.uuid4(), name="N1", pins={ref_a2, ref_b1})
        schematic = Schematic(nets={net.id: net})  # R3 not in any net
        components = {"COMP": defn}
        instances = {cid_a: inst_a, cid_b: inst_b, cid_c: inst_c}

        result = autoroute(board, layout, schematic, components, instances)

        assert result.unrouted_nets == []
        floating_pts = {Point(2.5, 0.5), Point(3.5, 0.5)}
        for wire in result.wires:
            for pt in wire.path[1:-1]:
                assert pt not in floating_pts, (
                    f"Wire passes through floating pin {pt}: {wire.path}"
                )

    def test_already_routed_net_same_component(self) -> None:
        """Two pins on the same placed component — already adjacent, nothing to route."""
        board = _perfboard(5, 5)
        defn = _sil2_def()
        cid = uuid.uuid4()
        inst = ComponentInstance(id=cid, definition_ref="COMP", refdes="R1", value="1k")
        layout = Layout(placements={cid: _place(cid, 0.5, 0.5)})
        ref1 = PinRef(component_id=cid, pin_number=1)
        ref2 = PinRef(component_id=cid, pin_number=2)
        net = Net(id=uuid.uuid4(), name="SAME", pins={ref1, ref2})
        schematic = Schematic(nets={net.id: net})
        result = autoroute(board, layout, schematic, {"COMP": defn}, {cid: inst})
        assert result.unrouted_nets == []
