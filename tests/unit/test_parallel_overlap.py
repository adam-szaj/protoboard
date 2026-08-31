# SPDX-License-Identifier: MIT
"""Tests for the parallel-opposite-side penalty (Feature B).

The cost-model field ``parallel_opposite_side`` adds a per-step penalty
when the new wire's step is collinear with — and overlaps the interior
of — an existing wire segment on the *opposite* side.  Applies only
when at least one of the two wires is insulated; bare×bare opposite-side
is electrically fine and incurs no cost.
"""

from __future__ import annotations

from protoboard.domain.routing.cost_model import RoutingCostModel
from protoboard.geometry import Point


def _tiny_graph_5x5():
    from protoboard.domain.board.board import Board
    from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
    from protoboard.domain.board.pads import PadLayers
    from protoboard.domain.connectivity.graph import ConnectivityGraph
    from protoboard.domain.layout.layout import Layout

    board = Board(
        root=BoardGrid(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            cell=PadAtom(
                width_policy=SizePolicy.HUG,
                height_policy=SizePolicy.HUG,
                layers=PadLayers(top=True, bottom=True, plated_through=True),
            ),
            rows=5,
            cols=5,
            row_gap=0.0,
            col_gap=0.0,
        )
    )
    layout = Layout()
    graph = ConnectivityGraph(board, layout, {}, {})
    return board, graph


class TestCostModelDefault:
    def test_parallel_opposite_side_defaults_to_zero(self) -> None:
        assert RoutingCostModel().parallel_opposite_side == 0.0

    def test_field_is_settable(self) -> None:
        cm = RoutingCostModel(parallel_opposite_side=0.5)
        assert cm.parallel_opposite_side == 0.5


class TestHotLoopPenaltyFires:
    def test_disabled_when_zero(self) -> None:
        """Penalty=0 → cost identical with or without all_segments."""
        from protoboard.services.routing.astar import find_paths

        _, graph = _tiny_graph_5x5()
        cm = RoutingCostModel()
        opp_segs = [
            (Point(0.5, 0.5), Point(4.5, 0.5), "bottom", True),
        ]
        baseline = find_paths(
            graph=graph,
            source=Point(0.5, 0.5),
            targets=[Point(4.5, 0.5)],
            side="top",
            cost_model=cm,
            k=1,
            insulated=True,
        )
        with_segs = find_paths(
            graph=graph,
            source=Point(0.5, 0.5),
            targets=[Point(4.5, 0.5)],
            side="top",
            cost_model=cm,
            k=1,
            insulated=True,
            all_segments=opp_segs,
        )
        assert baseline and with_segs
        assert baseline[0].cost == with_segs[0].cost

    def test_insulated_over_insulated_opposite_side_pays_penalty(self) -> None:
        """Top insulated runs over a bottom insulated trace on the same
        row → penalty fires per overlapping step.
        """
        from protoboard.services.routing.astar import find_paths

        _, graph = _tiny_graph_5x5()
        cm_pen = RoutingCostModel(parallel_opposite_side=2.0)
        # Existing insulated wire on the BOTTOM, full row at y=0.5.
        opp_segs = [
            (Point(0.5, 0.5), Point(4.5, 0.5), "bottom", True),
        ]
        baseline = find_paths(
            graph=graph,
            source=Point(0.5, 0.5),
            targets=[Point(4.5, 0.5)],
            side="top",
            cost_model=RoutingCostModel(),
            k=1,
            insulated=True,
        )
        with_pen = find_paths(
            graph=graph,
            source=Point(0.5, 0.5),
            targets=[Point(4.5, 0.5)],
            side="top",
            cost_model=cm_pen,
            k=1,
            insulated=True,
            all_segments=opp_segs,
        )
        assert baseline and with_pen
        # Penalty must drive the cost higher (router can't get off the
        # only 4-step straight line without going off-row at extra cost).
        assert with_pen[0].cost > baseline[0].cost

    def test_bare_over_bare_opposite_side_no_penalty(self) -> None:
        """Bare×bare opposite-side incurs no penalty by spec."""
        from protoboard.services.routing.astar import find_paths

        _, graph = _tiny_graph_5x5()
        cm_pen = RoutingCostModel(parallel_opposite_side=2.0)
        opp_segs = [
            (Point(0.5, 0.5), Point(4.5, 0.5), "bottom", False),
        ]
        baseline = find_paths(
            graph=graph,
            source=Point(0.5, 0.5),
            targets=[Point(4.5, 0.5)],
            side="top",
            cost_model=RoutingCostModel(),
            k=1,
            insulated=False,
        )
        with_pen = find_paths(
            graph=graph,
            source=Point(0.5, 0.5),
            targets=[Point(4.5, 0.5)],
            side="top",
            cost_model=cm_pen,
            k=1,
            insulated=False,
            all_segments=opp_segs,
        )
        assert baseline and with_pen
        # Both new wire and existing are bare → no penalty applied.
        assert with_pen[0].cost == baseline[0].cost

    def test_same_side_segments_ignored(self) -> None:
        """A segment on the SAME side must not trigger the opposite-side
        penalty; it's the wrong cost knob for that case (it would clash
        with the bare-wire crossing model).
        """
        from protoboard.services.routing.astar import find_paths

        _, graph = _tiny_graph_5x5()
        cm_pen = RoutingCostModel(parallel_opposite_side=2.0)
        # Existing insulated wire on the SAME (top) side at y=0.5.
        same_side_segs = [
            (Point(0.5, 0.5), Point(4.5, 0.5), "top", True),
        ]
        baseline = find_paths(
            graph=graph,
            source=Point(0.5, 0.5),
            targets=[Point(4.5, 0.5)],
            side="top",
            cost_model=RoutingCostModel(),
            k=1,
            insulated=True,
        )
        with_pen = find_paths(
            graph=graph,
            source=Point(0.5, 0.5),
            targets=[Point(4.5, 0.5)],
            side="top",
            cost_model=cm_pen,
            k=1,
            insulated=True,
            all_segments=same_side_segs,
        )
        assert baseline and with_pen
        assert with_pen[0].cost == baseline[0].cost

    def test_non_overlapping_segments_ignored(self) -> None:
        """An opposite-side segment that does NOT overlap the step must
        not contribute to the penalty."""
        from protoboard.services.routing.astar import find_paths

        _, graph = _tiny_graph_5x5()
        cm_pen = RoutingCostModel(parallel_opposite_side=2.0)
        # Bottom segment on a DIFFERENT row (y=2.5).
        opp_segs = [
            (Point(0.5, 2.5), Point(4.5, 2.5), "bottom", True),
        ]
        baseline = find_paths(
            graph=graph,
            source=Point(0.5, 0.5),
            targets=[Point(4.5, 0.5)],
            side="top",
            cost_model=RoutingCostModel(),
            k=1,
            insulated=True,
        )
        with_pen = find_paths(
            graph=graph,
            source=Point(0.5, 0.5),
            targets=[Point(4.5, 0.5)],
            side="top",
            cost_model=cm_pen,
            k=1,
            insulated=True,
            all_segments=opp_segs,
        )
        assert baseline and with_pen
        assert with_pen[0].cost == baseline[0].cost
