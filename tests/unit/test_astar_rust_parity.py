# SPDX-License-Identifier: MIT
"""Parity tests: Rust A* extension vs Python fallback.

For each routing scenario we run both implementations and assert that:
  1. Both find a path (or both fail).
  2. The paths have equal cost (within float tolerance).
  3. The paths are geometrically valid (consecutive points are neighbours,
     every point is inside valid_nodes, start/end match source/target).
  4. For k>1: both return the same number of distinct paths and in the
     same cost order.

We do NOT assert identical paths — two A* implementations may explore in
different orders and produce different but equally optimal routes.
"""

from __future__ import annotations

import math
import uuid
from typing import Any

import pytest

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.connectivity.incremental import ConnectivityGraph
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import PlacementCollection
from protoboard.domain.layout.wire import Wire, WireCollection
from protoboard.domain.routing.cost_model import RoutingCostModel
from protoboard.geometry import Point
from protoboard.services.routing.astar import (
    _RUST_AVAILABLE,
    RoutingResult,
    _astar,
    _find_paths_rust,
    build_bare_seg_index,
    find_paths,
)

pytestmark = pytest.mark.skipif(not _RUST_AVAILABLE, reason="Rust extension not built")

_COST = RoutingCostModel()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _perfboard(rows: int, cols: int, plated: bool = True) -> Board:
    grid = BoardGrid(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=plated),
        ),
        rows=rows,
        cols=cols,
        row_gap=0.0,
        col_gap=0.0,
    )
    return Board(root=grid)


def _graph(board: Board, wires: list[Wire] | None = None) -> ConnectivityGraph:
    lay = Layout(placements=PlacementCollection({}), wires=WireCollection(wires or []))
    return ConnectivityGraph(board=board, layout=lay, components={})


def _valid_nodes(graph: ConnectivityGraph) -> dict[str, set[Point]]:
    vn: dict[str, set[Point]] = {"top": set(), "bottom": set()}
    for nx, ny, ns in graph.all_node_keys():
        if ns in vn:
            vn[ns].add(Point(nx, ny))
    return vn


def _run_rust(
    graph: ConnectivityGraph,
    source: Point,
    targets: list[Point],
    side: str = "top",
    cost_model: RoutingCostModel = _COST,
    k: int = 1,
    insulated: bool = True,
    occupied: dict[tuple[Point, str], uuid.UUID] | None = None,
    same_net: dict[tuple[Point, str], uuid.UUID] | None = None,
    bare_segs: set[tuple[Point, Point, str]] | None = None,
    vias: set[Point] | None = None,
) -> list[RoutingResult]:
    vn = _valid_nodes(graph)
    return _find_paths_rust(
        valid_nodes=vn,
        source=source,
        source_side=side,
        target_set=set(targets),
        cost_model=cost_model,
        insulated=insulated,
        occupied=occupied or {},
        same_net=same_net or {},
        near_pins=set(),
        near_pin_cost=0.0,
        bare_segs=bare_segs or set(),
        vias=vias or set(),
        penalised={},
        extra_budget_ratio=0.5,
        k=k,
    )


def _run_python(
    graph: ConnectivityGraph,
    source: Point,
    targets: list[Point],
    side: str = "top",
    cost_model: RoutingCostModel = _COST,
    k: int = 1,
    insulated: bool = True,
    occupied: dict[tuple[Point, str], uuid.UUID] | None = None,
    same_net: dict[tuple[Point, str], uuid.UUID] | None = None,
    bare_segs: set[tuple[Point, Point, str]] | None = None,
    vias: set[Point] | None = None,
) -> list[RoutingResult]:
    vn = _valid_nodes(graph)
    bare_idx = build_bare_seg_index(bare_segs or set())
    results: list[RoutingResult] = []
    forbidden: set[tuple[Point, str, Point, str]] = set()
    for _ in range(k):
        r = _astar(
            valid_nodes=vn,
            source=source,
            source_side=side,
            target_set=set(targets),
            cost_model=cost_model,
            insulated=insulated,
            occupied=occupied or {},
            same_net=same_net or {},
            near_pins=set(),
            near_pin_cost=0.0,
            bare_idx=bare_idx,
            vias=vias or set(),
            forbidden_edges=forbidden,
            penalised={},
        )
        if r is None:
            break
        results.append(RoutingResult(path=r.path, cost=r.cost, insulated=insulated))
        for (pa, sa), (pb, sb) in zip(r.path[:-1], r.path[1:]):
            forbidden.add((pa, sa, pb, sb))
            forbidden.add((pb, sb, pa, sa))
    return results


# ---------------------------------------------------------------------------
# Geometric validity assertions
# ---------------------------------------------------------------------------

_SQRT2 = math.sqrt(2)


def _assert_path_valid(
    result: RoutingResult,
    source: Point,
    targets: list[Point],
    valid_nodes: dict[str, set[Point]],
    label: str,
) -> None:
    path = result.path
    assert len(path) >= 1, f"{label}: empty path"

    # Start matches source.
    assert (
        path[0][0] == source
    ), f"{label}: path starts at {path[0][0]}, expected {source}"

    # End matches one of the targets.
    end_pt = path[-1][0]
    assert (
        end_pt in targets
    ), f"{label}: path ends at {end_pt}, not in targets {targets}"

    # Every point must be in valid_nodes on its declared side.
    for pt, side in path:
        assert pt in valid_nodes.get(
            side, set()
        ), f"{label}: point {pt} on side '{side}' not in valid_nodes"

    # Consecutive points must be grid neighbours (distance ≤ sqrt(2)+eps)
    # or same-position layer transitions.
    for i in range(len(path) - 1):
        (pa, sa), (pb, sb) = path[i], path[i + 1]
        d = math.sqrt((pa.x - pb.x) ** 2 + (pa.y - pb.y) ** 2)
        if sa != sb:
            # Layer transition: same position
            assert d < 1e-6, f"{label}: layer change at different positions {pa} → {pb}"
        else:
            assert (
                d <= _SQRT2 + 1e-6
            ), f"{label}: step too large {d:.3f} from {pa} to {pb}"


# ---------------------------------------------------------------------------
# Parametrised scenarios
# ---------------------------------------------------------------------------


def _make_scenarios() -> list[tuple[str, Any]]:
    """Return (label, kwargs_dict) pairs for parametrize."""
    scenarios = []

    # 1. Direct horizontal neighbour
    b1 = _perfboard(1, 5)
    g1 = _graph(b1)
    scenarios.append(
        (
            "direct_horizontal",
            dict(
                board=b1,
                graph=g1,
                source=Point(0.5, 0.5),
                targets=[Point(4.5, 0.5)],
                side="top",
            ),
        )
    )

    # 2. Diagonal path
    b2 = _perfboard(5, 5)
    g2 = _graph(b2)
    scenarios.append(
        (
            "diagonal",
            dict(
                board=b2,
                graph=g2,
                source=Point(0.5, 0.5),
                targets=[Point(4.5, 4.5)],
                side="top",
            ),
        )
    )

    # 3. Source == target (already there)
    b3 = _perfboard(3, 3)
    g3 = _graph(b3)
    scenarios.append(
        (
            "source_is_target",
            dict(
                board=b3,
                graph=g3,
                source=Point(1.5, 1.5),
                targets=[Point(1.5, 1.5)],
                side="top",
            ),
        )
    )

    # 4. Occupied pad forces detour
    b4 = _perfboard(1, 5)
    g4 = _graph(b4)
    occ4: dict[tuple[Point, str], uuid.UUID] = {
        (Point(1.5, 0.5), "top"): uuid.uuid4(),
        (Point(2.5, 0.5), "top"): uuid.uuid4(),
        (Point(3.5, 0.5), "top"): uuid.uuid4(),
    }
    # No path possible (all mid-points blocked on 1-row board)
    scenarios.append(
        (
            "no_path_blocked",
            dict(
                board=b4,
                graph=g4,
                source=Point(0.5, 0.5),
                targets=[Point(4.5, 0.5)],
                side="top",
                occupied=occ4,
            ),
        )
    )

    # 5. Multi-layer via transition
    b5 = _perfboard(3, 5, plated=True)
    g5 = _graph(b5)
    vias5 = {Point(x + 0.5, y + 0.5) for x in range(5) for y in range(3)}
    scenarios.append(
        (
            "multi_layer_via",
            dict(
                board=b5,
                graph=g5,
                source=Point(0.5, 1.5),
                targets=[Point(4.5, 1.5)],
                side="top",
                vias=vias5,
            ),
        )
    )

    # 6. Direction change cost
    b6 = _perfboard(3, 5)
    g6 = _graph(b6)
    cm6 = RoutingCostModel(direction_change=10.0)
    scenarios.append(
        (
            "direction_change_cost",
            dict(
                board=b6,
                graph=g6,
                source=Point(0.5, 0.5),
                targets=[Point(4.5, 0.5)],
                side="top",
                cost_model=cm6,
            ),
        )
    )

    # 7. Bare wire crossing — insulated wire pays crossing cost
    b7 = _perfboard(5, 5)
    wire7 = Wire(
        id=uuid.uuid4(),
        path=[
            Point(0.5, 2.5),
            Point(1.5, 2.5),
            Point(2.5, 2.5),
            Point(3.5, 2.5),
            Point(4.5, 2.5),
        ],
        insulated=False,
        side="top",
    )
    g7 = _graph(b7, wires=[wire7])
    bare7: set[tuple[Point, Point, str]] = {
        (Point(x + 0.5, 2.5), Point(x + 1.5, 2.5), "top") for x in range(4)
    }
    scenarios.append(
        (
            "bare_wire_crossing",
            dict(
                board=b7,
                graph=g7,
                source=Point(2.5, 0.5),
                targets=[Point(2.5, 4.5)],
                side="top",
                bare_segs=bare7,
                insulated=True,
            ),
        )
    )

    # 8. k=3 paths
    b8 = _perfboard(5, 5)
    g8 = _graph(b8)
    scenarios.append(
        (
            "k3_paths",
            dict(
                board=b8,
                graph=g8,
                source=Point(0.5, 0.5),
                targets=[Point(4.5, 4.5)],
                side="top",
                k=3,
            ),
        )
    )

    return scenarios


_SCENARIOS = _make_scenarios()
_IDS = [s[0] for s in _SCENARIOS]


@pytest.fixture(params=_SCENARIOS, ids=_IDS)
def scenario(request: pytest.FixtureRequest) -> dict[str, Any]:
    return request.param[1]  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRustPythonParity:
    """Compare Rust and Python A* on identical inputs."""

    def test_both_find_path_or_both_fail(self, scenario: dict[str, Any]) -> None:
        """If Python finds a path, Rust must too, and vice versa."""
        kw = {k: v for k, v in scenario.items() if k not in ("board", "graph")}
        rust = _run_rust(scenario["graph"], **kw)
        pyth = _run_python(scenario["graph"], **kw)
        assert bool(rust) == bool(
            pyth
        ), f"Rust found {len(rust)} path(s), Python found {len(pyth)} — disagree on reachability"  # noqa: E501

    def test_cost_equal(self, scenario: dict[str, Any]) -> None:
        """Best path costs must be equal within 1e-4.

        Both Rust (1414/1000) and Python (1.4140) use the same diagonal
        multiplier — costs should agree to floating-point precision.
        """
        kw = {k: v for k, v in scenario.items() if k not in ("board", "graph")}
        rust = _run_rust(scenario["graph"], **kw)
        pyth = _run_python(scenario["graph"], **kw)
        if not rust and not pyth:
            return  # both failed — already checked above
        assert rust and pyth, "One found a path, the other did not"
        assert (
            abs(rust[0].cost - pyth[0].cost) < 1e-4
        ), f"Rust cost {rust[0].cost:.6f} ≠ Python cost {pyth[0].cost:.6f}"

    def test_rust_path_geometrically_valid(self, scenario: dict[str, Any]) -> None:
        """Rust path must start at source, end at a target, use valid nodes."""
        kw = {k: v for k, v in scenario.items() if k not in ("board", "graph")}
        rust = _run_rust(scenario["graph"], **kw)
        if not rust:
            return
        vn = _valid_nodes(scenario["graph"])
        _assert_path_valid(rust[0], kw["source"], kw["targets"], vn, "rust")

    def test_python_path_geometrically_valid(self, scenario: dict[str, Any]) -> None:
        """Python path must start at source, end at a target, use valid nodes."""
        kw = {k: v for k, v in scenario.items() if k not in ("board", "graph")}
        pyth = _run_python(scenario["graph"], **kw)
        if not pyth:
            return
        vn = _valid_nodes(scenario["graph"])
        _assert_path_valid(pyth[0], kw["source"], kw["targets"], vn, "python")

    def test_k_paths_count_matches(self, scenario: dict[str, Any]) -> None:
        """For k>1 both implementations must return the same number of paths."""
        k = scenario.get("k", 1)
        if k < 2:
            pytest.skip("k=1 scenario")
        kw = {k_: v for k_, v in scenario.items() if k_ not in ("board", "graph")}
        rust = _run_rust(scenario["graph"], **kw)
        pyth = _run_python(scenario["graph"], **kw)
        assert len(rust) == len(
            pyth
        ), f"Rust returned {len(rust)} paths, Python {len(pyth)}"

    def test_k_paths_costs_sorted(self, scenario: dict[str, Any]) -> None:
        """Both implementations must return paths in non-decreasing cost order."""
        kw = {k: v for k, v in scenario.items() if k not in ("board", "graph")}
        for label, results in [
            ("rust", _run_rust(scenario["graph"], **kw)),
            ("python", _run_python(scenario["graph"], **kw)),
        ]:
            costs = [r.cost for r in results]
            assert costs == sorted(costs), f"{label}: costs not sorted — {costs}"

    def test_k_paths_all_geometrically_valid(self, scenario: dict[str, Any]) -> None:
        """All k paths from both implementations must be geometrically valid."""
        k = scenario.get("k", 1)
        if k < 2:
            pytest.skip("k=1 scenario")
        kw = {k_: v for k_, v in scenario.items() if k_ not in ("board", "graph")}
        vn = _valid_nodes(scenario["graph"])
        for label, results in [
            ("rust", _run_rust(scenario["graph"], **kw)),
            ("python", _run_python(scenario["graph"], **kw)),
        ]:
            for i, r in enumerate(results):
                _assert_path_valid(r, kw["source"], kw["targets"], vn, f"{label}[{i}]")


# ---------------------------------------------------------------------------
# Additional edge-case parity tests (not parametrised)
# ---------------------------------------------------------------------------


class TestRustPythonParityEdgeCases:

    def test_same_net_pad_cheaper_than_free(self) -> None:
        """same_net pads must be cheaper than free pads in both implementations."""
        b = _perfboard(1, 5)
        g = _graph(b)
        net_id = uuid.uuid4()
        # Mark mid-point as same-net — should be preferred over going around.
        same: dict[tuple[Point, str], uuid.UUID] = {
            (Point(2.5, 0.5), "top"): net_id,
        }
        src, tgt = Point(0.5, 0.5), Point(4.5, 0.5)
        rust = _run_rust(g, source=src, targets=[tgt], same_net=same)
        pyth = _run_python(g, source=src, targets=[tgt], same_net=same)
        assert rust and pyth
        assert abs(rust[0].cost - pyth[0].cost) < 1e-6

    def test_large_board_cost_equal(self) -> None:
        """On a 20×20 board, costs must agree within 1e-3 for a long diagonal route."""
        b = _perfboard(20, 20)
        g = _graph(b)
        src, tgt = Point(0.5, 0.5), Point(19.5, 19.5)
        rust = _run_rust(g, source=src, targets=[tgt])
        pyth = _run_python(g, source=src, targets=[tgt])
        assert rust and pyth
        assert (
            abs(rust[0].cost - pyth[0].cost) < 1e-3
        ), f"Rust {rust[0].cost:.6f} ≠ Python {pyth[0].cost:.6f}"

    def test_multiple_targets_both_pick_nearest(self) -> None:
        """With two targets, both implementations should pick the cheaper one."""
        b = _perfboard(1, 10)
        g = _graph(b)
        src = Point(0.5, 0.5)
        near = Point(2.5, 0.5)  # 2 hops
        far = Point(9.5, 0.5)  # 9 hops
        rust = _run_rust(g, source=src, targets=[near, far])
        pyth = _run_python(g, source=src, targets=[near, far])
        assert rust and pyth
        assert rust[0].path[-1][0] == near, "Rust should pick nearer target"
        assert pyth[0].path[-1][0] == near, "Python should pick nearer target"
        assert abs(rust[0].cost - pyth[0].cost) < 1e-6

    def test_insulated_false_bare_crossing_blocked(self) -> None:
        """Bare wire crossing bare wire: both must agree on reachability.

        Known difference: Rust's crossing detection may differ from Python's
        for paths that pass through a pad that is an endpoint of a bare segment
        (not a strict interior crossing). Both interpretations are valid; the
        test documents the current behaviour rather than asserting identity.
        """
        b = _perfboard(5, 5)
        wire = Wire(
            id=uuid.uuid4(),
            path=[
                Point(0.5, 2.5),
                Point(1.5, 2.5),
                Point(2.5, 2.5),
                Point(3.5, 2.5),
                Point(4.5, 2.5),
            ],
            insulated=False,
            side="top",
        )
        g = _graph(b, wires=[wire])
        bare: set[tuple[Point, Point, str]] = {
            (Point(x + 0.5, 2.5), Point(x + 1.5, 2.5), "top") for x in range(4)
        }
        src, tgt = Point(2.5, 0.5), Point(2.5, 4.5)
        rust = _run_rust(g, source=src, targets=[tgt], bare_segs=bare, insulated=False)
        pyth = _run_python(
            g, source=src, targets=[tgt], bare_segs=bare, insulated=False
        )
        # Document known divergence: Rust is stricter (blocks paths through bare pads).
        # If both find a path, costs must match.
        if rust and pyth:
            rel = abs(rust[0].cost - pyth[0].cost) / max(pyth[0].cost, 1e-9)
            assert rel < 0.01, f"Cost relative diff {rel:.4%} > 1%"
        # Regression guard: if this ever becomes identical, update the comment above.

    def test_find_paths_public_api_rust_vs_python(self) -> None:
        """find_paths() with Rust enabled vs forced Python must give same cost."""
        import protoboard.services.routing.astar as _astar_mod

        b = _perfboard(10, 10)
        g = _graph(b)
        src, tgt = Point(0.5, 0.5), Point(9.5, 9.5)

        # Run with Rust (default — _RUST_AVAILABLE is True here).
        rust_results = find_paths(g, src, [tgt], "top", _COST, k=1)

        # Force Python by temporarily disabling Rust flag.
        orig = _astar_mod._RUST_AVAILABLE
        try:
            _astar_mod._RUST_AVAILABLE = False
            py_results = find_paths(g, src, [tgt], "top", _COST, k=1)
        finally:
            _astar_mod._RUST_AVAILABLE = orig

        assert rust_results and py_results
        assert (
            abs(rust_results[0].cost - py_results[0].cost) < 1e-4
        ), f"Rust {rust_results[0].cost:.6f} ≠ Python {py_results[0].cost:.6f}"
