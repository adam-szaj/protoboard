# SPDX-License-Identifier: MIT
"""Tests for the A* routing engine and RoutingCostModel.

Coordinate convention: pad centers at (col+0.5, row+0.5) for perfboard
with gap=0. Routing operates on the set of nodes from ConnectivityGraph.
"""

import uuid

import pytest

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.connectivity.graph import ConnectivityGraph
from protoboard.domain.layout.layout import Layout
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.geometry import Point
from protoboard.services.routing.astar import RoutingResult, find_paths
from protoboard.services.routing.cost_model import RoutingCostModel


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


class TestRoutingCostModel:
    def test_defaults(self) -> None:
        model = RoutingCostModel()
        assert model.free_pad > 0
        assert model.same_net_pad < model.free_pad  # encourages joining nets
        assert model.occupied_by_other_net == float("inf")
        assert model.direction_change > 0

    def test_custom_values(self) -> None:
        model = RoutingCostModel(free_pad=2.0, direction_change=0.5)
        assert model.free_pad == 2.0
        assert model.direction_change == 0.5

    def test_frozen(self) -> None:
        import dataclasses

        model = RoutingCostModel()
        with pytest.raises(dataclasses.FrozenInstanceError):
            model.free_pad = 99.0  # type: ignore[misc]


class TestFindPathsBasic:
    def test_direct_neighbour(self) -> None:
        # 1×3 board; source=(0.5,0.5), target=(1.5,0.5) — one step right.
        board = _perfboard(1, 3)
        lay = Layout(placements={}, wires=[], cuts=[])
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        source = Point(0.5, 0.5)
        target = Point(1.5, 0.5)
        results = find_paths(
            graph=graph,
            source=source,
            targets=[target],
            side="top",
            cost_model=RoutingCostModel(),
            k=3,
        )
        assert len(results) >= 1
        best = results[0]
        assert isinstance(best, RoutingResult)
        assert best.path[0] == source
        assert best.path[-1] == target
        assert best.cost < float("inf")

    def test_path_stays_on_board(self) -> None:
        # 1×3 board; source=(0.5,0.5), target=(2.5,0.5).
        # Path must go through (1.5,0.5) — only valid intermediate pad.
        board = _perfboard(1, 3)
        lay = Layout(placements={}, wires=[], cuts=[])
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        results = find_paths(
            graph=graph,
            source=Point(0.5, 0.5),
            targets=[Point(2.5, 0.5)],
            side="top",
            cost_model=RoutingCostModel(),
            k=1,
        )
        assert len(results) == 1
        path = results[0].path
        # All points must be valid pad centers.
        valid = {Point(0.5, 0.5), Point(1.5, 0.5), Point(2.5, 0.5)}
        for pt in path:
            assert pt in valid

    def test_no_path_when_target_unreachable(self) -> None:
        # 1×1 board — source == target only pad, target off-board.
        board = _perfboard(1, 1)
        lay = Layout(placements={}, wires=[], cuts=[])
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        results = find_paths(
            graph=graph,
            source=Point(0.5, 0.5),
            targets=[Point(99.5, 99.5)],  # not on board
            side="top",
            cost_model=RoutingCostModel(),
            k=3,
        )
        assert results == []

    def test_source_equals_target_returns_trivial_path(self) -> None:
        board = _perfboard(1, 2)
        lay = Layout(placements={}, wires=[], cuts=[])
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        pt = Point(0.5, 0.5)
        results = find_paths(
            graph=graph,
            source=pt,
            targets=[pt],
            side="top",
            cost_model=RoutingCostModel(),
            k=1,
        )
        assert len(results) == 1
        assert results[0].path == [pt]
        assert results[0].cost == 0.0

    def test_top_k_paths_ordered_by_cost(self) -> None:
        # 3×3 board; multiple paths from corner to corner.
        board = _perfboard(3, 3)
        lay = Layout(placements={}, wires=[], cuts=[])
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        results = find_paths(
            graph=graph,
            source=Point(0.5, 0.5),
            targets=[Point(2.5, 2.5)],
            side="top",
            cost_model=RoutingCostModel(),
            k=3,
        )
        assert len(results) >= 1
        # Costs must be non-decreasing.
        costs = [r.cost for r in results]
        assert costs == sorted(costs)

    def test_direction_change_penalises_turns(self) -> None:
        # 1×3 board, source=(0.5,0.5), target=(2.5,0.5).
        # No turns possible → direction_change cost should NOT apply.
        board = _perfboard(1, 3)
        lay = Layout(placements={}, wires=[], cuts=[])
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        model_no_penalty = RoutingCostModel(direction_change=0.0)
        model_with_penalty = RoutingCostModel(direction_change=10.0)
        r1 = find_paths(
            graph=graph,
            source=Point(0.5, 0.5),
            targets=[Point(2.5, 0.5)],
            side="top",
            cost_model=model_no_penalty,
            k=1,
        )
        r2 = find_paths(
            graph=graph,
            source=Point(0.5, 0.5),
            targets=[Point(2.5, 0.5)],
            side="top",
            cost_model=model_with_penalty,
            k=1,
        )
        # Straight path → cost equal regardless of turn penalty.
        assert abs(r1[0].cost - r2[0].cost) < 1e-9


class TestFindPathsWithOccupiedPads:
    def test_occupied_pad_blocks_path(self) -> None:
        # 1×3 board. Mark (1.5, 0.5) as occupied by another net.
        # Only path is through that pad → should return empty (inf cost).
        board = _perfboard(1, 3)
        comp_id = uuid.uuid4()
        net_other = Net(
            id=uuid.uuid4(),
            name="OTHER",
            pins={PinRef(component_id=comp_id, pin_number=1)},
        )
        lay = Layout(placements={}, wires=[], cuts=[])
        graph = ConnectivityGraph(board=board, layout=lay, components={})

        # Mark the middle pad as belonging to another net in the cost model.
        occupied = {Point(1.5, 0.5): net_other.id}
        model = RoutingCostModel(occupied_by_other_net=float("inf"))

        results = find_paths(
            graph=graph,
            source=Point(0.5, 0.5),
            targets=[Point(2.5, 0.5)],
            side="top",
            cost_model=model,
            k=1,
            occupied_pads=occupied,
        )
        assert results == []

    def test_same_net_pad_is_preferred(self) -> None:
        # 2×3 board; two paths source→target.
        # One path goes through a pad of the *same* net (lower cost).
        board = _perfboard(2, 3)
        net_id = uuid.uuid4()
        lay = Layout(placements={}, wires=[], cuts=[])
        graph = ConnectivityGraph(board=board, layout=lay, components={})

        # Mark (1.5, 0.5) as same-net (discount).
        same_net = {Point(1.5, 0.5): net_id}
        model = RoutingCostModel(free_pad=1.0, same_net_pad=0.1)

        results_with = find_paths(
            graph=graph,
            source=Point(0.5, 0.5),
            targets=[Point(2.5, 0.5)],
            side="top",
            cost_model=model,
            k=1,
            same_net_pads=same_net,
        )
        results_without = find_paths(
            graph=graph,
            source=Point(0.5, 0.5),
            targets=[Point(2.5, 0.5)],
            side="top",
            cost_model=model,
            k=1,
        )
        assert results_with[0].cost <= results_without[0].cost


class TestRoutingResult:
    def test_result_has_path_and_cost(self) -> None:
        board = _perfboard(1, 2)
        lay = Layout(placements={}, wires=[], cuts=[])
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        results = find_paths(
            graph=graph,
            source=Point(0.5, 0.5),
            targets=[Point(1.5, 0.5)],
            side="top",
            cost_model=RoutingCostModel(),
            k=1,
        )
        r = results[0]
        assert len(r.path) >= 2
        assert r.cost > 0
        assert r.path[0] == Point(0.5, 0.5)
        assert r.path[-1] == Point(1.5, 0.5)

    def test_path_is_connected(self) -> None:
        # Each consecutive pair of points in the path must be neighbours
        # (distance == 1.0 in Manhattan sense, i.e., differ by exactly 1
        # in x or y and 0 in the other).
        board = _perfboard(3, 3)
        lay = Layout(placements={}, wires=[], cuts=[])
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        results = find_paths(
            graph=graph,
            source=Point(0.5, 0.5),
            targets=[Point(2.5, 2.5)],
            side="top",
            cost_model=RoutingCostModel(),
            k=1,
        )
        path = results[0].path
        for a, b in zip(path[:-1], path[1:]):
            dx = abs(a.x - b.x)
            dy = abs(a.y - b.y)
            # Must be exactly one raster step in one axis.
            assert (abs(dx - 1.0) < 1e-9 and dy < 1e-9) or (
                abs(dy - 1.0) < 1e-9 and dx < 1e-9
            )
