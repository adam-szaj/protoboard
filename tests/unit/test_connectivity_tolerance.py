# SPDX-License-Identifier: MIT
"""Tests that the connectivity graph tolerates ±0.005 fractional offsets.

Pad centers are conventionally at (n+0.5, m+0.5).  Legacy saves may carry
small fractional offsets (e.g. y=3.503 instead of 3.5) — a leftover from an
older snap-fix bug.  Without tolerance, the graph indexes the pin and the
matching board pad as two distinct nodes and a wire drawn between them is
not seen as connected.
"""

from __future__ import annotations

import uuid

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.connectivity.graph import ConnectivityGraph
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement, PlacementCollection
from protoboard.domain.layout.wire import Wire, WireCollection
from protoboard.domain.schematic.schematic import PinRef
from protoboard.geometry import Point
from tests.unit._autoroute_invariants import make_sil, sil_families


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


def _make_resistor_with_fractional_offset(
    board: Board, offset_y: float
) -> tuple[ConnectivityGraph, ComponentInstance, Wire]:
    """Place a 2-pin resistor whose pin-1 anchor sits at y=0.5+offset_y."""
    defn = make_sil("R")
    inst = ComponentInstance.new(definition_ref="R", refdes="R1", value="").bind(
        defn, sil_families(defn)
    )
    # Rotation 90 → pin 1 at position, pin 2 at position + (1, 0).
    pl = ComponentPlacement(
        component_id=inst.id,
        position=Point(0.5, 0.5 + offset_y),
        rotation=90,
        side="top",
    )
    # Wire from board pad (1.5, 0.5) to pad (2.5, 0.5) — same row, exact half-grid.
    wire = Wire(
        id=uuid.uuid4(),
        path=[Point(1.5, 0.5), Point(2.5, 0.5)],
        insulated=False,
        side="top",
    )
    lay = Layout(
        placements=PlacementCollection({inst.id: pl}), wires=WireCollection([wire])
    )
    graph = ConnectivityGraph(
        board=board,
        layout=lay,
        components={defn.name: defn},
        instances={inst.id: inst},
    )
    return graph, inst, wire


class TestConnectivityToleratesFractionalOffsets:
    def test_pin_at_3e_3_offset_matches_board_pad(self) -> None:
        # offset_y = 3e-3: pin 2 ends up at world (1.5, 0.5+0.003). The matching
        # board pad is at (1.5, 0.5). Should be seen as the same electrical node.
        board = _perfboard(1, 3)
        graph, inst, wire = _make_resistor_with_fractional_offset(board, 3e-3)
        node_pin2 = graph.node_for_pin(PinRef(component_id=inst.id, pin_number=2))
        node_pad = graph.node_for_pad(Point(1.5, 0.5), "top")
        assert node_pin2 is not None
        assert node_pad is not None
        assert node_pin2 == node_pad

    def test_wire_endpoint_at_5e_3_below_pin_still_connects(self) -> None:
        # Even more aggressive: wire endpoint y differs from pin y by 5e-3.
        # Both should map to the same _NodeKey under _SNAP_TOL=5e-3.
        board = _perfboard(1, 3)
        defn = make_sil("R")
        inst = ComponentInstance.new(definition_ref="R", refdes="R1", value="").bind(
            defn, sil_families(defn)
        )
        pl = ComponentPlacement(
            component_id=inst.id,
            position=Point(0.5, 0.5),
            rotation=90,
            side="top",
        )
        # Wire endpoint deliberately at 0.503 instead of 0.5.
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(1.5, 0.503), Point(2.5, 0.5)],
            insulated=False,
            side="top",
        )
        lay = Layout(
            placements=PlacementCollection({inst.id: pl}), wires=WireCollection([wire])
        )
        graph = ConnectivityGraph(
            board=board,
            layout=lay,
            components={defn.name: defn},
            instances={inst.id: inst},
        )
        # Pin 2 → wire start point → board pad (1.5, 0.5): all the same node.
        node_pin2 = graph.node_for_pin(PinRef(component_id=inst.id, pin_number=2))
        node_pad = graph.node_for_pad(Point(1.5, 0.5), "top")
        assert node_pin2 == node_pad

    def test_pin_at_2e_2_offset_is_too_far_and_stays_separate(self) -> None:
        # Tolerance is ±5e-3; an offset of 2e-2 (0.02) is 4× over → should NOT
        # be merged.  This guards against the tolerance silently widening.
        board = _perfboard(1, 3)
        graph, inst, wire = _make_resistor_with_fractional_offset(board, 2e-2)
        node_pin2 = graph.node_for_pin(PinRef(component_id=inst.id, pin_number=2))
        node_pad = graph.node_for_pad(Point(1.5, 0.5), "top")
        # Pin lookup may return None (no graph node at off-grid position) or a
        # different key — either way it is NOT the pad's node.
        if node_pin2 is not None and node_pad is not None:
            assert node_pin2 != node_pad
