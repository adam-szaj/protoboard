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
from protoboard.domain.layout.wire import Wire, WireCollection
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
        lay = Layout(placements={}, wires=WireCollection([]))
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        # 4 pads, plated-through → each pad has top+bottom nodes connected by an
        # edge (2 nodes per pad = 8 total), but no wire connects different pads
        # → 4 separate electrical components.
        assert graph.node_count() == 8
        assert graph.component_count() == 4

    def test_wire_joins_two_pads(self) -> None:
        # Perfboard 1×3 with gap=0: pad centers at (0.5,0), (1.5,0), (2.5,0).
        board = _perfboard(1, 3)
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(1.5, 0.5)],
            insulated=False,
            side="top",
        )
        lay = Layout(placements={}, wires=WireCollection([wire]))
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        # 3 pads; pad at (0.5,0) and pad at (1.5,0) connected → 2 components.
        assert graph.component_count() == 2

    def test_plated_through_merges_top_and_bottom(self) -> None:
        # A perfboard pad with plated=True: top and bottom are electrically one
        # component (connected by an edge), but represented as 2 graph nodes.
        board = _perfboard(1, 1)
        lay = Layout(placements={}, wires=WireCollection([]))
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        assert graph.node_count() == 2  # one top node + one bottom node
        assert graph.component_count() == 1  # electrically one component

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
        lay = Layout(placements={}, wires=WireCollection([]))
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        assert graph.node_count() == 2


class TestConnectivityGraphWithComponents:
    def test_placed_component_pins_on_pads(self) -> None:
        # SIL(2) places pins vertically: pin1 at position, pin2 at position+(0,1).
        # Use a 2-row × 1-col board so both pads exist.
        # gap=0 → pad centers at (0.5, 0.5) and (0.5, 1.5).
        board = _perfboard(2, 1)
        comp_defn = ComponentDefinition.from_footprint(
            name="R",
            footprint=SIL(2),
            pin_names={1: "A", 2: "B"},
        )
        inst = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
        # Place pin1 on pad (0.5, 0.5), pin2 on pad (0.5, 1.5).
        pl = ComponentPlacement(
            component_id=inst.id,
            position=Point(0.5, 0.5),
            rotation=0,
            side="top",
        )
        lay = Layout(placements={inst.id: pl}, wires=WireCollection([]))
        graph = ConnectivityGraph(
            board=board,
            layout=lay,
            components={comp_defn.name: comp_defn},
            instances={inst.id: inst},
        )
        node_pin1 = graph.node_for_pin(PinRef(component_id=inst.id, pin_number=1))
        node_pin2 = graph.node_for_pin(PinRef(component_id=inst.id, pin_number=2))
        assert node_pin1 is not None
        assert node_pin2 is not None
        assert node_pin1 != node_pin2

    def test_wire_joins_component_pins(self) -> None:
        # Pad centers: (0.5,0), (1.5,0), (2.5,0).
        board = _perfboard(1, 3)
        comp_defn = ComponentDefinition.from_footprint(
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
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(1.5, 0.5), Point(2.5, 0.5)],
            insulated=False,
            side="top",
        )
        lay = Layout(placements={inst.id: pl}, wires=WireCollection([wire]))
        graph = ConnectivityGraph(
            board=board,
            layout=lay,
            components={comp_defn.name: comp_defn},
            instances={inst.id: inst},
        )
        node_pin2 = graph.node_for_pin(PinRef(component_id=inst.id, pin_number=2))
        node_pad3 = graph.node_for_pad(Point(2.5, 0.5), "top")
        assert node_pin2 == node_pad3


class TestInternalNetsSameNamePins:
    """Pins of one component sharing a name are one electrical node — a
    pre-routed internal net island — with no wire drawn between them."""

    def _ic_4pin(self, pin_names: dict[int, str]) -> ComponentDefinition:
        return ComponentDefinition.from_footprint(
            name="IC", footprint=SIL(4), pin_names=pin_names
        )

    def test_same_name_pins_share_node_without_wire(self) -> None:
        # SIL(4) vertical on a 4×1 board: pins at y=0.5,1.5,2.5,3.5 (x=0.5).
        # pin1 and pin3 are both "GND" → one electrical node, no wire.
        board = _perfboard(4, 1)
        defn = self._ic_4pin({1: "GND", 2: "A", 3: "GND", 4: "B"})
        inst = ComponentInstance.new(definition_ref="IC", refdes="U1", value="")
        pl = ComponentPlacement(
            component_id=inst.id, position=Point(0.5, 0.5), rotation=0, side="top"
        )
        lay = Layout(placements={inst.id: pl}, wires=WireCollection([]))
        graph = ConnectivityGraph(
            board=board, layout=lay,
            components={defn.name: defn}, instances={inst.id: inst},
        )
        node_pin1 = graph.node_for_pin(PinRef(component_id=inst.id, pin_number=1))
        node_pin3 = graph.node_for_pin(PinRef(component_id=inst.id, pin_number=3))
        assert node_pin1 is not None
        assert node_pin1 == node_pin3, "same-name pins must be one node"

    def test_differently_named_pins_stay_separate(self) -> None:
        board = _perfboard(4, 1)
        defn = self._ic_4pin({1: "GND", 2: "A", 3: "GND", 4: "B"})
        inst = ComponentInstance.new(definition_ref="IC", refdes="U1", value="")
        pl = ComponentPlacement(
            component_id=inst.id, position=Point(0.5, 0.5), rotation=0, side="top"
        )
        lay = Layout(placements={inst.id: pl}, wires=WireCollection([]))
        graph = ConnectivityGraph(
            board=board, layout=lay,
            components={defn.name: defn}, instances={inst.id: inst},
        )
        node_gnd = graph.node_for_pin(PinRef(component_id=inst.id, pin_number=1))
        node_a = graph.node_for_pin(PinRef(component_id=inst.id, pin_number=2))
        node_b = graph.node_for_pin(PinRef(component_id=inst.id, pin_number=4))
        assert node_a != node_gnd
        assert node_b != node_gnd
        assert node_a != node_b

    def test_connecting_one_gnd_connects_all(self) -> None:
        # Wire from an external pad to pin1 (GND); pin3 (GND) must end up in the
        # same electrical set without its own wire — the pre-routed island.
        board = _perfboard(4, 2)  # 2 cols: col x=0.5 holds the IC, col x=1.5 free
        defn = self._ic_4pin({1: "GND", 2: "A", 3: "GND", 4: "B"})
        inst = ComponentInstance.new(definition_ref="IC", refdes="U1", value="")
        pl = ComponentPlacement(
            component_id=inst.id, position=Point(0.5, 0.5), rotation=0, side="top"
        )
        # Wire connects free pad (1.5,0.5) to pin1 pad (0.5,0.5).
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(1.5, 0.5), Point(0.5, 0.5)],
            insulated=False, side="top",
        )
        lay = Layout(placements={inst.id: pl}, wires=WireCollection([wire]))
        graph = ConnectivityGraph(
            board=board, layout=lay,
            components={defn.name: defn}, instances={inst.id: inst},
        )
        node_free = graph.node_for_pad(Point(1.5, 0.5), "top")
        node_pin3 = graph.node_for_pin(PinRef(component_id=inst.id, pin_number=3))
        assert node_free is not None
        # pin3 (GND, no wire) is on the same net as the wired free pad.
        assert node_pin3 == node_free

    def test_single_named_pin_not_merged(self) -> None:
        # Only one "GND" pin → nothing to merge; it stays its own node.
        board = _perfboard(4, 1)
        defn = self._ic_4pin({1: "GND", 2: "A", 3: "B", 4: "C"})
        inst = ComponentInstance.new(definition_ref="IC", refdes="U1", value="")
        pl = ComponentPlacement(
            component_id=inst.id, position=Point(0.5, 0.5), rotation=0, side="top"
        )
        lay = Layout(placements={inst.id: pl}, wires=WireCollection([]))
        graph = ConnectivityGraph(
            board=board, layout=lay,
            components={defn.name: defn}, instances={inst.id: inst},
        )
        # Each pin on its own node — 4 distinct.
        nodes = {
            graph.node_for_pin(PinRef(component_id=inst.id, pin_number=p))
            for p in (1, 2, 3, 4)
        }
        assert len(nodes) == 4


class TestElectricalNets:
    def test_electrical_nets_empty_board(self) -> None:
        board = _perfboard(2, 2)
        lay = Layout(placements={}, wires=WireCollection([]))
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        nets = graph.electrical_nets()
        # 4 isolated pads → 4 nets of 0 pins each (no components placed).
        assert len(nets) == 4

    def test_wire_creates_one_net_from_two_pads(self) -> None:
        # Pad centers: (0.5,0) and (1.5,0).
        board = _perfboard(1, 2)
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(1.5, 0.5)],
            insulated=False,
            side="top",
        )
        lay = Layout(placements={}, wires=WireCollection([wire]))
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        nets = graph.electrical_nets()
        assert len(nets) == 1


class TestInsulatedWireIsolation:
    """Insulated wires must not electrically connect pads they cross over."""

    def test_insulated_wire_does_not_merge_intermediate_pads(self) -> None:
        # 1×3 perfboard, pads at x=0.5, 1.5, 2.5 (y=0.5).
        # An insulated wire runs from (0.5,0.5) to (2.5,0.5), crossing over the
        # middle pad at (1.5,0.5).  The middle pad must remain in its own set.
        board = _perfboard(1, 3)
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(1.5, 0.5), Point(2.5, 0.5)],
            insulated=True,
            side="top",
        )
        lay = Layout(placements={}, wires=WireCollection([wire]))
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        # Wire endpoints (0.5,0.5) and (2.5,0.5) should be in the same set.
        # Middle pad (1.5,0.5) must be in a DIFFERENT set.
        node_left = graph.node_for_pad(Point(0.5, 0.5), "top")
        node_mid = graph.node_for_pad(Point(1.5, 0.5), "top")
        node_right = graph.node_for_pad(Point(2.5, 0.5), "top")
        assert node_left is not None
        assert node_mid is not None
        assert node_right is not None
        assert node_left == node_right, "Wire endpoints must be electrically connected"
        assert node_mid != node_left, "Crossed pad must NOT be merged into wire's set"

    def test_two_insulated_wires_crossing_same_pad_stay_isolated(self) -> None:
        # 1×3 perfboard. Two insulated wires both pass through (1.5,0.5).
        # They must NOT be merged together via the shared intermediate pad.
        board = _perfboard(1, 3)
        wire_a = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(1.5, 0.5), Point(2.5, 0.5)],
            insulated=True,
            side="top",
        )
        wire_b = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(1.5, 0.5), Point(2.5, 0.5)],
            insulated=True,
            side="top",
        )
        lay = Layout(placements={}, wires=WireCollection([wire_a, wire_b]))
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        # Middle pad must still be isolated from the wire endpoints.
        node_mid = graph.node_for_pad(Point(1.5, 0.5), "top")
        node_end = graph.node_for_pad(Point(0.5, 0.5), "top")
        assert node_mid != node_end, "Intermediate pad must not merge with endpoints"

    def test_bare_wire_does_connect_intermediate_pads(self) -> None:
        # Sanity check: a bare (non-insulated) wire crossing a pad SHOULD merge it.
        board = _perfboard(1, 3)
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(1.5, 0.5), Point(2.5, 0.5)],
            insulated=False,
            side="top",
        )
        lay = Layout(placements={}, wires=WireCollection([wire]))
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        node_left = graph.node_for_pad(Point(0.5, 0.5), "top")
        node_mid = graph.node_for_pad(Point(1.5, 0.5), "top")
        node_right = graph.node_for_pad(Point(2.5, 0.5), "top")
        assert node_left == node_mid == node_right, "Bare wire merges all pads"

    def test_two_bare_wires_crossing_off_pad_stay_isolated(self) -> None:
        # 2×2 perfboard, pads at (0.5,0.5), (1.5,0.5), (0.5,1.5), (1.5,1.5).
        # Wire A: (0.5,0.5) → (1.0,0.5) → (1.5,0.5)  — mid-step at (1.0,0.5)
        # Wire B: (0.5,1.5) → (1.0,0.5) → (1.5,1.5)  — same off-pad mid-step
        # Both are bare.  They share the off-pad point (1.0,0.5) but that is
        # NOT a board pad, so they must NOT be merged electrically.
        board = _perfboard(2, 2)
        wire_a = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(1.0, 0.5), Point(1.5, 0.5)],
            insulated=False,
            side="top",
        )
        wire_b = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 1.5), Point(1.0, 0.5), Point(1.5, 1.5)],
            insulated=False,
            side="top",
        )
        lay = Layout(placements={}, wires=WireCollection([wire_a, wire_b]))
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        node_a_left = graph.node_for_pad(Point(0.5, 0.5), "top")
        node_b_left = graph.node_for_pad(Point(0.5, 1.5), "top")
        assert node_a_left != node_b_left, (
            "Bare wires sharing an off-pad crossing point must remain in separate ElectricSets"
        )

    def test_bare_wire_shortcut_connects_crossed_pads(self) -> None:
        # 1×4 perfboard.  A bare wire is laid as a direct shortcut from the
        # leftmost pad to the rightmost pad — without explicit vertices on the
        # two intermediate pads it physically rests on.  Connectivity must
        # still merge all four pads (the bare conductor contacts every pad it
        # crosses).
        board = _perfboard(1, 4)
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(3.5, 0.5)],  # no intermediate vertices
            insulated=False,
            side="top",
        )
        lay = Layout(placements={}, wires=WireCollection([wire]))
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        nodes = [
            graph.node_for_pad(Point(x + 0.5, 0.5), "top") for x in range(4)
        ]
        for n in nodes:
            assert n is not None
        assert len(set(nodes)) == 1, (
            "Bare shortcut must merge every pad on its physical path"
        )

    def test_insulated_wire_shortcut_does_not_connect_crossed_pads(self) -> None:
        # Mirror of the above: insulated wire shortcut must NOT merge crossed
        # pads — the conductor floats above the board.
        board = _perfboard(1, 4)
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(3.5, 0.5)],
            insulated=True,
            side="top",
        )
        lay = Layout(placements={}, wires=WireCollection([wire]))
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        node_left = graph.node_for_pad(Point(0.5, 0.5), "top")
        node_mid1 = graph.node_for_pad(Point(1.5, 0.5), "top")
        node_mid2 = graph.node_for_pad(Point(2.5, 0.5), "top")
        node_right = graph.node_for_pad(Point(3.5, 0.5), "top")
        assert node_left == node_right, "Insulated endpoints connect (soldered)"
        assert node_mid1 != node_left, "Floating insulated wire must not touch mid pads"
        assert node_mid2 != node_left, "Floating insulated wire must not touch mid pads"

    def test_insulated_wire_with_explicit_pad_vertex_still_isolates(self) -> None:
        # Confirms the existing rule under the new bare-aware enrichment:
        # an INSULATED wire with an explicit vertex coinciding with a board
        # pad must still NOT connect that pad — insulation is the source of
        # truth, vertex coincidence is irrelevant.  Re-asserts the principle
        # already covered by test_insulated_wire_does_not_merge_intermediate_pads
        # but explicitly in the context of the bare-enrichment fix.
        board = _perfboard(1, 3)
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(1.5, 0.5), Point(2.5, 0.5)],
            insulated=True,
            side="top",
        )
        lay = Layout(placements={}, wires=WireCollection([wire]))
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        node_left = graph.node_for_pad(Point(0.5, 0.5), "top")
        node_mid = graph.node_for_pad(Point(1.5, 0.5), "top")
        assert node_left is not None and node_mid is not None
        assert node_mid != node_left


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
        lay = Layout(placements={}, wires=WireCollection([]))
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        # All 4 holes in the strip share one electrical node.
        assert graph.component_count() == 1
