# SPDX-License-Identifier: MIT
"""A* router on a perfboard pad-graph.

Finds the top-K cheapest paths from a `source` pad to any pad in
`targets`, constrained to valid pads known to the `ConnectivityGraph`.

Graph model:
  - Nodes = (x, y, side) pad centers from ConnectivityGraph.
  - Edges  = 4-connected neighbours (±1.0 in x or y) that are valid nodes.
  - Cost   = RoutingCostModel weights per step + direction-change penalty.

Returns a list of at most K `RoutingResult`s, sorted by ascending cost.
"""

from __future__ import annotations

import heapq
import uuid
from dataclasses import dataclass

from protoboard.domain.connectivity.graph import ConnectivityGraph
from protoboard.geometry import Point
from protoboard.services.routing.cost_model import RoutingCostModel

_NEIGHBOURS = [(1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0)]

# Direction as a (dx, dy) tuple used for turn-penalty tracking.
_Dir = tuple[float, float]


@dataclass(frozen=True)
class RoutingResult:
    """One candidate routing path."""

    path: list[Point]
    cost: float


def find_paths(
    graph: ConnectivityGraph,
    source: Point,
    targets: list[Point],
    side: str,
    cost_model: RoutingCostModel,
    k: int,
    occupied_pads: dict[Point, uuid.UUID] | None = None,
    same_net_pads: dict[Point, uuid.UUID] | None = None,
) -> list[RoutingResult]:
    """Find up to *k* cheapest paths from *source* to any pad in *targets*.

    Returns an empty list if no path exists.
    """
    occupied = occupied_pads or {}
    same_net = same_net_pads or {}

    target_set = set(targets)

    # Trivial case: source is already a target.
    if source in target_set:
        return [RoutingResult(path=[source], cost=0.0)]

    # Build the set of valid node positions from the graph (only this side).
    valid_nodes: set[Point] = set()
    for node in graph._g.nodes:
        nx, ny, ns = node
        if ns == side:
            valid_nodes.add(Point(nx, ny))

    if source not in valid_nodes:
        return []

    # A* with path tracking.  State = (cost, step_count, position, direction, path).
    # We use Yen's k-shortest-paths approach: run A* repeatedly, on each run
    # forbidding the previously returned path's edges.
    # For MVP simplicity we run A* once (finds the single optimal path) then
    # perform K-1 additional runs with slight cost perturbation via spur paths.
    # A cleaner implementation of top-K is Yen's algorithm, but for K=3 and
    # small boards a simpler multi-run with used-path exclusion suffices.

    results: list[RoutingResult] = []
    forbidden_edges: set[tuple[Point, Point]] = set()

    for _ in range(k):
        result = _astar(
            valid_nodes=valid_nodes,
            source=source,
            target_set=target_set,
            cost_model=cost_model,
            occupied=occupied,
            same_net=same_net,
            forbidden_edges=forbidden_edges,
        )
        if result is None:
            break
        results.append(result)
        # Forbid the edges of the found path to force a different route next time.
        path = result.path
        for a, b in zip(path[:-1], path[1:]):
            forbidden_edges.add((a, b))
            forbidden_edges.add((b, a))

    results.sort(key=lambda r: r.cost)
    return results


def _astar(
    valid_nodes: set[Point],
    source: Point,
    target_set: set[Point],
    cost_model: RoutingCostModel,
    occupied: dict[Point, uuid.UUID],
    same_net: dict[Point, uuid.UUID],
    forbidden_edges: set[tuple[Point, Point]],
) -> RoutingResult | None:
    # heap entry: (f_cost, g_cost, position, direction, path_so_far)
    # f_cost = g_cost + heuristic (Manhattan to nearest target)
    def h(p: Point) -> float:
        return min(abs(p.x - t.x) + abs(p.y - t.y) for t in target_set)

    start_h = h(source)
    # (f, g, x, y, dir_x, dir_y, path)
    heap: list[tuple[float, float, float, float, float, float, list[Point]]] = [
        (start_h, 0.0, source.x, source.y, 0.0, 0.0, [source])
    ]
    visited: dict[tuple[float, float, float, float], float] = {}

    while heap:
        f, g, cx, cy, dx, dy, path = heapq.heappop(heap)
        pos = Point(cx, cy)
        state_key = (cx, cy, dx, dy)
        if state_key in visited and visited[state_key] <= g:
            continue
        visited[state_key] = g

        if pos in target_set:
            return RoutingResult(path=path, cost=g)

        for ndx, ndy in _NEIGHBOURS:
            npos = Point(cx + ndx, cy + ndy)
            if npos not in valid_nodes:
                continue
            if (pos, npos) in forbidden_edges:
                continue

            # Compute step cost.
            if npos in occupied:
                step_cost = cost_model.occupied_by_other_net
            elif npos in same_net:
                step_cost = cost_model.same_net_pad
            else:
                step_cost = cost_model.free_pad

            if step_cost == float("inf"):
                continue

            # Turn penalty: apply if direction changes and we had a previous dir.
            if (dx != 0.0 or dy != 0.0) and (ndx != dx or ndy != dy):
                step_cost += cost_model.direction_change

            ng = g + step_cost
            nstate = (npos.x, npos.y, ndx, ndy)
            if nstate in visited and visited[nstate] <= ng:
                continue

            nf = ng + h(npos)
            heapq.heappush(heap, (nf, ng, npos.x, npos.y, ndx, ndy, path + [npos]))

    return None


__all__ = ["RoutingResult", "find_paths"]
