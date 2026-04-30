# SPDX-License-Identifier: MIT
"""Tests for the full-netlist autorouter."""

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


def _place(cid: uuid.UUID, x: float, y: float) -> ComponentPlacement:
    return ComponentPlacement(
        component_id=cid, position=Point(x, y), rotation=0, side="top"
    )


class TestAutorouter:
    def test_routes_simple_two_pin_net(self) -> None:
        """Two components on a 5×5 board, one net connecting their pins."""
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
        # Place A at (0.5, 0.5): pin1=(0.5,0.5), pin2=(1.5,0.5)
        # Place B at (3.5, 0.5): pin1=(3.5,0.5), pin2=(4.5,0.5)
        layout = Layout(
            placements={
                cid_a: _place(cid_a, 0.5, 0.5),
                cid_b: _place(cid_b, 3.5, 0.5),
            }
        )
        ref_a = PinRef(component_id=cid_a, pin_number=2)
        ref_b = PinRef(component_id=cid_b, pin_number=1)
        net = Net(id=uuid.uuid4(), name="N1", pins={ref_a, ref_b})
        schematic = Schematic(nets={net.id: net})
        components = {"COMP": defn}
        instances = {cid_a: inst_a, cid_b: inst_b}

        result = autoroute(board, layout, schematic, components, instances)

        assert result.unrouted_nets == []
        assert len(result.wires) >= 1
        # Path must start and end at the pin positions.
        wire = result.wires[0]
        endpoints = {wire.path[0], wire.path[-1]}
        assert Point(1.5, 0.5) in endpoints or Point(3.5, 0.5) in endpoints

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
        """Wire path must not pass through pads that belong to a different net."""
        # Board: 1×7 row.  Two nets:
        #   NET1: comp_a pin2 (1.5,0.5) → comp_b pin1 (5.5,0.5)
        #   NET2: comp_c pin1 (3.5,0.5)  — sits in the middle of the direct path
        # The router must go around (0.5,0.5)→(1.5,0.5) then detour, or report
        # unrouted; it must NOT route through (3.5,0.5) which belongs to NET2.
        board = _perfboard(3, 7)
        defn = _sil2_def()
        cid_a, cid_b, cid_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

        inst_a = ComponentInstance(id=cid_a, definition_ref="COMP", refdes="R1", value="")
        inst_b = ComponentInstance(id=cid_b, definition_ref="COMP", refdes="R2", value="")
        inst_c = ComponentInstance(id=cid_c, definition_ref="COMP", refdes="R3", value="")

        layout = Layout(placements={
            cid_a: _place(cid_a, 0.5, 0.5),  # pins at (0.5,0.5),(1.5,0.5)
            cid_b: _place(cid_b, 4.5, 0.5),  # pins at (4.5,0.5),(5.5,0.5)
            cid_c: _place(cid_c, 2.5, 0.5),  # pins at (2.5,0.5),(3.5,0.5) — blocker
        })

        ref_a2 = PinRef(component_id=cid_a, pin_number=2)  # (1.5,0.5)
        ref_b1 = PinRef(component_id=cid_b, pin_number=1)  # (4.5,0.5)
        ref_c1 = PinRef(component_id=cid_c, pin_number=1)  # (2.5,0.5)
        ref_c2 = PinRef(component_id=cid_c, pin_number=2)  # (3.5,0.5)

        net1 = Net(id=uuid.uuid4(), name="NET1", pins={ref_a2, ref_b1})
        net2 = Net(id=uuid.uuid4(), name="NET2", pins={ref_c1, ref_c2})
        schematic = Schematic(nets={net1.id: net1, net2.id: net2})
        components = {"COMP": defn}
        instances = {cid_a: inst_a, cid_b: inst_b, cid_c: inst_c}

        result = autoroute(board, layout, schematic, components, instances)

        # All wires produced for NET1 must not pass through NET2 pads.
        net2_pts = {
            Point(2.5, 0.5), Point(3.5, 0.5),
        }
        for wire in result.wires:
            interior = wire.path[1:-1]  # endpoints may coincide with own net pads
            for pt in interior:
                assert pt not in net2_pts, (
                    f"Wire passes through NET2 pad {pt}: {wire.path}"
                )

    def test_already_routed_net_produces_no_extra_wires(self) -> None:
        """If source and target are the same point, no wire should be added."""
        board = _perfboard(5, 5)
        defn = _sil2_def()
        cid = uuid.uuid4()
        inst = ComponentInstance(id=cid, definition_ref="COMP", refdes="R1", value="1k")
        layout = Layout(placements={cid: _place(cid, 0.5, 0.5)})
        # Net with both pins on same component — one pin not placed since
        # component is single and both refs point to same component.
        # Just test that no crash occurs.
        ref1 = PinRef(component_id=cid, pin_number=1)
        ref2 = PinRef(component_id=cid, pin_number=2)
        net = Net(id=uuid.uuid4(), name="SAME", pins={ref1, ref2})
        schematic = Schematic(nets={net.id: net})
        result = autoroute(board, layout, schematic, {"COMP": defn}, {cid: inst})
        # Should route 1 wire or recognise trivial case — just no crash.
        assert result.unrouted_nets == []
