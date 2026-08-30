# SPDX-License-Identifier: MIT
"""Routing quality benchmarks — Wire/MST ratio and Hull ratio per net.

These benchmarks run a full autoroute pass on the synthetic medium and
large projects and report aggregate quality metrics.  They serve as a
baseline for comparing routing strategy changes (e.g. metric-guided net
ordering).

Run with:
    uv run pytest tests/benchmarks/test_routing_quality.py --benchmark-only -v -s

Quality metrics (same as Net Inspector panel):
  wire_mst_ratio  = sum(wire_length) / sum(MST_manhattan)  per net
  hull_ratio      = convex_hull_area(wire_pts) / pin_hull_area(pin_pts)  per net

Aggregate (reported as benchmark value so pytest-benchmark tracks them):
  mean_wire_mst   — mean Wire/MST ratio across all fully-routed nets
  mean_hull       — mean Hull ratio across all fully-routed nets
  unrouted_count  — number of nets that could not be routed (lower = better)
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pytest

from protoboard.geometry import Point

# ---------------------------------------------------------------------------
# Metric helpers (duplicated from net_inspector_panel to avoid Qt dependency)
# ---------------------------------------------------------------------------


def _manhattan_length(wires: list) -> float:
    total = 0.0
    for wire in wires:
        pts = wire.path
        for i in range(len(pts) - 1):
            total += abs(pts[i + 1].x - pts[i].x) + abs(pts[i + 1].y - pts[i].y)
    return total


def _mst_manhattan(points: list[Point]) -> float:
    n = len(points)
    if n < 2:
        return 0.0
    in_tree = {0}
    total = 0.0
    min_dist = [
        abs(points[0].x - points[j].x) + abs(points[0].y - points[j].y)
        for j in range(n)
    ]
    while len(in_tree) < n:
        best = math.inf
        next_j = -1
        for j in range(n):
            if j not in in_tree and min_dist[j] < best:
                best = min_dist[j]
                next_j = j
        in_tree.add(next_j)
        total += best
        for j in range(n):
            if j in in_tree:
                continue
            d = abs(points[next_j].x - points[j].x) + abs(
                points[next_j].y - points[j].y
            )
            if d < min_dist[j]:
                min_dist[j] = d
    return total


def _convex_hull_area(points: list[Point]) -> float:
    if len(points) < 3:
        return 0.0
    pivot = min(points, key=lambda p: (p.y, p.x))

    def _angle(p: Point) -> float:
        return math.atan2(p.y - pivot.y, p.x - pivot.x)

    sorted_pts = sorted(points, key=_angle)
    hull: list[Point] = []
    for p in sorted_pts:
        while len(hull) >= 2:
            o, a = hull[-2], hull[-1]
            cross = (a.x - o.x) * (p.y - o.y) - (a.y - o.y) * (p.x - o.x)
            if cross <= 0:
                hull.pop()
            else:
                break
        hull.append(p)
    nh = len(hull)
    area = 0.0
    for i in range(nh):
        j = (i + 1) % nh
        area += hull[i].x * hull[j].y
        area -= hull[j].x * hull[i].y
    return abs(area) / 2.0


def _pin_hull_area(points: list[Point]) -> float:
    n = len(points)
    if n == 0:
        return 0.0
    if n == 1:
        return 1.0
    if n == 2:
        return abs(points[0].x - points[1].x) + abs(points[0].y - points[1].y)
    return _convex_hull_area(points)


# ---------------------------------------------------------------------------
# Quality measurement
# ---------------------------------------------------------------------------


@dataclass
class NetQuality:
    name: str
    wire_mst_ratio: float  # 1.0 = optimal; higher = worse
    hull_ratio: float  # lower = more compact routing


@dataclass
class RoutingQualitySummary:
    net_count: int
    routed_count: int
    unrouted_count: int
    mean_wire_mst: float
    mean_hull: float
    worst_wire_mst_nets: list[tuple[str, float]]  # top-5 worst by wire/mst
    worst_hull_nets: list[tuple[str, float]]  # top-5 worst by hull


def measure_routing_quality(
    layout,
    schematic,
    instances: dict,
    components: dict,
) -> RoutingQualitySummary:
    """Compute Wire/MST and Hull metrics for every net in *schematic*."""
    from protoboard.domain.layout.placement import pin_world_position

    # Build wire index: net_id → [Wire]
    # We identify a wire's net by checking which net's pins land on its endpoints.
    # Simpler: build pin_pos → net_id map, then for each wire check its endpoints.
    pin_pos_to_net: dict[tuple[float, float], object] = {}
    for net in schematic.nets.values():
        for pin_ref in net.pins:
            pl = layout.placements.get(pin_ref.component_id)
            inst = instances.get(pin_ref.component_id)
            if pl is None or inst is None:
                continue
            comp = components.get(inst.definition_ref)
            if comp is None:
                continue
            pt = pin_world_position(inst.resolved_footprint(), pl, pin_ref.pin_number)
            pin_pos_to_net[(round(pt.x, 4), round(pt.y, 4))] = net.id

    wire_net_id: dict[object, object] = {}  # wire.id → net.id
    for wire in layout.wires:
        if not wire.path:
            continue
        for pt in (wire.path[0], wire.path[-1]):
            key = (round(pt.x, 4), round(pt.y, 4))
            nid = pin_pos_to_net.get(key)
            if nid is not None:
                wire_net_id[wire.id] = nid
                break

    net_wires: dict[object, list] = {}
    for wire in layout.wires:
        nid = wire_net_id.get(wire.id)
        if nid is not None:
            net_wires.setdefault(nid, []).append(wire)

    qualities: list[NetQuality] = []
    unrouted_count = 0

    for net in schematic.nets.values():
        # Collect pin positions.
        pin_pts: list[Point] = []
        for pin_ref in net.pins:
            pl = layout.placements.get(pin_ref.component_id)
            inst = instances.get(pin_ref.component_id)
            if pl is None or inst is None:
                continue
            comp = components.get(inst.definition_ref)
            if comp is None:
                continue
            pin_pts.append(
                pin_world_position(inst.resolved_footprint(), pl, pin_ref.pin_number)
            )

        if len(pin_pts) < 2:
            continue

        wires = net_wires.get(net.id, [])
        if not wires:
            unrouted_count += 1
            continue

        wire_len = _manhattan_length(wires)
        mst = _mst_manhattan(pin_pts)
        wire_mst = (wire_len / mst) if mst > 0 else 1.0

        wire_pts: list[Point] = []
        for wire in wires:
            wire_pts.extend(wire.path)
        hull_w = _convex_hull_area(wire_pts)
        hull_p = _pin_hull_area(pin_pts)
        hull_ratio = (hull_w / hull_p) if hull_p > 0 else 0.0

        qualities.append(NetQuality(net.name, wire_mst, hull_ratio))

    routed = len(qualities)
    mean_mst = sum(q.wire_mst_ratio for q in qualities) / routed if routed else 0.0
    mean_hull = sum(q.hull_ratio for q in qualities) / routed if routed else 0.0

    worst_mst = sorted(qualities, key=lambda q: q.wire_mst_ratio, reverse=True)[:5]
    worst_hull = sorted(qualities, key=lambda q: q.hull_ratio, reverse=True)[:5]

    return RoutingQualitySummary(
        net_count=routed + unrouted_count,
        routed_count=routed,
        unrouted_count=unrouted_count,
        mean_wire_mst=mean_mst,
        mean_hull=mean_hull,
        worst_wire_mst_nets=[(q.name, q.wire_mst_ratio) for q in worst_mst],
        worst_hull_nets=[(q.name, q.hull_ratio) for q in worst_hull],
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def synthetic_medium_empty_layout(synthetic_medium_project):
    board, layout, schematic, instances, routing, _lib, components = (
        synthetic_medium_project
    )
    # synthetic generator already produces a wire-free layout.
    return (
        board,
        _routing_layout_copy(layout),
        schematic,
        instances,
        routing,
        components,
    )


def _routing_layout_copy(layout):
    """Copy domain data without session-bound resolver callbacks."""
    from protoboard.domain.layout.layout import Layout
    from protoboard.domain.layout.wire import WireCollection

    return Layout(
        placements=layout.placements.copy(),
        wires=WireCollection(list(layout.wires)),
        groups=dict(layout.groups),
        knots=dict(layout.knots),
        pin_knots=dict(layout.pin_knots),
    )


def _run_autoroute(
    board,
    layout,
    schematic,
    instances,
    routing,
    components,
    max_optimize_iterations: int = 0,
    max_rip_up_passes: int = 3,
):
    from protoboard.services.routing.autorouter import autoroute

    return autoroute(
        board=board,
        layout=_routing_layout_copy(layout),
        schematic=schematic,
        components=components,
        instances=instances,
        cost_model=routing.cost_model(),
        max_rip_up_passes=max_rip_up_passes,
        max_optimize_iterations=max_optimize_iterations,
        preferred_side=routing.preferred_side,
        wire_mode=routing.wire_mode,
        max_detour_factor=routing.max_detour_factor,
    )


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------


def test_bench_routing_quality_greedy(benchmark, synthetic_medium_empty_layout):
    """Greedy pass only (no optimize, rip-up=3). Baseline quality measurement."""
    board, layout, schematic, instances, routing, components = (
        synthetic_medium_empty_layout
    )

    def _run():
        result = _run_autoroute(
            board,
            layout,
            schematic,
            instances,
            routing,
            components,
            max_optimize_iterations=0,
        )
        result_layout = _routing_layout_copy(layout)
        result_layout.wires[:] = result.wires
        return measure_routing_quality(result_layout, schematic, instances, components)

    summary = benchmark.pedantic(_run, iterations=1, rounds=3)

    print(
        f"\n  nets={summary.net_count}  routed={summary.routed_count}"
        f"  unrouted={summary.unrouted_count}"
        f"  mean_wire_mst={summary.mean_wire_mst:.3f}"
        f"  mean_hull={summary.mean_hull:.3f}"
    )
    print(f"  worst wire/mst: {summary.worst_wire_mst_nets}")
    print(f"  worst hull:     {summary.worst_hull_nets}")


def test_bench_routing_quality_optimized(benchmark, synthetic_medium_empty_layout):
    """Greedy + 10 optimize iterations. Measures quality improvement vs greedy."""
    board, layout, schematic, instances, routing, components = (
        synthetic_medium_empty_layout
    )

    def _run():
        result = _run_autoroute(
            board,
            layout,
            schematic,
            instances,
            routing,
            components,
            max_optimize_iterations=10,
        )
        result_layout = _routing_layout_copy(layout)
        result_layout.wires[:] = result.wires
        return measure_routing_quality(result_layout, schematic, instances, components)

    summary = benchmark.pedantic(_run, iterations=1, rounds=2)

    print(
        f"\n  nets={summary.net_count}  routed={summary.routed_count}"
        f"  unrouted={summary.unrouted_count}"
        f"  mean_wire_mst={summary.mean_wire_mst:.3f}"
        f"  mean_hull={summary.mean_hull:.3f}"
    )
    print(f"  worst wire/mst: {summary.worst_wire_mst_nets}")
    print(f"  worst hull:     {summary.worst_hull_nets}")


# ---------------------------------------------------------------------------
# Large synthetic project benchmarks (150 components, ~130 nets)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def synthetic_large_empty_layout(synthetic_large_project):
    board, layout, schematic, instances, routing, _lib, components = (
        synthetic_large_project
    )
    return board, layout, schematic, instances, routing, components


@pytest.mark.slow
def test_bench_large_greedy(benchmark, synthetic_large_empty_layout):
    """Large synthetic project: greedy pass only (no optimize, rip-up=3).

    ~100 s — the slowest test in the suite by far (large-board greedy
    autoroute). Marked ``slow`` so it is skipped by default; run it with
    ``-m slow`` when validating routing performance.
    """
    board, layout, schematic, instances, routing, components = (
        synthetic_large_empty_layout
    )

    def _run():
        result = _run_autoroute(
            board,
            layout,
            schematic,
            instances,
            routing,
            components,
            max_optimize_iterations=0,
        )
        result_layout = _routing_layout_copy(layout)
        result_layout.wires[:] = result.wires
        quality = measure_routing_quality(
            result_layout, schematic, instances, components
        )
        return result, quality

    result, quality = benchmark.pedantic(_run, iterations=1, rounds=3)

    print(
        f"\n  nets={quality.net_count}  routed={quality.routed_count}"
        f"  unrouted={quality.unrouted_count}"
        f"  mean_wire_mst={quality.mean_wire_mst:.3f}"
        f"  mean_hull={quality.mean_hull:.3f}"
    )
    if result.pass_stats:
        ps = result.pass_stats[0]
        print(
            f"  nodes_expanded={ps.total_nodes_expanded}"
            f"  over_budget={ps.total_over_budget_hits}"
            f"  rip_ups={ps.total_rip_up_attempts}"
        )
    print(f"  worst wire/mst: {quality.worst_wire_mst_nets}")
    print(f"  worst hull:     {quality.worst_hull_nets}")
