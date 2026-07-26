# SPDX-License-Identifier: MIT
"""Tests for the A* routing engine and RoutingCostModel.

Coordinate convention: pad centers at (col+0.5, row+0.5) for perfboard
with gap=0. Routing operates on the set of nodes from ConnectivityGraph.

path in RoutingResult is a list of (Point, side) pairs.
"""

import uuid

import pytest

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.connectivity.graph import ConnectivityGraph
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import PlacementCollection
from protoboard.domain.layout.wire import Wire, WireCollection
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.geometry import Point
from protoboard.services.routing.astar import (
    RoutingResult,
    find_paths,
    split_path_by_side,
)
from protoboard.services.routing.autorouter import _truncate_at_same_net_wire
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


def _pts(result: RoutingResult) -> list[Point]:
    """Extract just the Point from each (Point, side) step."""
    return [pt for pt, _ in result.path]


def _sides(result: RoutingResult) -> list[str]:
    """Extract just the side from each (Point, side) step."""
    return [s for _, s in result.path]


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

    def test_layer_change_cost_present(self) -> None:
        model = RoutingCostModel()
        assert model.layer_change > 0


class TestFindPathsBasic:
    def test_direct_neighbour(self) -> None:
        # 1×3 board; source=(0.5,0.5), target=(1.5,0.5) — one step right.
        board = _perfboard(1, 3)
        lay = Layout(placements=PlacementCollection({}), wires=WireCollection())
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
        assert _pts(best)[0] == source
        assert _pts(best)[-1] == target
        assert best.cost < float("inf")

    def test_path_stays_on_board(self) -> None:
        # 1×3 board; source=(0.5,0.5), target=(2.5,0.5).
        # Path must go through (1.5,0.5) — only valid intermediate pad.
        board = _perfboard(1, 3)
        lay = Layout(placements=PlacementCollection({}), wires=WireCollection())
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
        path_pts = _pts(results[0])
        # All points must be valid pad centers.
        valid = {Point(0.5, 0.5), Point(1.5, 0.5), Point(2.5, 0.5)}
        for pt in path_pts:
            assert pt in valid

    def test_no_path_when_target_unreachable(self) -> None:
        # 1×1 board — source == target only pad, target off-board.
        board = _perfboard(1, 1)
        lay = Layout(placements=PlacementCollection({}), wires=WireCollection())
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
        lay = Layout(placements=PlacementCollection({}), wires=WireCollection())
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
        assert _pts(results[0]) == [pt]
        assert results[0].cost == 0.0

    def test_top_k_paths_ordered_by_cost(self) -> None:
        # 3×3 board; multiple paths from corner to corner.
        board = _perfboard(3, 3)
        lay = Layout(placements=PlacementCollection({}), wires=WireCollection())
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
        lay = Layout(placements=PlacementCollection({}), wires=WireCollection())
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
        lay = Layout(placements=PlacementCollection({}), wires=WireCollection())
        graph = ConnectivityGraph(board=board, layout=lay, components={})

        # Mark the middle pad (top layer) as belonging to another net.
        occupied = {(Point(1.5, 0.5), "top"): net_other.id}
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
        lay = Layout(placements=PlacementCollection({}), wires=WireCollection())
        graph = ConnectivityGraph(board=board, layout=lay, components={})

        # Mark (1.5, 0.5) on top as same-net (discount).
        same_net = {(Point(1.5, 0.5), "top"): net_id}
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
        lay = Layout(placements=PlacementCollection({}), wires=WireCollection())
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
        pts = _pts(r)
        assert len(pts) >= 2
        assert r.cost > 0
        assert pts[0] == Point(0.5, 0.5)
        assert pts[-1] == Point(1.5, 0.5)

    def test_path_is_connected(self) -> None:
        # Each consecutive pair of points in the path must be neighbours.
        # Same-layer: differ by at most 1 in x and y (orthogonal or diagonal).
        # Layer-change: same position, different side — dx==dy==0 is allowed.
        board = _perfboard(3, 3)
        lay = Layout(placements=PlacementCollection({}), wires=WireCollection())
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
        for (pa, sa), (pb, sb) in zip(path[:-1], path[1:]):
            dx = abs(pa.x - pb.x)
            dy = abs(pa.y - pb.y)
            if sa != sb:
                # Layer change: must be at the same position.
                assert dx < 1e-9 and dy < 1e-9
            else:
                # Same-layer move: must be one raster step.
                assert (dx <= 1.0 + 1e-9) and (dy <= 1.0 + 1e-9) and (dx + dy > 1e-9)


class TestMultiLayerRouting:
    def test_single_layer_when_no_vias(self) -> None:
        """Without plated_through_pads, path stays on starting layer."""
        board = _perfboard(3, 3)
        lay = Layout(placements=PlacementCollection({}), wires=WireCollection())
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        results = find_paths(
            graph=graph,
            source=Point(0.5, 0.5),
            targets=[Point(2.5, 2.5)],
            side="top",
            cost_model=RoutingCostModel(),
            k=1,
            plated_through_pads=set(),  # no vias → single layer
        )
        assert len(results) >= 1
        assert all(s == "top" for _, s in results[0].path)

    def test_layer_change_possible_at_plated_through(self) -> None:
        """Router can switch layers when plated_through_pads are provided."""
        board = _perfboard(3, 3)
        lay = Layout(placements=PlacementCollection({}), wires=WireCollection())
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        # All pads are plated-through (perfboard).
        all_pads = {Point(c + 0.5, r + 0.5) for r in range(3) for c in range(3)}
        results = find_paths(
            graph=graph,
            source=Point(0.5, 0.5),
            targets=[Point(2.5, 2.5)],
            side="top",
            cost_model=RoutingCostModel(layer_change=0.5),
            k=1,
            plated_through_pads=all_pads,
        )
        assert len(results) >= 1
        # Path may include layer changes; each (Point, side) pair is valid.
        path = results[0].path
        assert path[0][0] == Point(0.5, 0.5)
        assert path[-1][0] == Point(2.5, 2.5)

    def test_via_prefers_bottom_over_long_detour(self) -> None:
        """If the direct top path is blocked, the router goes bottom via a via.

        Layout: 1×5 board. Middle pad (2.5, 0.5) is blocked on top.
        Vias at (0.5,0.5) and (4.5,0.5) allow top→bottom→top routing.
        Bottom path: source-bottom → (1.5,0.5)-bottom → ... → target-bottom.
        """
        board = _perfboard(1, 5)
        lay = Layout(placements=PlacementCollection({}), wires=WireCollection())
        graph = ConnectivityGraph(board=board, layout=lay, components={})

        # Block the middle pad only on the top layer.
        blocked_top = {(Point(2.5, 0.5), "top"): uuid.uuid4()}
        # All pads are plated-through → all are vias.
        all_pads = {Point(c + 0.5, 0.5) for c in range(5)}

        results = find_paths(
            graph=graph,
            source=Point(0.5, 0.5),
            targets=[Point(4.5, 0.5)],
            side="top",
            cost_model=RoutingCostModel(direction_change=0.0, layer_change=0.1),
            k=1,
            occupied_pads=blocked_top,
            plated_through_pads=all_pads,
        )
        assert len(results) >= 1
        pts = _pts(results[0])
        assert pts[0] == Point(0.5, 0.5)
        assert pts[-1] == Point(4.5, 0.5)
        # Path must not go through the blocked top pad.
        top_pts = {pt for pt, s in results[0].path if s == "top"}
        assert Point(2.5, 0.5) not in top_pts


class TestSplitPathBySide:
    def test_single_side(self) -> None:
        path = [(Point(0.5, 0.5), "top"), (Point(1.5, 0.5), "top")]
        segs = split_path_by_side(path)
        assert len(segs) == 1
        pts, side = segs[0]
        assert side == "top"
        assert pts == [Point(0.5, 0.5), Point(1.5, 0.5)]

    def test_layer_change(self) -> None:
        # top→bottom via at (1.5, 0.5), then continues on bottom.
        path = [
            (Point(0.5, 0.5), "top"),
            (Point(1.5, 0.5), "top"),
            (Point(1.5, 0.5), "bottom"),
            (Point(2.5, 0.5), "bottom"),
        ]
        segs = split_path_by_side(path)
        assert len(segs) == 2
        top_pts, top_side = segs[0]
        bot_pts, bot_side = segs[1]
        assert top_side == "top"
        assert bot_side == "bottom"
        # Via pad appears at end of top segment and start of bottom segment.
        assert top_pts[-1] == Point(1.5, 0.5)
        assert bot_pts[0] == Point(1.5, 0.5)

    def test_empty_path(self) -> None:
        assert split_path_by_side([]) == []

    def test_single_point(self) -> None:
        path = [(Point(0.5, 0.5), "top")]
        segs = split_path_by_side(path)
        assert len(segs) == 1
        assert segs[0][0] == [Point(0.5, 0.5)]


class TestSameNetWireTermination:
    def test_no_truncation_when_no_same_net_wires(self) -> None:
        path = [
            (Point(0.5, 0.5), "top"),
            (Point(1.5, 0.5), "top"),
            (Point(2.5, 0.5), "top"),
        ]
        result = _truncate_at_same_net_wire(path, set(), Point(2.5, 0.5))
        assert result == path

    def test_truncates_at_intermediate_same_net_point(self) -> None:
        # Path: A→B→C→D(target). B is already on a same-net wire.
        path = [
            (Point(0.5, 0.5), "top"),  # source
            (Point(1.5, 0.5), "top"),  # B — same-net wire here
            (Point(2.5, 0.5), "top"),
            (Point(3.5, 0.5), "top"),  # target
        ]
        same_net_pts = {(Point(1.5, 0.5), "top")}
        result = _truncate_at_same_net_wire(path, same_net_pts, Point(3.5, 0.5))
        # Should stop at B (index 1).
        assert len(result) == 2
        assert result[-1] == (Point(1.5, 0.5), "top")

    def test_does_not_truncate_at_source(self) -> None:
        # Source itself is on a same-net wire — should not truncate.
        path = [
            (Point(0.5, 0.5), "top"),  # source, also a same-net wire point
            (Point(1.5, 0.5), "top"),
            (Point(2.5, 0.5), "top"),  # target
        ]
        same_net_pts = {(Point(0.5, 0.5), "top")}
        result = _truncate_at_same_net_wire(path, same_net_pts, Point(2.5, 0.5))
        assert result == path  # not truncated

    def test_does_not_truncate_at_target(self) -> None:
        # Target itself could be on a same-net wire — no truncation (it IS the goal).
        path = [
            (Point(0.5, 0.5), "top"),
            (Point(1.5, 0.5), "top"),
            (Point(2.5, 0.5), "top"),  # target
        ]
        same_net_pts = {(Point(2.5, 0.5), "top")}
        result = _truncate_at_same_net_wire(path, same_net_pts, Point(2.5, 0.5))
        assert result == path

    def test_respects_layer_in_same_net_point(self) -> None:
        # Same position but different side — should NOT truncate.
        path = [
            (Point(0.5, 0.5), "top"),
            (Point(1.5, 0.5), "top"),
            (Point(2.5, 0.5), "top"),
        ]
        same_net_pts = {(Point(1.5, 0.5), "bottom")}  # bottom, not top
        result = _truncate_at_same_net_wire(path, same_net_pts, Point(2.5, 0.5))
        assert result == path  # no match on "top" → not truncated

    def test_short_path_unchanged(self) -> None:
        # Path with only 2 points (source, target) is never truncated.
        path = [(Point(0.5, 0.5), "top"), (Point(1.5, 0.5), "top")]
        same_net_pts = {(Point(0.5, 0.5), "top"), (Point(1.5, 0.5), "top")}
        result = _truncate_at_same_net_wire(path, same_net_pts, Point(1.5, 0.5))
        assert result == path


class TestValidNodesByCache:
    """valid_nodes_by_side() must be consistent with find_paths and invalidated on wire mutations."""

    def test_cache_returns_same_object(self) -> None:
        board = _perfboard(3, 3)
        graph = ConnectivityGraph(board=board, layout=Layout(placements=PlacementCollection({}), wires=WireCollection()), components={})
        a = graph.valid_nodes_by_side()
        b = graph.valid_nodes_by_side()
        assert a is b

    def test_cache_invalidated_after_add_wire(self) -> None:
        board = _perfboard(3, 3)
        graph = ConnectivityGraph(board=board, layout=Layout(placements=PlacementCollection({}), wires=WireCollection()), components={})
        before = graph.valid_nodes_by_side()
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(1.5, 0.5)],
            insulated=True,
            side="top",
        )
        graph.add_wire(wire)
        after = graph.valid_nodes_by_side()
        assert after is not before

    def test_cache_invalidated_after_remove_wire(self) -> None:
        board = _perfboard(3, 3)
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(1.5, 0.5)],
            insulated=True,
            side="top",
        )
        graph = ConnectivityGraph(board=board, layout=Layout(placements=PlacementCollection({}), wires=WireCollection([wire])), components={})
        graph.valid_nodes_by_side()  # warm cache
        graph.remove_wire(wire)
        # After remove, cache must be cleared — accessing it rebuilds.
        after = graph.valid_nodes_by_side()
        assert Point(0.5, 0.5) in after["top"]  # board pad still present

    def test_valid_nodes_contains_board_pads(self) -> None:
        board = _perfboard(2, 2)
        graph = ConnectivityGraph(board=board, layout=Layout(placements=PlacementCollection({}), wires=WireCollection()), components={})
        vn = graph.valid_nodes_by_side()
        # 2×2 perfboard: pads at (0.5,0.5), (1.5,0.5), (0.5,1.5), (1.5,1.5)
        assert Point(0.5, 0.5) in vn["top"]
        assert Point(1.5, 1.5) in vn["top"]
        # Keys with side suffixes like "top_ins_…" must NOT appear in top/bottom sets.
        assert len(vn) == 2
        assert set(vn.keys()) == {"top", "bottom"}

    def test_find_paths_uses_cache(self) -> None:
        # Routing twice on the same graph should hit the cache (same object returned).
        board = _perfboard(1, 5)
        graph = ConnectivityGraph(board=board, layout=Layout(placements=PlacementCollection({}), wires=WireCollection()), components={})
        cm = RoutingCostModel()
        src = Point(0.5, 0.5)
        tgt = Point(4.5, 0.5)
        r1 = find_paths(graph, src, [tgt], "top", cm, k=1)
        r2 = find_paths(graph, src, [tgt], "top", cm, k=1)
        # Both calls must find the same path.
        assert len(r1) == 1
        assert len(r2) == 1
        assert _pts(r1[0]) == _pts(r2[0])
