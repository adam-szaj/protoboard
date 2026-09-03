# SPDX-License-Identifier: MIT
"""Pure-Python metrics for benchmarking autoroute quality.

Used by `tools/benchmark_autoroute.py` to capture per-net and per-run
statistics that travel with each commit's fixture snapshot.  The metric
formulas mirror the ones displayed live in
`src/protoboard/gui/panels/net_inspector_panel.py` so the GUI and the
offline benchmark agree on what "wire/MST ratio" or "hull ratio" mean.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

from protoboard.domain.board.board import Board
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.connectivity.incremental import ConnectivityGraph
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import pin_world_position
from protoboard.domain.layout.wire import Wire
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from protoboard.services.routing.net_status import net_is_complete

# ---------------------------------------------------------------------------
# Per-net + per-run dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NetMetrics:
    name: str
    pin_count: int  # pins listed in schematic
    placed_pin_count: int  # pins whose component is placed on the board
    connected_pin_count: int  # pins in the same electric set (largest cluster)
    is_routed: bool  # all placed pins in one set
    wire_count: int
    redundant_wires: int  # wires whose removal still leaves their ends connected
    length: float  # total Manhattan length of all wires
    mst_length: float  # MST of the placed pins (lower bound)
    wire_mst_ratio: float | None
    wire_hull_area: float
    pin_hull_area: float
    hull_ratio: float | None


@dataclass(frozen=True)
class RunSummary:
    wire_mode: str
    max_rip_up_passes: int
    max_optimize_iterations: int
    elapsed_s: float
    total_nets: int
    routed_nets: int
    unrouted_nets: int
    total_pins: int
    placed_pins: int
    connected_pins: int  # summed across nets (multi-pin nets only)
    total_wires: int
    redundant_wires_total: int
    total_length: float
    mean_wire_mst_ratio: float | None
    mean_hull_ratio: float | None
    per_net: list[NetMetrics] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Geometry helpers (mirrors net_inspector_panel.py, kept Qt-free)
# ---------------------------------------------------------------------------


def _manhattan_length(wires: list[Wire]) -> float:
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
            if j in in_tree:
                continue
            if min_dist[j] < best:
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
# Top-level entry point
# ---------------------------------------------------------------------------


def compute_metrics(
    *,
    board: Board,
    layout: Layout,  # without autoroute wires (pre-routing layout)
    schematic: Schematic,
    components: dict[str, ComponentDefinition],
    instances: dict,
    wires: list[Wire],  # autoroute output wires
    wire_mode: str,
    max_rip_up_passes: int,
    max_optimize_iterations: int,
    elapsed_s: float,
) -> RunSummary:
    """Build a RunSummary from one autoroute invocation."""
    from dataclasses import replace

    final_layout = replace(layout, wires=list(layout.wires) + list(wires))
    graph = ConnectivityGraph(
        board=board,
        layout=final_layout,
        components=components,
        instances=instances,
    )

    # Group autoroute wires per net by inspecting which electric set each wire
    # endpoint belongs to.  A wire's canonical key matches exactly one net's
    # pins (or none, if the wire is a free-floating router scratch — should
    # not happen in autoroute output).
    from protoboard.domain.schematic.net import PinRef as _PinRef  # local alias

    pin_world: dict[_PinRef, Point] = {}
    net_canonicals: dict[object, list[_PinRef]] = defaultdict(list)
    for net in schematic.nets.values():
        for ref in net.pins:
            inst = instances.get(ref.component_id)
            if inst is None:
                continue
            defn = components.get(inst.definition_ref)
            placement = layout.placements.get(ref.component_id)
            if defn is None or placement is None:
                continue
            footprint = inst.resolved_footprint()
            if ref.pin_number > footprint.pin_count():
                continue
            wp = pin_world_position(footprint, placement, ref.pin_number)
            pin_world[ref] = wp
            ck = graph.node_for_pin(ref)
            if ck is not None:
                net_canonicals[ck].append(ref)

    wires_by_canonical: dict[object, list[Wire]] = defaultdict(list)
    for w in wires:
        ck0 = graph.node_for_pad(w.path[0], w.side)
        if ck0 is not None:
            wires_by_canonical[ck0].append(w)

    per_net: list[NetMetrics] = []
    redundant_total = 0
    connected_pins_total = 0
    placed_pins_total = 0
    total_pins = 0
    ratios: list[float] = []
    hull_ratios: list[float] = []

    for net in sorted(schematic.nets.values(), key=lambda n: n.name):
        total_pins += len(net.pins)
        placed_refs = [r for r in net.pins if r in pin_world]
        placed_pins_total += len(placed_refs)
        pin_pts = [pin_world[r] for r in placed_refs]

        # Find the canonical key that owns the most pins of this net.  In a
        # fully-routed net every pin shares one canonical; in partial routing
        # there may be several clusters — we pick the largest.
        cluster_sizes: dict[object, int] = defaultdict(int)
        for r in placed_refs:
            ck = graph.node_for_pin(r)
            if ck is not None:
                cluster_sizes[ck] += 1
        if cluster_sizes:
            biggest_ck, biggest_size = max(cluster_sizes.items(), key=lambda kv: kv[1])
        else:
            biggest_ck, biggest_size = None, 0
        connected_pins_total += biggest_size if len(placed_refs) >= 2 else 0

        net_wires = wires_by_canonical.get(biggest_ck, []) if biggest_ck else []
        length = _manhattan_length(net_wires)
        mst = _mst_manhattan(pin_pts)
        ratio: float | None = (length / mst) if mst > 0 else None
        if ratio is not None:
            ratios.append(ratio)

        wire_pts: list[Point] = []
        for w in net_wires:
            wire_pts.extend(w.path)
        wire_hull = _convex_hull_area(wire_pts)
        pin_hull = _pin_hull_area(pin_pts)
        hull_ratio: float | None = (wire_hull / pin_hull) if pin_hull > 0 else None
        if hull_ratio is not None:
            hull_ratios.append(hull_ratio)

        # Redundant wire count for this net: wires whose endpoints remain
        # connected after their removal.  O(wires) graph rebuilds per net.
        red = 0
        if net_wires:
            for w in net_wires:
                rest = [
                    ww for ww in (list(layout.wires) + list(wires)) if ww.id != w.id
                ]
                test_layout = replace(layout, wires=rest)
                g2 = ConnectivityGraph(
                    board=board,
                    layout=test_layout,
                    components=components,
                    instances=instances,
                )
                if g2.are_connected(w.path[0], w.side, w.path[-1], w.side):
                    red += 1
        redundant_total += red

        is_routed = net_is_complete(net, graph)
        per_net.append(
            NetMetrics(
                name=net.name,
                pin_count=len(net.pins),
                placed_pin_count=len(placed_refs),
                connected_pin_count=biggest_size,
                is_routed=is_routed,
                wire_count=len(net_wires),
                redundant_wires=red,
                length=length,
                mst_length=mst,
                wire_mst_ratio=ratio,
                wire_hull_area=wire_hull,
                pin_hull_area=pin_hull,
                hull_ratio=hull_ratio,
            )
        )

    routed_nets = sum(1 for nm in per_net if nm.is_routed)

    return RunSummary(
        wire_mode=wire_mode,
        max_rip_up_passes=max_rip_up_passes,
        max_optimize_iterations=max_optimize_iterations,
        elapsed_s=elapsed_s,
        total_nets=len(per_net),
        routed_nets=routed_nets,
        unrouted_nets=len(per_net) - routed_nets,
        total_pins=total_pins,
        placed_pins=placed_pins_total,
        connected_pins=connected_pins_total,
        total_wires=len(wires),
        redundant_wires_total=redundant_total,
        total_length=sum(nm.length for nm in per_net),
        mean_wire_mst_ratio=(sum(ratios) / len(ratios)) if ratios else None,
        mean_hull_ratio=(sum(hull_ratios) / len(hull_ratios)) if hull_ratios else None,
        per_net=per_net,
    )
