# SPDX-License-Identifier: MIT
"""Tests for net routing-completeness helpers."""

from __future__ import annotations

import uuid

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.connectivity.graph import ConnectivityGraph
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement, PlacementCollection
from protoboard.domain.layout.wire import Wire, WireCollection
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from protoboard.services.routing.net_status import net_is_complete, routed_net_ids
from tests.unit._autoroute_invariants import make_sil, sil_families


def _perfboard(rows: int = 1, cols: int = 5) -> Board:
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


def _r_defn() -> ComponentDefinition:
    return make_sil("R", 2)


def _inst(defn: ComponentDefinition, refdes: str) -> ComponentInstance:
    return ComponentInstance.new("R", refdes, "10k").bind(defn, sil_families(defn))


def _place(comp_id: uuid.UUID, x: float, rotation: int = 90) -> ComponentPlacement:
    return ComponentPlacement(
        component_id=comp_id,
        position=Point(x, 0.5),
        rotation=rotation,
        side="top",
    )


class TestNetIsComplete:
    def test_single_pin_net_is_complete(self) -> None:
        board = _perfboard()
        defn = _r_defn()
        r1 = _inst(defn, "R1")
        lay = Layout(
            placements=PlacementCollection({r1.id: _place(r1.id, 0.5)}),
            wires=WireCollection(),
        )
        graph = ConnectivityGraph(board, lay, {defn.name: defn}, {r1.id: r1})
        net = Net(
            id=uuid.uuid4(),
            name="VCC",
            pins={PinRef(component_id=r1.id, pin_number=1)},
        )
        assert net_is_complete(net, graph)

    def test_two_pins_connected_via_wire(self) -> None:
        board = _perfboard()
        defn = _r_defn()
        r1 = _inst(defn, "R1")
        r2 = _inst(defn, "R2")
        # R1 pin1@(0.5,0.5), pin2@(1.5,0.5); R2 pin1@(2.5,0.5), pin2@(3.5,0.5)
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(1.5, 0.5), Point(2.5, 0.5)],
            insulated=False,
            side="top",
        )
        lay = Layout(
            placements=PlacementCollection(
                {r1.id: _place(r1.id, 0.5), r2.id: _place(r2.id, 2.5)}
            ),
            wires=WireCollection([wire]),
        )
        graph = ConnectivityGraph(board, lay, {defn.name: defn}, {r1.id: r1, r2.id: r2})
        net = Net(
            id=uuid.uuid4(),
            name="MID",
            pins={
                PinRef(component_id=r1.id, pin_number=2),
                PinRef(component_id=r2.id, pin_number=1),
            },
        )
        assert net_is_complete(net, graph)

    def test_two_pins_not_connected(self) -> None:
        board = _perfboard()
        defn = _r_defn()
        r1 = _inst(defn, "R1")
        r2 = _inst(defn, "R2")
        lay = Layout(
            placements=PlacementCollection(
                {r1.id: _place(r1.id, 0.5), r2.id: _place(r2.id, 2.5)}
            ),
            wires=WireCollection(),
        )
        graph = ConnectivityGraph(board, lay, {defn.name: defn}, {r1.id: r1, r2.id: r2})
        net = Net(
            id=uuid.uuid4(),
            name="MID",
            pins={
                PinRef(component_id=r1.id, pin_number=2),
                PinRef(component_id=r2.id, pin_number=1),
            },
        )
        assert not net_is_complete(net, graph)

    def test_unplaced_pins_considered_complete(self) -> None:
        board = _perfboard()
        defn = _r_defn()
        r1 = _inst(defn, "R1")
        # r1 not placed
        lay = Layout(placements=PlacementCollection({}), wires=WireCollection())
        graph = ConnectivityGraph(board, lay, {defn.name: defn}, {r1.id: r1})
        net = Net(
            id=uuid.uuid4(),
            name="VCC",
            pins={
                PinRef(component_id=r1.id, pin_number=1),
                PinRef(component_id=r1.id, pin_number=2),
            },
        )
        # fewer than 2 placed pins → complete (nothing to route)
        assert net_is_complete(net, graph)


class TestRoutedNetIds:
    def test_returns_complete_nets(self) -> None:
        board = _perfboard()
        defn = _r_defn()
        r1 = _inst(defn, "R1")
        r2 = _inst(defn, "R2")
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(1.5, 0.5), Point(2.5, 0.5)],
            insulated=False,
            side="top",
        )
        lay = Layout(
            placements=PlacementCollection(
                {r1.id: _place(r1.id, 0.5), r2.id: _place(r2.id, 2.5)}
            ),
            wires=WireCollection([wire]),
        )
        graph = ConnectivityGraph(board, lay, {defn.name: defn}, {r1.id: r1, r2.id: r2})
        mid_id = uuid.uuid4()
        vcc_id = uuid.uuid4()
        sch = Schematic(
            nets={
                mid_id: Net(
                    id=mid_id,
                    name="MID",
                    pins={
                        PinRef(component_id=r1.id, pin_number=2),
                        PinRef(component_id=r2.id, pin_number=1),
                    },
                ),
                vcc_id: Net(
                    id=vcc_id,
                    name="VCC",
                    pins={PinRef(component_id=r1.id, pin_number=1)},
                ),
            }
        )
        result = routed_net_ids(sch, graph)
        assert mid_id in result  # connected via wire
        assert vcc_id in result  # single placed pin → complete

    def test_unconnected_net_not_in_result(self) -> None:
        board = _perfboard()
        defn = _r_defn()
        r1 = _inst(defn, "R1")
        r2 = _inst(defn, "R2")
        lay = Layout(
            placements=PlacementCollection(
                {r1.id: _place(r1.id, 0.5), r2.id: _place(r2.id, 2.5)}
            ),
            wires=WireCollection(),
        )
        graph = ConnectivityGraph(board, lay, {defn.name: defn}, {r1.id: r1, r2.id: r2})
        mid_id = uuid.uuid4()
        sch = Schematic(
            nets={
                mid_id: Net(
                    id=mid_id,
                    name="MID",
                    pins={
                        PinRef(component_id=r1.id, pin_number=2),
                        PinRef(component_id=r2.id, pin_number=1),
                    },
                ),
            }
        )
        result = routed_net_ids(sch, graph)
        assert mid_id not in result
