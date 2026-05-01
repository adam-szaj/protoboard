# SPDX-License-Identifier: MIT
"""A* router on a perfboard pad-graph.

Finds the top-K cheapest paths from a `source` pad to any pad in
`targets`, constrained to valid pads known to the `ConnectivityGraph`.

Graph model:
  - Nodes = (x, y, side) pad centers from ConnectivityGraph.
  - Edges  = 8-connected neighbours (±1 in x and/or y) that are valid nodes.
  - Cost   = RoutingCostModel weights per step + diagonal_multiplier for
             diagonals + direction-change penalty.

Wire-crossing awareness:
  - `bare_wire_segments`: set of (Point, Point) pairs covering all segments
    of existing *bare* (non-insulated) wires.  A new wire step that would
    cross such a segment pays `crossing_insulated_bare` (insulated wire) or
    `crossing_bare_bare` (bare wire, default INF).
  - Crossing detection uses integer-grid arithmetic: a horizontal/vertical
    step from A to B crosses a bare segment P→Q when the two axis-aligned
    segments intersect strictly in their interiors.  For diagonal steps the
    same check is performed after decomposing into two half-steps.

Returns a list of at most K `RoutingResult`s, sorted by ascending cost.
"""

from __future__ import annotations

import heapq
import math
import uuid
from dataclasses import dataclass

from protoboard.domain.connectivity.graph import ConnectivityGraph
from protoboard.geometry import Point
from protoboard.services.routing.cost_model import RoutingCostModel

# 4 orthogonal + 4 diagonal neighbours, each with its distance multiplier.
_NEIGHBOURS: list[tuple[float, float, float]] = [
    (1.0,  0.0, 1.0),
    (-1.0, 0.0, 1.0),
    (0.0,  1.0, 1.0),
    (0.0, -1.0, 1.0),
    (1.0,  1.0, math.sqrt(2)),
    (1.0, -1.0, math.sqrt(2)),
    (-1.0, 1.0, math.sqrt(2)),
    (-1.0,-1.0, math.sqrt(2)),
]


@dataclass(frozen=True)
class RoutingResult:
    """One candidate routing path."""

    path: list[Point]
    cost: float
    insulated: bool = True  # whether the wire should be marked insulated


def find_paths(
    graph: ConnectivityGraph,
    source: Point,
    targets: list[Point],
    side: str,
    cost_model: RoutingCostModel,
    k: int,
    insulated: bool = True,
    occupied_pads: dict[Point, uuid.UUID] | None = None,
    same_net_pads: dict[Point, uuid.UUID] | None = None,
    near_pin_pads: set[Point] | None = None,
    near_pin_cost: float = 10.0,
    bare_wire_segments: set[tuple[Point, Point]] | None = None,
) -> list[RoutingResult]:
    """Find up to *k* cheapest paths from *source* to any pad in *targets*.

    Parameters
    ----------
    insulated:
        Whether the wire being routed is insulated.  A bare wire cannot cross
        any existing bare wire segment; an insulated wire may cross but pays
        `cost_model.crossing_insulated_bare` per crossing.
    bare_wire_segments:
        Set of (p1, p2) pairs representing segments of existing bare wires.
        Used for crossing-cost calculation.
    near_pin_pads:
        Pads adjacent to pins of other nets — allowed but penalised by
        *near_pin_cost* to discourage corridor-stealing detours.
    """
    occupied = occupied_pads or {}
    same_net = same_net_pads or {}
    near_pins = near_pin_pads or set()
    bare_segs = bare_wire_segments or set()

    target_set = set(targets)

    if source in target_set:
        return [RoutingResult(path=[source], cost=0.0, insulated=insulated)]

    valid_nodes: set[Point] = set()
    for node in graph._g.nodes:
        nx, ny, ns = node
        if ns == side:
            valid_nodes.add(Point(nx, ny))

    if source not in valid_nodes:
        return []

    results: list[RoutingResult] = []
    forbidden_edges: set[tuple[Point, Point]] = set()

    for _ in range(k):
        result = _astar(
            valid_nodes=valid_nodes,
            source=source,
            target_set=target_set,
            cost_model=cost_model,
            insulated=insulated,
            occupied=occupied,
            same_net=same_net,
            near_pins=near_pins,
            near_pin_cost=near_pin_cost,
            bare_segs=bare_segs,
            forbidden_edges=forbidden_edges,
        )
        if result is None:
            break
        results.append(
            RoutingResult(path=result.path, cost=result.cost, insulated=insulated)
        )
        for a, b in zip(result.path[:-1], result.path[1:]):
            forbidden_edges.add((a, b))
            forbidden_edges.add((b, a))

    results.sort(key=lambda r: r.cost)
    return results


def _astar(
    valid_nodes: set[Point],
    source: Point,
    target_set: set[Point],
    cost_model: RoutingCostModel,
    insulated: bool,
    occupied: dict[Point, uuid.UUID],
    same_net: dict[Point, uuid.UUID],
    near_pins: set[Point],
    near_pin_cost: float,
    bare_segs: set[tuple[Point, Point]],
    forbidden_edges: set[tuple[Point, Point]],
) -> RoutingResult | None:
    # Euclidean heuristic (admissible for 8-connectivity with √2 diagonal cost).
    def h(p: Point) -> float:
        return min(math.sqrt((p.x - t.x)**2 + (p.y - t.y)**2) for t in target_set)

    _seq = 0
    heap: list[tuple[float, float, int, float, float, float, float, list[Point]]] = [
        (h(source), 0.0, _seq, source.x, source.y, 0.0, 0.0, [source])
    ]
    visited: dict[tuple[float, float, float, float], float] = {}

    while heap:
        _f, g, _s, cx, cy, dx, dy, path = heapq.heappop(heap)
        pos = Point(cx, cy)
        state_key = (cx, cy, dx, dy)
        if state_key in visited and visited[state_key] <= g:
            continue
        visited[state_key] = g

        if pos in target_set:
            return RoutingResult(path=path, cost=g)

        for ndx, ndy, dist_mult in _NEIGHBOURS:
            npos = Point(cx + ndx, cy + ndy)
            if npos not in valid_nodes:
                continue
            if (pos, npos) in forbidden_edges:
                continue

            # Base step cost.
            if npos in occupied:
                step_cost = cost_model.occupied_by_other_net
            elif npos in same_net:
                step_cost = cost_model.same_net_pad * dist_mult
            else:
                step_cost = cost_model.free_pad * dist_mult
                if npos in near_pins:
                    step_cost += near_pin_cost

            if step_cost >= float("inf"):
                continue

            # Wire-crossing cost for this step.
            crossing_cost = _crossing_cost(
                pos, npos, bare_segs, cost_model, insulated
            )
            if crossing_cost >= float("inf"):
                continue
            step_cost += crossing_cost

            # Turn penalty.
            if (dx != 0.0 or dy != 0.0) and (ndx != dx or ndy != dy):
                step_cost += cost_model.direction_change

            ng = g + step_cost
            nstate = (npos.x, npos.y, ndx, ndy)
            if nstate in visited and visited[nstate] <= ng:
                continue

            nf = ng + h(npos)
            _seq += 1
            heapq.heappush(
                heap, (nf, ng, _seq, npos.x, npos.y, ndx, ndy, path + [npos])
            )

    return None


def _crossing_cost(
    p1: Point,
    p2: Point,
    bare_segs: set[tuple[Point, Point]],
    cost_model: RoutingCostModel,
    insulated: bool,
) -> float:
    """Return the extra cost for the step p1→p2 due to bare-wire crossings.

    For orthogonal steps, check if this segment crosses any bare segment.
    For diagonal steps, check both half-steps (p1→mid and mid→p2 where
    mid = (p1+p2)/2 rounded to grid) — a diagonal crosses a segment if
    either orthogonal decomposition does.
    """
    if not bare_segs:
        return 0.0

    dx = p2.x - p1.x
    dy = p2.y - p1.y
    is_diagonal = dx != 0.0 and dy != 0.0

    crossing_cost = (
        cost_model.crossing_insulated_bare if insulated
        else cost_model.crossing_bare_bare
    )

    if is_diagonal:
        # Decompose diagonal into two orthogonal half-steps for crossing check.
        # Check both possible decompositions (H-then-V and V-then-H).
        mid1 = Point(p1.x + dx, p1.y)   # H first
        mid2 = Point(p1.x,      p1.y + dy)  # V first
        crosses = (
            _seg_crosses_any(p1, mid1, bare_segs) or
            _seg_crosses_any(mid1, p2, bare_segs) or
            _seg_crosses_any(p1, mid2, bare_segs) or
            _seg_crosses_any(mid2, p2, bare_segs)
        )
    else:
        crosses = _seg_crosses_any(p1, p2, bare_segs)

    if not crosses:
        return 0.0
    return crossing_cost


def _seg_crosses_any(
    p1: Point, p2: Point, bare_segs: set[tuple[Point, Point]]
) -> bool:
    """Return True if axis-aligned segment p1→p2 strictly crosses any bare segment."""
    for bsrc, bdst in bare_segs:
        if _segs_cross(p1, p2, bsrc, bdst):
            return True
    return False


def _segs_cross(
    a1: Point, a2: Point, b1: Point, b2: Point
) -> bool:
    """Return True if two axis-aligned segments cross strictly in their interiors."""
    a_h = a1.y == a2.y  # a is horizontal
    b_h = b1.y == b2.y  # b is horizontal

    if a_h == b_h:
        # Parallel (both H or both V) — can overlap but don't cross.
        return False

    if a_h:
        # a horizontal, b vertical
        bx = b1.x
        ay = a1.y
        ax_min, ax_max = (a1.x, a2.x) if a1.x < a2.x else (a2.x, a1.x)
        by_min, by_max = (b1.y, b2.y) if b1.y < b2.y else (b2.y, b1.y)
        return ax_min < bx < ax_max and by_min < ay < by_max
    else:
        # a vertical, b horizontal
        ax = a1.x
        by = b1.y
        bx_min, bx_max = (b1.x, b2.x) if b1.x < b2.x else (b2.x, b1.x)
        ay_min, ay_max = (a1.y, a2.y) if a1.y < a2.y else (a2.y, a1.y)
        return bx_min < ax < bx_max and ay_min < by < ay_max


__all__ = ["RoutingResult", "find_paths"]
