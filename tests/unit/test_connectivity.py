# SPDX-License-Identifier: MIT
"""Tests for ConnectivityGraph — builds an electrical graph from Board+Layout.

Each node = one electrically distinct point. Pads on the same strip are
one node (unless cut); plated-through pads are one node; wires join nodes.
"""

import uuid

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy, VBox
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.connectivity.graph import ConnectivityGraph
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import WireSegment
from protoboard.domain.schematic.schematic import PinRef
from protoboard.geometry import Point


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


class TestConnectivityGraphBasic:
    def test_isolated_pads_are_separate_nodes(self) -> None:
        board = _perfboard(2, 2)
        lay = Layout(placements={}, wires=[])
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        # 4 pads, plated through → each pad is one merged node (top=bottom),
        # but no wire connects them → 4 separate components.
        assert graph.node_count() == 4
        assert graph.component_count() == 4

    def test_wire_joins_two_pads(self) -> None:
        # Perfboard 1×3 with gap=0: pad centers at (0.5,0), (1.5,0), (2.5,0).
        board = _perfboard(1, 3)
        wire = WireSegment(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(1.5, 0.5)],
            insulated=False,
            side="top",
        )
        lay = Layout(placements={}, wires=[wire])
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        # 3 pads; pad at (0.5,0) and pad at (1.5,0) connected → 2 components.
        assert graph.component_count() == 2

    def test_plated_through_merges_top_and_bottom(self) -> None:
        # A perfboard pad with plated=True: top and bottom are one node.
        board = _perfboard(1, 1)
        lay = Layout(placements={}, wires=[])
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        assert graph.node_count() == 1  # top+bottom merged

    def test_non_plated_pad_has_two_nodes(self) -> None:
        # A pad with top=True, bottom=True, plated=False → 2 independent nodes.
        grid = BoardGrid(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            cell=PadAtom(
                width_policy=SizePolicy.HUG,
                height_policy=SizePolicy.HUG,
                layers=PadLayers(top=True, bottom=True, plated_through=False),
            ),
            rows=1,
            cols=1,
            row_gap=0.0,
            col_gap=0.0,
        )
        board = Board(root=grid)
        lay = Layout(placements={}, wires=[])
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        assert graph.node_count() == 2


class TestConnectivityGraphWithComponents:
    def test_placed_component_pins_on_pads(self) -> None:
        # Pad centers at (0.5,0), (1.5,0), (2.5,0).
        board = _perfboard(1, 3)
        comp_defn = ComponentDefinition(
            name="R",
            footprint=SIL(2),
            pin_names={1: "A", 2: "B"},
        )
        inst = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
        # SIL(2): pin1=(0,0), pin2=(0,1) → place pin1 on pad center (0.5,0).
        pl = ComponentPlacement(
            component_id=inst.id,
            position=Point(0.5, 0.5),
            rotation=0,
            side="top",
        )
        lay = Layout(placements={inst.id: pl}, wires=[])
        graph = ConnectivityGraph(
            board=board,
            layout=lay,
            components={comp_defn.name: comp_defn},
            instances={inst.id: inst},
        )
        # Pin 1 of R1 sits on pad(0,0), pin 2 on pad(0,1).
        node_pin1 = graph.node_for_pin(PinRef(component_id=inst.id, pin_number=1))
        node_pin2 = graph.node_for_pin(PinRef(component_id=inst.id, pin_number=2))
        assert node_pin1 is not None
        assert node_pin2 is not None
        assert node_pin1 != node_pin2

    def test_wire_joins_component_pins(self) -> None:
        # Pad centers: (0.5,0), (1.5,0), (2.5,0).
        board = _perfboard(1, 3)
        comp_defn = ComponentDefinition(
            name="R",
            footprint=SIL(2),
            pin_names={},
        )
        inst = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
        # SIL(2) rotation=90: pin2=(0,1)→rot(1,0)→world(1.5, 0.5).
        pl = ComponentPlacement(
            component_id=inst.id,
            position=Point(0.5, 0.5),
            rotation=90,
            side="top",
        )
        # Wire from pin2=(1.5,0.5) to third pad center=(2.5,0.5).
        wire = WireSegment(
            id=uuid.uuid4(),
            path=[Point(1.5, 0.5), Point(2.5, 0.5)],
            insulated=False,
            side="top",
        )
        lay = Layout(placements={inst.id: pl}, wires=[wire])
        graph = ConnectivityGraph(
            board=board,
            layout=lay,
            components={comp_defn.name: comp_defn},
            instances={inst.id: inst},
        )
        node_pin2 = graph.node_for_pin(PinRef(component_id=inst.id, pin_number=2))
        node_pad3 = graph.node_for_pad(Point(2.5, 0.5), "top")
        assert node_pin2 == node_pad3


class TestElectricalNets:
    def test_electrical_nets_empty_board(self) -> None:
        board = _perfboard(2, 2)
        lay = Layout(placements={}, wires=[])
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        nets = graph.electrical_nets()
        # 4 isolated pads → 4 nets of 0 pins each (no components placed).
        assert len(nets) == 4

    def test_wire_creates_one_net_from_two_pads(self) -> None:
        # Pad centers: (0.5,0) and (1.5,0).
        board = _perfboard(1, 2)
        wire = WireSegment(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(1.5, 0.5)],
            insulated=False,
            side="top",
        )
        lay = Layout(placements={}, wires=[wire])
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        nets = graph.electrical_nets()
        assert len(nets) == 1


class TestStripboardConnectivity:
    def test_strip_connects_all_holes(self) -> None:
        from protoboard.domain.board.boxes import StripAtom

        strip = StripAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            length=4,
            orientation="horizontal",
            layers=PadLayers(top=False, bottom=True),
        )
        board = Board(
            root=VBox(
                width_policy=SizePolicy.HUG,
                height_policy=SizePolicy.HUG,
                children=[strip],
            )
        )
        lay = Layout(placements={}, wires=[])
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        # All 4 holes in the strip share one electrical node.
        assert graph.component_count() == 1
