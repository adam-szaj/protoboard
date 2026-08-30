# SPDX-License-Identifier: MIT
"""Tests for the enhanced at-pad penalty (Feature A).

The cost-model fields ``at_pad_insulated_insulated_same_side`` and
``at_pad_insulated_bare_same_side`` add a per-step penalty when A*
considers a pad that already hosts another wire's vertex on the same
side.  Defaults are 0 → no behavioural change vs the legacy router.

These tests exercise both the bookkeeping helper
(``_build_at_pad_wire_index``) and the hot-loop integration (penalty
visible in routed cost / chosen geometry).
"""

from __future__ import annotations

import uuid

from protoboard.domain.layout.wire import Wire
from protoboard.geometry import Point
from protoboard.services.routing.autorouter import _build_at_pad_wire_index
from protoboard.services.routing.cost_model import RoutingCostModel

# ---------------------------------------------------------------------------
# _build_at_pad_wire_index — pure helper, no Qt / board needed
# ---------------------------------------------------------------------------


def _wire(insulated: bool, side: str, *pts: tuple[float, float]) -> Wire:
    return Wire(
        id=uuid.uuid4(),
        path=[Point(x, y) for x, y in pts],
        insulated=insulated,
        side=side,  # type: ignore[arg-type]
    )


class TestAtPadWireIndex:
    def test_empty_input_returns_empty_dict(self) -> None:
        bm, nets = _build_at_pad_wire_index([])
        assert bm == {}
        assert nets == {}

    def test_single_insulated_wire_sets_bit_1(self) -> None:
        w = _wire(True, "top", (0.5, 0.5), (1.5, 0.5), (2.5, 0.5))
        idx, _nets = _build_at_pad_wire_index([w])
        assert idx == {
            (Point(0.5, 0.5), "top"): 1,
            (Point(1.5, 0.5), "top"): 1,
            (Point(2.5, 0.5), "top"): 1,
        }

    def test_single_bare_wire_sets_bit_2(self) -> None:
        w = _wire(False, "bottom", (0.5, 0.5), (1.5, 0.5))
        idx, _nets = _build_at_pad_wire_index([w])
        assert idx == {
            (Point(0.5, 0.5), "bottom"): 2,
            (Point(1.5, 0.5), "bottom"): 2,
        }

    def test_mixed_wires_on_same_pad_or_bits(self) -> None:
        """Insulated + bare on same (pad, side) → bitmask 3 (=1|2)."""
        w_ins = _wire(True, "top", (0.5, 0.5), (1.5, 0.5))
        w_bare = _wire(False, "top", (1.5, 0.5), (2.5, 0.5))
        idx, _nets = _build_at_pad_wire_index([w_ins, w_bare])
        # (1.5, 0.5) on top hosts both → bit 1 | bit 2 = 3
        assert idx[(Point(1.5, 0.5), "top")] == 3
        # (0.5, 0.5) only insulated → 1
        assert idx[(Point(0.5, 0.5), "top")] == 1
        # (2.5, 0.5) only bare → 2
        assert idx[(Point(2.5, 0.5), "top")] == 2

    def test_different_sides_do_not_combine(self) -> None:
        """Same (x,y) but different sides → separate entries."""
        w_top = _wire(True, "top", (0.5, 0.5), (1.5, 0.5))
        w_bot = _wire(False, "bottom", (0.5, 0.5), (1.5, 0.5))
        idx, _nets = _build_at_pad_wire_index([w_top, w_bot])
        assert idx[(Point(0.5, 0.5), "top")] == 1
        assert idx[(Point(0.5, 0.5), "bottom")] == 2
        assert (Point(0.5, 0.5), "top") in idx
        assert (Point(0.5, 0.5), "bottom") in idx

    def test_net_ids_tracked_when_provided(self) -> None:
        """When wire_to_net_id is provided, net_index gathers nets per pad."""
        w1 = _wire(False, "top", (0.5, 0.5), (1.5, 0.5))
        w2 = _wire(True, "top", (1.5, 0.5), (2.5, 0.5))
        net_a = uuid.uuid4()
        net_b = uuid.uuid4()
        _bm, nets = _build_at_pad_wire_index([w1, w2], {w1.id: net_a, w2.id: net_b})
        # Shared pad (1.5, 0.5) hosts both nets.
        assert nets[(Point(1.5, 0.5), "top")] == frozenset({net_a, net_b})
        # Lone pads see only one net.
        assert nets[(Point(0.5, 0.5), "top")] == frozenset({net_a})
        assert nets[(Point(2.5, 0.5), "top")] == frozenset({net_b})


# ---------------------------------------------------------------------------
# Cost-model defaults
# ---------------------------------------------------------------------------


class TestCostModelDefaults:
    def test_at_pad_fields_default_to_zero(self) -> None:
        cm = RoutingCostModel()
        assert cm.at_pad_insulated_insulated_same_side == 0.0
        assert cm.at_pad_insulated_bare_same_side == 0.0

    def test_at_pad_fields_are_settable(self) -> None:
        cm = RoutingCostModel(
            at_pad_insulated_insulated_same_side=1.5,
            at_pad_insulated_bare_same_side=4.0,
        )
        assert cm.at_pad_insulated_insulated_same_side == 1.5
        assert cm.at_pad_insulated_bare_same_side == 4.0


# ---------------------------------------------------------------------------
# Hot-loop integration — via find_paths() directly on a tiny graph
# ---------------------------------------------------------------------------


def _tiny_graph_5x5():
    """Return a ConnectivityGraph for a 5×5 perfboard, no components."""
    from protoboard.domain.board.board import Board
    from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
    from protoboard.domain.board.pads import PadLayers
    from protoboard.domain.connectivity.graph import ConnectivityGraph
    from protoboard.domain.layout.layout import Layout
    from protoboard.domain.schematic.schematic import Schematic

    board = Board(
        root=BoardGrid(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            cell=PadAtom(
                width_policy=SizePolicy.HUG,
                height_policy=SizePolicy.HUG,
                layers=PadLayers(top=True, bottom=False, plated_through=False),
            ),
            rows=5,
            cols=5,
            row_gap=0.0,
            col_gap=0.0,
        )
    )
    layout = Layout()
    Schematic(nets={})  # noqa: F841 — kept for parity if helpers expand later
    graph = ConnectivityGraph(board, layout, {}, {})
    return board, graph


class TestHotLoopPenaltyFires:
    """When ``at_pad_insulated_insulated_same_side`` > 0 and the index
    flags a pad as already occupied by an insulated wire vertex, the
    A* result cost increases by that amount per crossed pad.
    """

    def test_penalty_zero_same_path_same_cost(self) -> None:
        """Defaults (penalty=0) → cost identical with or without index."""
        from protoboard.services.routing.astar import find_paths

        _, graph = _tiny_graph_5x5()
        cm = RoutingCostModel()  # defaults

        baseline = find_paths(
            graph=graph,
            source=Point(0.5, 0.5),
            targets=[Point(4.5, 0.5)],
            side="top",
            cost_model=cm,
            k=1,
            insulated=True,
        )
        with_idx = find_paths(
            graph=graph,
            source=Point(0.5, 0.5),
            targets=[Point(4.5, 0.5)],
            side="top",
            cost_model=cm,
            k=1,
            insulated=True,
            at_pad_wires={(Point(2.5, 0.5), "top"): 1},
        )
        assert baseline and with_idx
        assert baseline[0].cost == with_idx[0].cost

    def test_penalty_positive_increases_cost(self) -> None:
        """With penalty > 0 and a flagged pad on the cheapest path, cost rises."""
        from protoboard.services.routing.astar import find_paths

        _, graph = _tiny_graph_5x5()

        cm_pen = RoutingCostModel(at_pad_insulated_insulated_same_side=10.0)

        baseline = find_paths(
            graph=graph,
            source=Point(0.5, 0.5),
            targets=[Point(4.5, 0.5)],
            side="top",
            cost_model=RoutingCostModel(),  # no penalty
            k=1,
            insulated=True,
        )
        # Same scenario but every pad on the direct row is "occupied" by
        # an insulated wire vertex.  The router still has to walk through
        # them (no alternative cheap path on a 5×1 corridor), so it pays
        # the penalty per pad.
        with_idx = find_paths(
            graph=graph,
            source=Point(0.5, 0.5),
            targets=[Point(4.5, 0.5)],
            side="top",
            cost_model=cm_pen,
            k=1,
            insulated=True,
            at_pad_wires={
                (Point(1.5, 0.5), "top"): 1,
                (Point(2.5, 0.5), "top"): 1,
                (Point(3.5, 0.5), "top"): 1,
                (Point(4.5, 0.5), "top"): 1,
            },
        )
        assert baseline and with_idx
        # Cost must rise; magnitude depends on how many penalty pads
        # the path traverses.  Penalty = 10/pad ⇒ at minimum one pad.
        assert with_idx[0].cost > baseline[0].cost

    def test_new_bare_over_existing_bare_at_pad_blocks_step(self) -> None:
        """Bare × bare at a pad must be rejected (electrical short).

        The enhanced helper treats this as ``continue`` — equivalent to
        ``inf`` — so the new bare wire either detours or fails to find
        a path when the only route runs through the flagged pad.
        """
        from protoboard.services.routing.astar import find_paths

        _, graph = _tiny_graph_5x5()

        cm = RoutingCostModel(at_pad_insulated_bare_same_side=1.0)
        # Flag the centre pad as already hosting a bare-wire vertex.
        idx = {(Point(2.5, 0.5), "top"): 2}

        # New wire is bare; only path on this row goes through (2.5, 0.5).
        # The hot loop should refuse that step → reroute via off-row pads.
        results = find_paths(
            graph=graph,
            source=Point(0.5, 0.5),
            targets=[Point(4.5, 0.5)],
            side="top",
            cost_model=cm,
            k=1,
            insulated=False,  # NEW wire is bare
            at_pad_wires=idx,
        )
        # The 5×5 board allows detour via y=1.5, so a path exists, but
        # it must NOT contain (2.5, 0.5).
        assert results
        path_pts = {(p.x, p.y) for p, _ in results[0].path}
        assert (2.5, 0.5) not in path_pts
