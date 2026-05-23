# SPDX-License-Identifier: MIT
"""Pre-route congestion estimator.

Builds a side-agnostic congestion map by laying down trivial L-shape paths
over the MST of every net, then averaging votes from both L-shape variants
(HV and VH).  The map is fed to A* as additional per-cell cost so that long
nets routed first naturally detour around clusters where short nets will
later need corridor space.

The algorithm is a single non-iterative pass — a lightweight cousin of
probabilistic congestion maps from VLSI literature (Lou/Krishnamoorthy).
Cost: O(n² log n) per net for Kruskal MST + O(k·L) for path rasterisation,
where n is pins-per-net, k = n-1 MST edges, L = path length in cells.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from protoboard.domain.schematic.net import Net, PinRef
from protoboard.geometry import Point


def _manhattan(a: Point, b: Point) -> float:
    return abs(a.x - b.x) + abs(a.y - b.y)


def _mst_edges(points: list[Point]) -> list[tuple[Point, Point]]:
    """Kruskal MST over Manhattan distances.

    Returns ``n-1`` edges connecting all input points.  Empty for ``len<2``.
    """
    n = len(points)
    if n < 2:
        return []

    candidates: list[tuple[float, int, int]] = []
    for i in range(n):
        for j in range(i + 1, n):
            candidates.append((_manhattan(points[i], points[j]), i, j))
    candidates.sort()

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    edges: list[tuple[Point, Point]] = []
    for _, i, j in candidates:
        ri, rj = find(i), find(j)
        if ri == rj:
            continue
        parent[ri] = rj
        edges.append((points[i], points[j]))
        if len(edges) == n - 1:
            break
    return edges


def _l_shape_cells(
    a: Point, b: Point, *, horizontal_first: bool,
) -> list[Point]:
    """Integer cells on the L-shape path from *a* to *b*, excluding endpoints.

    Pad centres lie at half-integer coordinates ``(col+0.5, row+0.5)``.
    The L-shape walks from *a* in unit steps along one axis, then the other.
    Returns the intermediate cells (corner included; endpoints excluded).
    Degenerate collinear cases produce a single straight line.
    """
    if a == b:
        return []
    corner = Point(b.x, a.y) if horizontal_first else Point(a.x, b.y)

    def _walk(p: Point, q: Point) -> list[Point]:
        """Unit-step cells strictly between *p* and *q* (both excluded)."""
        if p == q:
            return []
        out: list[Point] = []
        if p.y == q.y:
            step = 1.0 if q.x > p.x else -1.0
            x = p.x + step
            while x != q.x:
                out.append(Point(x, p.y))
                x += step
        else:  # p.x == q.x
            step = 1.0 if q.y > p.y else -1.0
            y = p.y + step
            while y != q.y:
                out.append(Point(p.x, y))
                y += step
        return out

    cells: list[Point] = []
    cells.extend(_walk(a, corner))
    if corner != a and corner != b:
        cells.append(corner)
    cells.extend(_walk(corner, b))
    return cells


def build_congestion_map(
    nets: Iterable[Net],
    pin_positions: dict[PinRef, tuple[Point, str]],
) -> dict[Point, float]:
    """Return a side-agnostic congestion map keyed by board cell.

    For every net, builds a Manhattan MST over its placed pins.  For each
    MST edge, both L-shape variants (HV and VH) vote with weight 0.5 on
    every transit cell (endpoints excluded).  Corner cells of an L-shape
    are shared by both variants and end up with weight 1.0 naturally.

    Pins of the net itself do not vote on their own pads — only on the
    intermediate cells between MST endpoints.  Global nets vote normally
    (per design decision 2026-05-23).
    """
    votes: dict[Point, float] = defaultdict(float)
    for net in nets:
        pts: list[Point] = []
        seen: set[Point] = set()
        for pin in net.pins:
            entry = pin_positions.get(pin)
            if entry is None:
                continue
            p = entry[0]
            if p in seen:
                continue
            seen.add(p)
            pts.append(p)
        if len(pts) < 2:
            continue
        for a, b in _mst_edges(pts):
            for hv in (True, False):
                for cell in _l_shape_cells(a, b, horizontal_first=hv):
                    votes[cell] += 0.5
    return dict(votes)
