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


@dataclass
class _SharedState:
    """Shared state across all universe workers for one A* search."""

    result_queue: Queue[RoutingResult]
    # Node count when the first universe found the target; None until then.
    first_found_nodes: int | None = None
    # Extra budget ratio: remaining universes may expand this many more nodes
    # after first_found_nodes is set (0.5 = N/2 extra nodes).
    extra_budget_ratio: float = 0.5
    # Lock protecting first_found_nodes and futures list.
    _lock: threading.Lock = field(default_factory=threading.Lock)
    # All submitted futures — needed to wait for completion.
    futures: list[Future[None]] = field(default_factory=list)

    def record_found(self, nodes_expanded: int) -> None:
        with self._lock:
            if self.first_found_nodes is None:
                self.first_found_nodes = nodes_expanded

    def over_budget(self, nodes_expanded: int) -> bool:
        ffn = self.first_found_nodes  # read without lock (int assignment is atomic in CPython)
        if ffn is None:
            return False
        budget = ffn + int(ffn * self.extra_budget_ratio)
        return nodes_expanded > budget

    def add_future(self, fut: Future[None]) -> None:
        with self._lock:
            self.futures.append(fut)


def _universe_worker(
    heap: list[tuple[float, float, int, float, float, int, float, float, list[PathStep]]],
    visited: dict[tuple[float, float, int, float, float], float],
    nodes_expanded: int,
    seq_counter: int,
    shared: _SharedState,
    # Read-only routing context (shared across all universes):
    valid_nodes: dict[str, set[Point]],
    target_set: set[Point],
    cost_model: RoutingCostModel,
    insulated: bool,
    occupied: dict[tuple[Point, str], uuid.UUID],
    same_net: dict[tuple[Point, str], uuid.UUID],
    near_pins: set[tuple[Point, str]],
    near_pin_cost: float,
    bare_segs: set[tuple[Point, Point, str]],
    vias: set[Point],
    forbidden_edges: set[tuple[Point, str, Point, str]],
    penalised: dict[tuple[Point, str], float],
) -> None:
    """Run one A* universe until it finds a path, exhausts the heap, or exceeds budget."""

    def h(p: Point) -> float:
        return min(math.sqrt((p.x - t.x)**2 + (p.y - t.y)**2) for t in target_set)

    _seq = seq_counter

    while heap:
        if shared.over_budget(nodes_expanded):
            return

        _f, g, _s, cx, cy, cs_idx, dx, dy, path = heapq.heappop(heap)
        pos = Point(cx, cy)
        cur_side = _SIDES[cs_idx]
        state_key = (cx, cy, cs_idx, dx, dy)

        if visited.get(state_key, float("inf")) <= g:
            continue
        visited[state_key] = g
        nodes_expanded += 1

        if pos in target_set:
            shared.record_found(nodes_expanded)
            shared.result_queue.put(RoutingResult(path=path, cost=g, insulated=insulated))
            return

        # Build the list of valid neighbour heap entries before deciding
        # whether to branch — we need the count to decide.
        neighbor_entries: list[tuple[float, float, int, float, float, int, float, float, list[PathStep]]] = []

        # --- Same-layer moves ---
        for ndx, ndy, dist_mult in _NEIGHBOURS:
            npos = Point(cx + ndx, cy + ndy)
            if npos not in valid_nodes[cur_side]:
                continue
            if (pos, cur_side, npos, cur_side) in forbidden_edges:
                continue

            step_cost = _step_cost(
                npos, cur_side, dist_mult, cost_model, occupied, same_net,
                near_pins, near_pin_cost, penalised,
            )
            if step_cost >= float("inf"):
                continue

            crossing_cost = _crossing_cost(pos, npos, cur_side, bare_segs, cost_model, insulated)
            if crossing_cost >= float("inf"):
                continue
            step_cost += crossing_cost

            if (dx != 0.0 or dy != 0.0) and (ndx != dx or ndy != dy):
                step_cost += cost_model.direction_change

            ng = g + step_cost
            nstate = (npos.x, npos.y, cs_idx, ndx, ndy)
            if nstate in visited and visited[nstate] <= ng:
                continue

            nf = ng + h(npos)
            _seq += 1
            neighbor_entries.append(
                (nf, ng, _seq, npos.x, npos.y, cs_idx, ndx, ndy, path + [(npos, cur_side)])
            )

        # --- Layer change at plated-through pad ---
        via_entry: tuple[float, float, int, float, float, int, float, float, list[PathStep]] | None = None
        if pos in vias:
            other_side = _OTHER_SIDE[cur_side]
            other_idx = 1 - cs_idx
            if pos in valid_nodes[other_side]:
                if (pos, cur_side, pos, other_side) not in forbidden_edges:
                    ng = g + cost_model.layer_change
                    nstate = (pos.x, pos.y, other_idx, 0.0, 0.0)
                    if not (nstate in visited and visited[nstate] <= ng):
                        nf = ng + h(pos)
                        _seq += 1
                        via_entry = (nf, ng, _seq, pos.x, pos.y, other_idx, 0.0, 0.0, path + [(pos, other_side)])

        if via_entry is not None:
            neighbor_entries.append(via_entry)

        if not neighbor_entries:
            continue

        # Decide whether to branch or continue in-place.
        # Branch only when: ≥2 neighbours AND this search's own universes
        # are not already flooding the pool.  We count only futures belonging
        # to THIS search (shared.futures) so that parallel nets on the same
        # pool don't block each other from ever branching.
        pool_queue_size = _UNIVERSE_POOL._work_queue.qsize()  # type: ignore[attr-defined]
        with shared._lock:
            active_futures = sum(1 for f in shared.futures if not f.done())
        # Allow branching when this search has fewer live universes than CPU count
        # AND the pool's pending queue isn't flooded (avoid unbounded explosion).
        should_branch = (
            len(neighbor_entries) >= 2
            and active_futures < _CPU_COUNT
            and pool_queue_size < _CPU_COUNT * 4
        )

        if should_branch:
            _log.info(
                "astar.universe.fork",
                thread_id=threading.get_ident(),
                branches=len(neighbor_entries),
                pool_queue_size=pool_queue_size,
                active_futures=active_futures,
                cpu_count=_CPU_COUNT,
                nodes_expanded=nodes_expanded,
            )
            # Copy-on-write: each neighbour gets its own heap+visited snapshot.
            for entry in neighbor_entries:
                new_heap = list(heap)
                heapq.heappush(new_heap, entry)
                new_visited = dict(visited)
                fut = _UNIVERSE_POOL.submit(
                    _universe_worker,
                    new_heap, new_visited, nodes_expanded, _seq, shared,
                    valid_nodes, target_set, cost_model, insulated,
                    occupied, same_net, near_pins, near_pin_cost,
                    bare_segs, vias, forbidden_edges, penalised,
                )
                shared.add_future(fut)
            _log.info(
                "astar.universe.forked",
                thread_id=threading.get_ident(),
                pool_queue_after=_UNIVERSE_POOL._work_queue.qsize(),  # type: ignore[attr-defined]
                total_futures=len(shared.futures),
            )
            # This universe has delegated all branches — it is done.
            return
        else:
            # Log why we didn't branch (only first few times to avoid flood).
            if nodes_expanded <= 3:
                _log.info(
                    "astar.universe.no_fork",
                    thread_id=threading.get_ident(),
                    neighbors=len(neighbor_entries),
                    pool_queue_size=pool_queue_size,
                    active_futures=active_futures,
                    cpu_count=_CPU_COUNT,
                    reason="single_neighbor" if len(neighbor_entries) < 2 else "pool_full",
                )
            # No branching — push all neighbours onto our own heap (mutable, zero copy).
            for entry in neighbor_entries:
                heapq.heappush(heap, entry)


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
    bare_segs: set[tuple[Point, Point, str]],
    vias: set[Point],
    forbidden_edges: set[tuple[Point, str, Point, str]],
    penalised: dict[tuple[Point, str], float],
    extra_budget_ratio: float = 0.5,
) -> RoutingResult | None:
    """Parallel-universe A* — may spawn sub-searches on the thread pool.

    Returns the cheapest path found across all universes, or None if unreachable.
    """
    def h(p: Point) -> float:
        return min(math.sqrt((p.x - t.x)**2 + (p.y - t.y)**2) for t in target_set)

    side_idx = 0 if source_side == "top" else 1
    initial_heap: list[tuple[float, float, int, float, float, int, float, float, list[PathStep]]] = [
        (h(source), 0.0, 0, source.x, source.y, side_idx, 0.0, 0.0, [(source, source_side)])
    ]
    initial_visited: dict[tuple[float, float, int, float, float], float] = {}

    shared = _SharedState(
        result_queue=Queue(),
        extra_budget_ratio=extra_budget_ratio,
    )

    pool_q = _UNIVERSE_POOL._work_queue.qsize()  # type: ignore[attr-defined]
    _log.info(
        "astar.parallel.start",
        source=f"({source.x},{source.y})",
        source_side=source_side,
        targets=len(target_set),
        cpu_count=_CPU_COUNT,
        pool_queue_size=pool_q,
        thread_id=threading.get_ident(),
    )

    # Submit the root universe.
    fut = _UNIVERSE_POOL.submit(
        _universe_worker,
        initial_heap, initial_visited, 0, 0, shared,
        valid_nodes, target_set, cost_model, insulated,
        occupied, same_net, near_pins, near_pin_cost,
        bare_segs, vias, forbidden_edges, penalised,
    )
    shared.add_future(fut)
    _log.info("astar.parallel.root_submitted", pool_queue_after=_UNIVERSE_POOL._work_queue.qsize())  # type: ignore[attr-defined]

    # Wait for all futures to complete (they may spawn more futures dynamically).
    # We poll in a loop because futures list grows as universes branch.
    # Timeout per poll cycle: 5s total max (A* on a 30×20 board never takes that long).
    _MAX_WAIT_S = 5.0
    _POLL_INTERVAL_S = 0.05
    import time
    deadline = time.monotonic() + _MAX_WAIT_S
    while time.monotonic() < deadline:
        with shared._lock:
            pending = [f for f in shared.futures if not f.done()]
        if not pending:
            break
        # Wait for whatever is currently pending, with short timeout so we
        # can re-check for newly added futures.
        from concurrent.futures import wait as _wait, ALL_COMPLETED
        remaining = deadline - time.monotonic()
        _wait(pending, timeout=min(_POLL_INTERVAL_S, remaining), return_when=ALL_COMPLETED)

    # Drain all results from the queue.
    results: list[RoutingResult] = []
    while True:
        try:
            results.append(shared.result_queue.get_nowait())
        except Empty:
            break

    if not results:
        return None
    return min(results, key=lambda r: r.cost)


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

    # Build valid-node sets per side from the ConnectivityGraph.
    valid_nodes: dict[str, set[Point]] = {"top": set(), "bottom": set()}
    for node in graph.all_node_keys():
        nx, ny, ns = node
        if ns in valid_nodes:
            valid_nodes[ns].add(Point(nx, ny))

    if source not in valid_nodes[side]:
        return []

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
            bare_segs=bare_segs,
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
    bare_segs: set[tuple[Point, Point, str]],
    vias: set[Point],
    forbidden_edges: set[tuple[Point, str, Point, str]],
    penalised: dict[tuple[Point, str], float],
) -> RoutingResult | None:
    """Sequential A* — kept for reference and fallback use."""
    def h(p: Point) -> float:
        return min(math.sqrt((p.x - t.x)**2 + (p.y - t.y)**2) for t in target_set)

    _seq = 0
    side_idx = 0 if source_side == "top" else 1
    heap: list[tuple[float, float, int, float, float, int, float, float, list[PathStep]]] = [
        (h(source), 0.0, _seq, source.x, source.y, side_idx, 0.0, 0.0, [(source, source_side)])
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
                pos, npos, cur_side, bare_segs, cost_model, insulated
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

            nf = ng + h(npos)
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
                        nf = ng + h(pos)
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
    bare_segs: set[tuple[Point, Point, str]],
    cost_model: RoutingCostModel,
    insulated: bool,
) -> float:
    """Return the extra cost for the step p1→p2 due to bare-wire crossings.

    Only segments on the same layer can cross each other.
    For orthogonal steps, check if this segment crosses any bare segment.
    For diagonal steps, check both half-steps.
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
        mid1 = Point(p1.x + dx, p1.y)
        mid2 = Point(p1.x,      p1.y + dy)
        crosses = (
            _seg_crosses_any(p1, mid1, side, bare_segs) or
            _seg_crosses_any(mid1, p2, side, bare_segs) or
            _seg_crosses_any(p1, mid2, side, bare_segs) or
            _seg_crosses_any(mid2, p2, side, bare_segs)
        )
    else:
        crosses = _seg_crosses_any(p1, p2, side, bare_segs)

    if not crosses:
        return 0.0
    return crossing_cost


def _seg_crosses_any(
    p1: Point, p2: Point, side: str, bare_segs: set[tuple[Point, Point, str]]
) -> bool:
    """Return True if axis-aligned segment p1→p2 strictly crosses any bare segment on the same layer."""
    for bsrc, bdst, bside in bare_segs:
        if bside != side:
            continue
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
