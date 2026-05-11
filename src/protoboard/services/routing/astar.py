# SPDX-License-Identifier: MIT
"""A* router on a perfboard pad-graph.

Finds the top-K cheapest paths from a `source` pad to any pad in
`targets`, constrained to valid pads known to the `ConnectivityGraph`.

Graph model:
  - Nodes = (x, y, side) pad centers from ConnectivityGraph.
  - Horizontal/vertical/diagonal neighbours on the same layer.
  - Layer change: (x, y, "top") ↔ (x, y, "bottom") allowed only at
    plated-through pads (zero horizontal movement, costs `layer_change`).

Wire-crossing awareness:
  - `bare_wire_segments`: set of (Point, Point, side) triples covering all
    segments of existing *bare* (non-insulated) wires.  A new wire step that
    would cross such a segment on the same layer pays
    `crossing_insulated_bare` (insulated wire) or `crossing_bare_bare`
    (bare wire, default INF).
  - Crossing detection uses integer-grid arithmetic: a horizontal/vertical
    step from A to B crosses a bare segment P→Q when the two axis-aligned
    segments intersect strictly in their interiors.  For diagonal steps the
    same check is performed after decomposing into two half-steps.

Returns a list of at most K `RoutingResult`s, sorted by ascending cost.

`RoutingResult.path` is a list of `(Point, side)` pairs so the caller can
split the result into per-layer wire segments.

Parallel universe A*:
  When `_UNIVERSE_POOL` has available capacity, `_astar_parallel()` is used
  instead of the sequential `_astar()`.  At each node with ≥2 valid neighbours,
  if the pool has free slots, the search branches: each neighbour gets a
  copy-on-write snapshot of the current heap+visited and continues
  independently as a separate "universe".  When the first universe reaches
  the target, remaining universes are given an extra budget of N/2 nodes
  (where N is the node count of the first finder) to look for a cheaper path.
  The lowest-cost result wins.
"""

from __future__ import annotations

import heapq
import math
import os
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from queue import Empty, Queue

import structlog

from protoboard.domain.connectivity.graph import ConnectivityGraph
from protoboard.geometry import Point
from protoboard.services.routing.cost_model import RoutingCostModel

_log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Rust extension (optional — true parallelism without GIL)
# ---------------------------------------------------------------------------
try:
    import protoboard_astar_rs as _rs  # type: ignore[import-not-found]
    _RUST_AVAILABLE = True
    import os as _os
    _log.info("astar.rust_extension", status="loaded", cpu_count=_os.cpu_count())
except ImportError:
    _RUST_AVAILABLE = False
    _log.info("astar.rust_extension", status="not_available_using_python_fallback")


def _find_paths_rust(
    *,
    valid_nodes: dict[str, set[Point]],
    source: Point,
    source_side: str,
    target_set: set[Point],
    cost_model: RoutingCostModel,
    insulated: bool,
    occupied: dict[tuple[Point, str], uuid.UUID],
    same_net: dict[tuple[Point, str], uuid.UUID],
    near_pins: set[tuple[Point, str]],
    near_pin_cost: float,
    bare_segs: set[tuple[Point, Point, str]],
    vias: set[Point],
    penalised: dict[tuple[Point, str], float],
    extra_budget_ratio: float,
    k: int,
) -> list[RoutingResult]:
    """Call the Rust A* extension and convert results back to Python types."""
    raw = _rs.find_paths_parallel(
        [(p.x, p.y) for p in valid_nodes["top"]],
        [(p.x, p.y) for p in valid_nodes["bottom"]],
        source.x, source.y, source_side,
        [(p.x, p.y) for p in target_set],
        cost_model.free_pad,
        cost_model.same_net_pad,
        cost_model.occupied_by_other_net,
        cost_model.direction_change,
        cost_model.layer_change,
        cost_model.crossing_insulated_bare,
        cost_model.crossing_bare_bare,
        near_pin_cost,
        insulated,
        [(p.x, p.y, s) for (p, s) in occupied],
        [(p.x, p.y, s) for (p, s) in same_net],
        [(p.x, p.y, s) for (p, s) in near_pins],
        [(p.x, p.y, s, v) for (p, s), v in penalised.items()],
        [(p1.x, p1.y, p2.x, p2.y, s) for (p1, p2, s) in bare_segs],
        [(p.x, p.y) for p in vias],
        [],  # forbidden edges — handled inside Rust per-k iteration
        extra_budget_ratio,
        k,
    )
    results = [
        RoutingResult(
            path=[(Point(x, y), s) for x, y, s in path],
            cost=cost,
            insulated=insulated,
        )
        for path, cost in raw
    ]
    results.sort(key=lambda r: r.cost)
    return results


# Spatial index for bare wire segments: (cell_x, cell_y, side) → [(p1, p2), ...]
# Each segment is stored under every integer cell its bounding box covers.
# Lookup: for a step covering bbox (x0..x1, y0..y1) query all overlapping cells.
BareSegIndex = dict[tuple[int, int, str], list[tuple[Point, Point]]]


def _build_bare_seg_index(bare_segs: set[tuple[Point, Point, str]]) -> BareSegIndex:
    idx: BareSegIndex = {}
    for p1, p2, side in bare_segs:
        x0 = int(math.floor(min(p1.x, p2.x)))
        x1 = int(math.floor(max(p1.x, p2.x)))
        y0 = int(math.floor(min(p1.y, p2.y)))
        y1 = int(math.floor(max(p1.y, p2.y)))
        for cx in range(x0, x1 + 1):
            for cy in range(y0, y1 + 1):
                key = (cx, cy, side)
                if key not in idx:
                    idx[key] = []
                idx[key].append((p1, p2))
    return idx


# 4 orthogonal + 4 diagonal neighbours (same layer), each with its distance
# multiplier.  Layer changes are handled separately.
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

_SIDES: tuple[str, str] = ("top", "bottom")
_OTHER_SIDE: dict[str, str] = {"top": "bottom", "bottom": "top"}

# A routed path step: grid position + which layer it lives on.
PathStep = tuple[Point, str]

# Thread pool for parallel universe A*.  Shared across all find_paths calls.
# Daemon threads so they don't block process exit.
_CPU_COUNT = max(2, os.cpu_count() or 2)
_UNIVERSE_POOL = ThreadPoolExecutor(max_workers=_CPU_COUNT, thread_name_prefix="astar_universe")


@dataclass(frozen=True)
class RoutingResult:
    """One candidate routing path.

    `path` is a list of (Point, side) pairs.  Consecutive entries that share
    the same side form a wire segment on that layer; a change in side
    represents a layer transition through a plated-through via.
    """

    path: list[PathStep]
    cost: float
    insulated: bool = True  # whether the wire should be marked insulated
    nodes_expanded: int = 0
    over_budget_hits: int = 0


@dataclass
class _SharedState:
    """Shared state across all universe workers for one A* search."""

    result_queue: Queue[RoutingResult]
    # Node count when the first universe found the target; None until then.
    first_found_nodes: int | None = None
    # Extra budget ratio: remaining universes may expand this many more nodes
    # after first_found_nodes is set (0.5 = N/2 extra nodes).
    extra_budget_ratio: float = 0.5
    # Lock protecting first_found_nodes, active_count, and futures list.
    _lock: threading.Lock = field(default_factory=threading.Lock)
    # All submitted futures — needed to wait for completion in _astar_parallel.
    futures: list[Future[None]] = field(default_factory=list)
    # Count of futures not yet done — O(1) update instead of O(N) scan.
    active_count: int = 0
    # Cumulative stats across all universes for this search.
    total_nodes_expanded: int = 0
    over_budget_hits: int = 0

    def record_found(self, nodes_expanded: int) -> None:
        with self._lock:
            if self.first_found_nodes is None:
                self.first_found_nodes = nodes_expanded

    def over_budget(self, nodes_expanded: int) -> bool:
        ffn = self.first_found_nodes  # read without lock (int assignment is atomic in CPython)
        if ffn is None:
            return False
        budget = ffn + int(ffn * self.extra_budget_ratio)
        if nodes_expanded > budget:
            self.over_budget_hits += 1  # CPython int incr is atomic
            return True
        return False

    def record_nodes(self, n: int) -> None:
        self.total_nodes_expanded += n  # CPython int incr is atomic

    def add_future(self, fut: Future[None]) -> None:
        with self._lock:
            self.futures.append(fut)
            self.active_count += 1

    def mark_done(self) -> None:
        """Called by each universe worker when it finishes."""
        with self._lock:
            self.active_count -= 1


_HE = tuple[float, float, int, float, float, int, float, float, list[PathStep]]


def _universe_worker(
    heap: list[_HE],
    visited: dict[tuple[float, float, int, float, float], float],
    nodes_expanded: int,
    seq_counter: int,
    shared: _SharedState,
    # Read-only routing context (shared across all universes):
    valid_nodes: dict[str, set[Point]],
    target_set: set[Point],
    # Precomputed (tx, ty) pairs for O(1) heuristic — avoids Point allocation in h().
    target_coords: list[tuple[float, float]],
    cost_model: RoutingCostModel,
    insulated: bool,
    occupied: dict[tuple[Point, str], uuid.UUID],
    same_net: dict[tuple[Point, str], uuid.UUID],
    near_pins: set[tuple[Point, str]],
    near_pin_cost: float,
    bare_idx: BareSegIndex,
    vias: set[Point],
    forbidden_edges: set[tuple[Point, str, Point, str]],
    penalised: dict[tuple[Point, str], float],
) -> None:
    """Run one A* universe until it finds a path, exhausts the heap, or exceeds budget."""

    def h(px: float, py: float) -> float:
        best = float("inf")
        for tx, ty in target_coords:
            d2 = (px - tx) ** 2 + (py - ty) ** 2
            if d2 < best:
                best = d2
        return math.sqrt(best)

    # Convert Point-based structures to float-tuple keys — avoids Point.__init__
    # and Point.__hash__ on every neighbour lookup in the hot loop.
    _vn_top:    set[tuple[float, float]] = {(p.x, p.y) for p in valid_nodes.get("top", set())}
    _vn_bottom: set[tuple[float, float]] = {(p.x, p.y) for p in valid_nodes.get("bottom", set())}
    _vn = (_vn_top, _vn_bottom)  # indexed by cs_idx (0=top, 1=bottom)
    _tgt: set[tuple[float, float]] = {(p.x, p.y) for p in target_set}
    _via: set[tuple[float, float]] = {(p.x, p.y) for p in vias}
    _occ:  dict[tuple[float, float, str], float] = {
        (p.x, p.y, s): cost_model.occupied_by_other_net for (p, s) in occupied
    }
    _same: dict[tuple[float, float, str], float] = {
        (p.x, p.y, s): cost_model.same_net_pad for (p, s) in same_net
    }
    _near: set[tuple[float, float, str]] = {(p.x, p.y, s) for (p, s) in near_pins}
    _pen:  dict[tuple[float, float, str], float] = {
        (p.x, p.y, s): v for (p, s), v in penalised.items()
    }
    _fe:   set[tuple[float, float, str, float, float, str]] = {
        (pa.x, pa.y, sa, pb.x, pb.y, sb) for (pa, sa, pb, sb) in forbidden_edges
    }
    _free = cost_model.free_pad
    _inf = float("inf")
    _dc  = cost_model.direction_change
    _lc  = cost_model.layer_change

    _seq = seq_counter

    try:
        while heap:
            if shared.over_budget(nodes_expanded):
                return

            _f, g, _s, cx, cy, cs_idx, dx, dy, path = heapq.heappop(heap)
            cur_side = _SIDES[cs_idx]
            state_key = (cx, cy, cs_idx, dx, dy)

            if visited.get(state_key, _inf) <= g:
                continue
            visited[state_key] = g
            nodes_expanded += 1

            if (cx, cy) in _tgt:
                shared.record_found(nodes_expanded)
                shared.result_queue.put(RoutingResult(
                    path=path, cost=g, insulated=insulated,
                    nodes_expanded=nodes_expanded,
                ))
                return

            neighbor_entries: list[_HE] = []
            _vn_cur = _vn[cs_idx]
            pos = Point(cx, cy)

            for ndx, ndy, dist_mult in _NEIGHBOURS:
                nx = cx + ndx
                ny = cy + ndy
                if (nx, ny) not in _vn_cur:
                    continue
                if (cx, cy, cur_side, nx, ny, cur_side) in _fe:
                    continue

                # Inlined _step_cost using float-tuple dicts.
                _nkey = (nx, ny, cur_side)
                if _nkey in _occ:
                    continue
                _sc = _same.get(_nkey)
                if _sc is not None:
                    step_cost = _sc * dist_mult
                else:
                    step_cost = _free * dist_mult
                    if _nkey in _near:
                        step_cost += near_pin_cost
                    _ex = _pen.get(_nkey)
                    if _ex is not None:
                        step_cost += _ex

                # Point created here — needed for _crossing_cost and path.
                npos = Point(nx, ny)
                crossing_cost = _crossing_cost(pos, npos, cur_side, bare_idx, cost_model, insulated)
                if crossing_cost >= _inf:
                    continue
                step_cost += crossing_cost

                if (dx != 0.0 or dy != 0.0) and (ndx != dx or ndy != dy):
                    step_cost += _dc

                ng = g + step_cost
                nstate = (nx, ny, cs_idx, ndx, ndy)
                if nstate in visited and visited[nstate] <= ng:
                    continue

                nf = ng + h(nx, ny)
                _seq += 1
                neighbor_entries.append(
                    (nf, ng, _seq, nx, ny, cs_idx, ndx, ndy, path + [(npos, cur_side)])
                )

            via_entry: _HE | None = None
            if (cx, cy) in _via:
                other_side = _OTHER_SIDE[cur_side]
                other_idx = 1 - cs_idx
                if (cx, cy) in _vn[other_idx]:
                    if (cx, cy, cur_side, cx, cy, other_side) not in _fe:
                        ng = g + _lc
                        nstate = (cx, cy, other_idx, 0.0, 0.0)
                        if not (nstate in visited and visited[nstate] <= ng):
                            nf = ng + h(cx, cy)
                            _seq += 1
                            via_entry = (nf, ng, _seq, cx, cy, other_idx, 0.0, 0.0, path + [(pos, other_side)])

            if via_entry is not None:
                neighbor_entries.append(via_entry)

            if not neighbor_entries:
                continue

            for entry in neighbor_entries:
                    heapq.heappush(heap, entry)
    finally:
        shared.record_nodes(nodes_expanded)
        shared.mark_done()


def _astar_parallel(
    valid_nodes: dict[str, set[Point]],
    source: Point,
    source_side: str,
    target_set: set[Point],
    cost_model: RoutingCostModel,
    insulated: bool,
    occupied: dict[tuple[Point, str], uuid.UUID],
    same_net: dict[tuple[Point, str], uuid.UUID],
    near_pins: set[tuple[Point, str]],
    near_pin_cost: float,
    bare_idx: BareSegIndex,
    vias: set[Point],
    forbidden_edges: set[tuple[Point, str, Point, str]],
    penalised: dict[tuple[Point, str], float],
    extra_budget_ratio: float = 0.5,
) -> RoutingResult | None:
    """Parallel-universe A* — may spawn sub-searches on the thread pool.

    Returns the cheapest path found across all universes, or None if unreachable.
    """
    import time
    from concurrent.futures import wait as _wait, ALL_COMPLETED

    # Precompute target coords once — workers use floats directly, no Point alloc.
    target_coords: list[tuple[float, float]] = [(t.x, t.y) for t in target_set]

    def h0(px: float, py: float) -> float:
        best = float("inf")
        for tx, ty in target_coords:
            d2 = (px - tx) ** 2 + (py - ty) ** 2
            if d2 < best:
                best = d2
        return math.sqrt(best)

    side_idx = 0 if source_side == "top" else 1
    initial_heap: list[_HE] = [
        (h0(source.x, source.y), 0.0, 0, source.x, source.y, side_idx, 0.0, 0.0, [(source, source_side)])
    ]
    initial_visited: dict[tuple[float, float, int, float, float], float] = {}

    shared = _SharedState(
        result_queue=Queue(),
        extra_budget_ratio=extra_budget_ratio,
    )

    # Submit the root universe.
    fut = _UNIVERSE_POOL.submit(
        _universe_worker,
        initial_heap, initial_visited, 0, 0, shared,
        valid_nodes, target_set, target_coords, cost_model, insulated,
        occupied, same_net, near_pins, near_pin_cost,
        bare_idx, vias, forbidden_edges, penalised,
    )
    shared.add_future(fut)

    # Wait using active_count (O(1)) instead of scanning futures list (O(N)).
    # Poll until all universe workers have called mark_done().
    _MAX_WAIT_S = 5.0
    _POLL_INTERVAL_S = 0.02
    deadline = time.monotonic() + _MAX_WAIT_S
    while time.monotonic() < deadline:
        with shared._lock:
            if shared.active_count == 0:
                break
            # Snapshot a small slice of futures for the wait call so we get
            # notified promptly rather than sleeping the full poll interval.
            pending_sample = [f for f in shared.futures[-_CPU_COUNT:] if not f.done()]
        if pending_sample:
            remaining = deadline - time.monotonic()
            _wait(pending_sample, timeout=min(_POLL_INTERVAL_S, remaining), return_when=ALL_COMPLETED)
        else:
            # No recently-added futures to wait on — short sleep to avoid busy spin.
            time.sleep(_POLL_INTERVAL_S)

    # Drain all results from the queue.
    results: list[RoutingResult] = []
    while True:
        try:
            results.append(shared.result_queue.get_nowait())
        except Empty:
            break

    if not results:
        return None
    best = min(results, key=lambda r: r.cost)
    # Attach cumulative stats from shared state.
    return RoutingResult(
        path=best.path,
        cost=best.cost,
        insulated=best.insulated,
        nodes_expanded=shared.total_nodes_expanded,
        over_budget_hits=shared.over_budget_hits,
    )


def find_paths(
    graph: ConnectivityGraph,
    source: Point,
    targets: list[Point],
    side: str,
    cost_model: RoutingCostModel,
    k: int,
    insulated: bool = True,
    occupied_pads: dict[tuple[Point, str], uuid.UUID] | None = None,
    same_net_pads: dict[tuple[Point, str], uuid.UUID] | None = None,
    near_pin_pads: set[tuple[Point, str]] | None = None,
    near_pin_cost: float = 10.0,
    bare_wire_segments: set[tuple[Point, Point, str]] | None = None,
    plated_through_pads: set[Point] | None = None,
    penalised_pads: dict[tuple[Point, str], float] | None = None,
    extra_budget_ratio: float = 0.5,
) -> list[RoutingResult]:
    """Find up to *k* cheapest paths from *source* to any pad in *targets*.

    Parameters
    ----------
    side:
        Starting layer for the route.
    plated_through_pads:
        Set of pad positions that have both top and bottom layers connected
        (plated-through holes).  The router may switch layers at these points
        at the cost of `cost_model.layer_change`.  Pass `None` (or empty set)
        to disable multi-layer routing (single-layer behaviour).
    insulated:
        Whether the wire being routed is insulated.  A bare wire cannot cross
        any existing bare wire segment; an insulated wire may cross but pays
        `cost_model.crossing_insulated_bare` per crossing.
    bare_wire_segments:
        Set of (p1, p2, side) triples representing segments of existing bare
        wires.  Used for crossing-cost calculation.
    near_pin_pads:
        Pads adjacent to pins of other nets — allowed but penalised by
        *near_pin_cost* to discourage corridor-stealing detours.
    penalised_pads:
        Map of (Point, side) → extra cost.  Steps onto these pads pay the
        base step cost plus the extra penalty.  Used to discourage routing
        on the top layer beneath component bodies.
    extra_budget_ratio:
        After the first universe finds a path, remaining universes may expand
        this fraction more nodes (e.g. 0.5 = N/2 extra).  0 = stop immediately,
        1.0 = double the budget.
    """
    occupied = occupied_pads or {}
    same_net = same_net_pads or {}
    near_pins: set[tuple[Point, str]] = near_pin_pads or set()
    bare_segs = bare_wire_segments or set()
    vias = plated_through_pads or set()
    penalised = penalised_pads or {}

    target_set = set(targets)

    if source in target_set:
        return [RoutingResult(path=[(source, side)], cost=0.0, insulated=insulated)]

    # valid_nodes is cached on the graph — O(1) after first call, rebuilt only
    # when add_wire/remove_wire mutates the graph.
    valid_nodes = graph.valid_nodes_by_side()

    if source not in valid_nodes[side]:
        return []

    # Build spatial index for bare segments once per find_paths call.
    bare_idx = _build_bare_seg_index(bare_segs)

    # --- Rust extension path (true parallelism, no GIL) ---
    if _RUST_AVAILABLE:
        return _find_paths_rust(
            valid_nodes=valid_nodes,
            source=source,
            source_side=side,
            target_set=target_set,
            cost_model=cost_model,
            insulated=insulated,
            occupied=occupied,
            same_net=same_net,
            near_pins=near_pins,
            near_pin_cost=near_pin_cost,
            bare_segs=bare_segs,
            vias=vias,
            penalised=penalised,
            extra_budget_ratio=extra_budget_ratio,
            k=k,
        )

    # --- Python fallback ---
    # _astar_parallel provides an over_budget cutoff (N/2 extra nodes after
    # the first universe finds the target) that limits expansion on hard routes.
    # Without it, _astar() explores unboundedly and can be 8× slower on dense
    # boards where bare_segs crossing checks dominate.
    results: list[RoutingResult] = []
    forbidden_edges: set[tuple[Point, str, Point, str]] = set()

    for _ in range(k):
        result = _astar_parallel(
            valid_nodes=valid_nodes,
            source=source,
            source_side=side,
            target_set=target_set,
            cost_model=cost_model,
            insulated=insulated,
            occupied=occupied,
            same_net=same_net,
            near_pins=near_pins,
            near_pin_cost=near_pin_cost,
            bare_idx=bare_idx,
            vias=vias,
            forbidden_edges=forbidden_edges,
            penalised=penalised,
            extra_budget_ratio=extra_budget_ratio,
        )
        if result is None:
            break
        results.append(
            RoutingResult(path=result.path, cost=result.cost, insulated=insulated)
        )
        for (pa, sa), (pb, sb) in zip(result.path[:-1], result.path[1:]):
            forbidden_edges.add((pa, sa, pb, sb))
            forbidden_edges.add((pb, sb, pa, sa))

    results.sort(key=lambda r: r.cost)
    return results


def _astar(
    valid_nodes: dict[str, set[Point]],
    source: Point,
    source_side: str,
    target_set: set[Point],
    cost_model: RoutingCostModel,
    insulated: bool,
    occupied: dict[tuple[Point, str], uuid.UUID],
    same_net: dict[tuple[Point, str], uuid.UUID],
    near_pins: set[tuple[Point, str]],
    near_pin_cost: float,
    bare_idx: BareSegIndex,
    vias: set[Point],
    forbidden_edges: set[tuple[Point, str, Point, str]],
    penalised: dict[tuple[Point, str], float],
) -> RoutingResult | None:
    """Sequential A*."""
    target_coords = [(t.x, t.y) for t in target_set]

    def h(px: float, py: float) -> float:
        best = float("inf")
        for tx, ty in target_coords:
            d2 = (px - tx) ** 2 + (py - ty) ** 2
            if d2 < best:
                best = d2
        return math.sqrt(best)

    _seq = 0
    side_idx = 0 if source_side == "top" else 1
    heap: list[tuple[float, float, int, float, float, int, float, float, list[PathStep]]] = [
        (h(source.x, source.y), 0.0, _seq, source.x, source.y, side_idx, 0.0, 0.0, [(source, source_side)])
    ]
    visited: dict[tuple[float, float, int, float, float], float] = {}

    while heap:
        _f, g, _s, cx, cy, cs_idx, dx, dy, path = heapq.heappop(heap)
        pos = Point(cx, cy)
        cur_side = _SIDES[cs_idx]
        state_key = (cx, cy, cs_idx, dx, dy)
        if state_key in visited and visited[state_key] <= g:
            continue
        visited[state_key] = g

        if pos in target_set:
            return RoutingResult(path=path, cost=g)

        for ndx, ndy, dist_mult in _NEIGHBOURS:
            npos = Point(cx + ndx, cy + ndy)
            if npos not in valid_nodes[cur_side]:
                continue
            if (pos, cur_side, npos, cur_side) in forbidden_edges:
                continue

            step_cost = _step_cost(
                npos, cur_side, dist_mult, cost_model, occupied, same_net, near_pins, near_pin_cost, penalised
            )
            if step_cost >= float("inf"):
                continue

            crossing_cost = _crossing_cost(
                pos, npos, cur_side, bare_idx, cost_model, insulated
            )
            if crossing_cost >= float("inf"):
                continue
            step_cost += crossing_cost

            if (dx != 0.0 or dy != 0.0) and (ndx != dx or ndy != dy):
                step_cost += cost_model.direction_change

            ng = g + step_cost
            nstate = (npos.x, npos.y, cs_idx, ndx, ndy)
            if nstate in visited and visited[nstate] <= ng:
                continue

            nf = ng + h(npos.x, npos.y)
            _seq += 1
            heapq.heappush(
                heap, (nf, ng, _seq, npos.x, npos.y, cs_idx, ndx, ndy, path + [(npos, cur_side)])
            )

        if pos in vias:
            other_side = _OTHER_SIDE[cur_side]
            other_idx = 1 - cs_idx
            if pos in valid_nodes[other_side]:
                if (pos, cur_side, pos, other_side) not in forbidden_edges:
                    ng = g + cost_model.layer_change
                    nstate = (pos.x, pos.y, other_idx, 0.0, 0.0)
                    if not (nstate in visited and visited[nstate] <= ng):
                        nf = ng + h(cx, cy)
                        _seq += 1
                        heapq.heappush(
                            heap, (nf, ng, _seq, pos.x, pos.y, other_idx, 0.0, 0.0, path + [(pos, other_side)])
                        )

    return None


def _step_cost(
    npos: Point,
    side: str,
    dist_mult: float,
    cost_model: RoutingCostModel,
    occupied: dict[tuple[Point, str], uuid.UUID],
    same_net: dict[tuple[Point, str], uuid.UUID],
    near_pins: set[tuple[Point, str]],
    near_pin_cost: float,
    penalised: dict[tuple[Point, str], float],
) -> float:
    key = (npos, side)
    if key in occupied:
        return cost_model.occupied_by_other_net
    if key in same_net:
        return cost_model.same_net_pad * dist_mult
    cost = cost_model.free_pad * dist_mult
    if key in near_pins:
        cost += near_pin_cost
    extra = penalised.get(key)
    if extra is not None:
        cost += extra
    return cost


def _crossing_cost(
    p1: Point,
    p2: Point,
    side: str,
    bare_idx: BareSegIndex,
    cost_model: RoutingCostModel,
    insulated: bool,
) -> float:
    """Return the extra cost for the step p1→p2 due to bare-wire crossings.

    Only segments on the same layer can cross each other.
    For orthogonal steps, check if this segment crosses any bare segment.
    For diagonal steps, check both half-steps.
    """
    if not bare_idx:
        return 0.0

    dx = p2.x - p1.x
    dy = p2.y - p1.y
    is_diagonal = dx != 0.0 and dy != 0.0

    crossing_cost = (
        cost_model.crossing_insulated_bare if insulated
        else cost_model.crossing_bare_bare
    )

    if is_diagonal:
        mid1 = Point(p1.x + dx, p1.y)
        mid2 = Point(p1.x,      p1.y + dy)
        crosses = (
            _seg_crosses_any(p1, mid1, side, bare_idx) or
            _seg_crosses_any(mid1, p2, side, bare_idx) or
            _seg_crosses_any(p1, mid2, side, bare_idx) or
            _seg_crosses_any(mid2, p2, side, bare_idx)
        )
    else:
        crosses = _seg_crosses_any(p1, p2, side, bare_idx)

    if not crosses:
        return 0.0
    return crossing_cost


def _seg_crosses_any(
    p1: Point, p2: Point, side: str, bare_idx: BareSegIndex
) -> bool:
    """Return True if axis-aligned segment p1→p2 strictly crosses any bare segment on the same layer."""
    x0 = int(math.floor(min(p1.x, p2.x)))
    x1 = int(math.floor(max(p1.x, p2.x)))
    y0 = int(math.floor(min(p1.y, p2.y)))
    y1 = int(math.floor(max(p1.y, p2.y)))
    seen: set[int] = set()
    for cx in range(x0, x1 + 1):
        for cy in range(y0, y1 + 1):
            segs = bare_idx.get((cx, cy, side))
            if not segs:
                continue
            for bsrc, bdst in segs:
                seg_id = id(bsrc)  # each tuple is unique; id() is O(1)
                if seg_id in seen:
                    continue
                seen.add(seg_id)
                if _segs_cross(p1, p2, bsrc, bdst):
                    return True
    return False


def _segs_cross(
    a1: Point, a2: Point, b1: Point, b2: Point
) -> bool:
    """Return True if two axis-aligned segments cross strictly in their interiors."""
    a_h = a1.y == a2.y
    b_h = b1.y == b2.y

    if a_h == b_h:
        return False

    if a_h:
        bx = b1.x
        ay = a1.y
        ax_min, ax_max = (a1.x, a2.x) if a1.x < a2.x else (a2.x, a1.x)
        by_min, by_max = (b1.y, b2.y) if b1.y < b2.y else (b2.y, b1.y)
        return ax_min < bx < ax_max and by_min < ay < by_max
    else:
        ax = a1.x
        by = b1.y
        bx_min, bx_max = (b1.x, b2.x) if b1.x < b2.x else (b2.x, b1.x)
        ay_min, ay_max = (a1.y, a2.y) if a1.y < a2.y else (a2.y, a1.y)
        return bx_min < ax < bx_max and ay_min < by < ay_max


def split_path_by_side(path: list[PathStep]) -> list[tuple[list[Point], str]]:
    """Split a multi-layer path into contiguous same-side segments.

    Returns a list of (points, side) pairs.  Consecutive points that share a
    side are grouped; a via (same position, different side) marks a boundary.

    Example:
        [(A, "top"), (B, "top"), (B, "bottom"), (C, "bottom")]
        → [([A, B], "top"), ([B, C], "bottom")]
    """
    if not path:
        return []

    segments: list[tuple[list[Point], str]] = []
    cur_pts: list[Point] = [path[0][0]]
    cur_side: str = path[0][1]

    for pt, side in path[1:]:
        if side == cur_side:
            cur_pts.append(pt)
        else:
            if cur_pts[-1] != pt:
                cur_pts.append(pt)
            segments.append((cur_pts, cur_side))
            cur_pts = [pt]
            cur_side = side

    segments.append((cur_pts, cur_side))
    return segments


__all__ = ["RoutingResult", "PathStep", "find_paths", "split_path_by_side"]
