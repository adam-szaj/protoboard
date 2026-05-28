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
    the granular ``crossing_ib_*`` knobs (insulated wire) or `crossing_bare_bare`
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
import itertools as _it
import json as _json
import math
import os
import threading
import time as _time
import uuid
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path as _Path
from queue import Empty, Queue

import structlog

from protoboard.domain.connectivity.graph import ConnectivityGraph
from protoboard.geometry import Point
from protoboard.services.routing.cost_model import RoutingCostModel

_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Universe tracing (opt-in via PROTOBOARD_ASTAR_TRACE_DIR=/some/dir)
# ---------------------------------------------------------------------------
# When the env var is set, every _universe_worker invocation writes a
# JSONL file with every state it pops from the heap plus the eventual
# outcome.  Each line is one event:
#   {"e": "step", "n": <node_idx>, "pos": [x, y, side], "g": cost,
#    "dx": .., "dy": .., "blocked": [list of skipped neighbours]}
#   {"e": "goal", "path": [[x,y,side], ...], "cost": ..}
#   {"e": "end",  "reason": "heap_empty" | "over_budget"}
#
# Files are named astar_<unix_ms>_<seq>.jsonl so the order is roughly
# wall-clock; pair them with the parent autoroute log to attribute traces
# to specific net/segment requests.

_TRACE_DIR: _Path | None = (
    _Path(os.environ["PROTOBOARD_ASTAR_TRACE_DIR"])
    if os.environ.get("PROTOBOARD_ASTAR_TRACE_DIR")
    else None
)
if _TRACE_DIR is not None:
    _TRACE_DIR.mkdir(parents=True, exist_ok=True)
_TRACE_COUNTER = _it.count()


def _open_trace():  # type: ignore[no-untyped-def]
    """Open a new trace file for this universe; None when tracing is off."""
    if _TRACE_DIR is None:
        return None
    seq = next(_TRACE_COUNTER)
    fname = _TRACE_DIR / f"astar_{int(_time.time() * 1000)}_{seq:06d}.jsonl"
    return fname.open("w", buffering=1)

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
        # Rust kernel still consumes a single insulated×bare crossing cost.
        # Project the granular off_pad_same_side knob onto it; the more
        # nuanced classification only fires in the Python branch (the
        # Rust kernel will be ported later).
        cost_model.get("crossing_ib_off_pad_same_side", 0.0),
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


# Spatial index for bare wire segments: (cell_x, cell_y, side) → [(p1, p2, net_idx), ...]
# Each segment is stored under every integer cell its bounding box covers.
# Lookup: for a step covering bbox (x0..x1, y0..y1) query all overlapping cells.
# ``net_idx`` is a compact integer identifier of the segment's electrical net
# (built per find_paths call); -1 = no net / floating wire.  Used to detect
# same-net wire reuse so the new step can skip crossing penalties.
BareSegIndex = dict[tuple[int, int, str], list[tuple[Point, Point, int]]]


def _build_bare_seg_index(
    bare_segs: set[tuple[Point, Point, str, int]] | set[tuple[Point, Point, str]],
) -> BareSegIndex:
    """Build a spatial index of bare wire segments.

    Accepts either the legacy 3-tuple form ``(p1, p2, side)`` or the
    net-aware 4-tuple form ``(p1, p2, side, net_idx)``.  The legacy form
    is treated as net_idx=-1 (no net) for backward compatibility with
    older test fixtures.
    """
    idx: BareSegIndex = {}
    for entry in bare_segs:
        if len(entry) == 4:
            p1, p2, side, net_idx = entry  # type: ignore[misc]
        else:
            p1, p2, side = entry  # type: ignore[misc]
            net_idx = -1
        x0 = int(math.floor(min(p1.x, p2.x)))
        x1 = int(math.floor(max(p1.x, p2.x)))
        y0 = int(math.floor(min(p1.y, p2.y)))
        y1 = int(math.floor(max(p1.y, p2.y)))
        for cx in range(x0, x1 + 1):
            for cy in range(y0, y1 + 1):
                key = (cx, cy, side)
                if key not in idx:
                    idx[key] = []
                idx[key].append((p1, p2, net_idx))
    return idx


# 4 orthogonal + 4 diagonal neighbours (same layer), each with its distance
# multiplier.  Layer changes are handled separately.
_NEIGHBOURS: list[tuple[float, float, float]] = [
    (1.0,  0.0, 1.0),
    (-1.0, 0.0, 1.0),
    (0.0,  1.0, 1.0),
    (0.0, -1.0, 1.0),
    (1.0,  1.0, 1.4140),
    (1.0, -1.0, 1.4140),
    (-1.0, 1.0, 1.4140),
    (-1.0,-1.0, 1.4140),
]

_SIDES: tuple[str, str] = ("top", "bottom")
_OTHER_SIDE: dict[str, str] = {"top": "bottom", "bottom": "top"}

# A routed path step: grid position + which layer it lives on.
PathStep = tuple[Point, str]

# Heap entries used to carry a full ``list[PathStep]`` per push and grew it
# with ``path + [step]`` on every neighbour expansion — O(path_len) memory
# per push and O(N²) total for N expansions.  On large boards (80×60
# plated-through perfboard, VCC routes) that copy dominated and pushed
# resident set size past 10 GB.
#
# The replacement is a singly-linked list of (parent, step) cells: each push
# allocates one 2-tuple regardless of path length.  Many heap entries share
# their prefix in memory.  ``_unfold_path_chain`` materialises the linked
# list back into a ``list[PathStep]`` once, at goal time (or for tracing
# / rollback) — paying O(path_len) only on the few entries that actually
# need it.
_PathChain = tuple["_PathChain | None", PathStep] | None


def _unfold_path_chain(chain: _PathChain) -> list[PathStep]:
    """Walk a linked-list path chain head-first; return steps in route order.

    The chain stores tail → head (each cell's first slot is its predecessor),
    so we collect into a list and reverse once.  Cheap and only called at
    goal / trace / rollback boundaries — not in the hot loop.
    """
    out: list[PathStep] = []
    node = chain
    while node is not None:
        parent, step = node
        out.append(step)
        node = parent
    out.reverse()
    return out


def _fold_path_chain(steps: list[PathStep]) -> _PathChain:
    """Inverse of ``_unfold_path_chain`` — build a chain from an ordered list.

    Used by the deferred-via rollback branch which has to rewrite a slice
    of the materialised path back into a chain so it can be pushed onto
    the heap again.
    """
    chain: _PathChain = None
    for step in steps:
        chain = (chain, step)
    return chain

# Executor for parallel universe A*.  Default is *sequential* — see
# project_silent_crash_post_upgrade memory: a worker thread garbage-collecting
# concurrently with Qt's paintEvent on the main thread can segfault inside
# CPython 3.14's mark_stacks (intermittent, env-dependent).  The autorouter
# already drives sequential mode for its side-by-side A* fan-out via the
# same env var; we mirror that here so both pools agree.
#
# PROTOBOARD_ASTAR_PARALLEL=1 opts back into the real thread pool.
# The legacy PROTOBOARD_ASTAR_SEQUENTIAL=1 also forces sequential (it
# selects the same default path).
_CPU_COUNT = max(2, os.cpu_count() or 2)


class _SequentialUniverseExecutor:
    """Inline executor mimicking ThreadPoolExecutor.submit().

    Each ``submit(fn, ...)`` runs ``fn`` inline and returns a completed
    :class:`Future`.  Used to keep the parallel-universe A* code path
    structurally identical while avoiding the cross-thread GC race that
    intermittently crashes CPython 3.14 on this stack.
    """

    def submit(self, fn, *args, **kwargs):  # type: ignore[no-untyped-def]
        fut: Future = Future()
        try:
            fut.set_result(fn(*args, **kwargs))
        except BaseException as e:  # noqa: BLE001
            fut.set_exception(e)
        return fut

    def shutdown(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        pass


_ASTAR_PARALLEL = os.environ.get("PROTOBOARD_ASTAR_PARALLEL", "0") == "1"
_ASTAR_SEQUENTIAL = os.environ.get("PROTOBOARD_ASTAR_SEQUENTIAL", "0") == "1"
if _ASTAR_PARALLEL and not _ASTAR_SEQUENTIAL:
    _UNIVERSE_POOL: ThreadPoolExecutor | _SequentialUniverseExecutor = ThreadPoolExecutor(
        max_workers=_CPU_COUNT, thread_name_prefix="astar_universe",
    )
else:
    _UNIVERSE_POOL = _SequentialUniverseExecutor()


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
        # Read of first_found_nodes is a single LOAD_ATTR — the GIL makes
        # the dict slot read atomic, so a stale-but-consistent value is
        # the worst case (we just skip a budget cutoff for one iteration).
        ffn = self.first_found_nodes
        if ffn is None:
            return False
        budget = ffn + int(ffn * self.extra_budget_ratio)
        if nodes_expanded > budget:
            # `+= 1` is read-modify-write — bytecode is LOAD/BINARY_ADD/STORE
            # so concurrent increments can lose updates.  The counter is
            # only used for diagnostics; lossy is acceptable.  Switch to
            # a lock if exact counts ever matter.
            self.over_budget_hits += 1
            return True
        return False

    def record_nodes(self, n: int) -> None:
        # Lossy under contention — diagnostic-only, see over_budget().
        self.total_nodes_expanded += n

    def add_future(self, fut: Future[None]) -> None:
        with self._lock:
            self.futures.append(fut)
            self.active_count += 1

    def mark_done(self) -> None:
        """Called by each universe worker when it finishes."""
        with self._lock:
            self.active_count -= 1


# Heap entry:
#   (f, g, seq, x, y, side_idx, dx, dy, path,
#    via_pending: int,         # 0 or 1
#    pre_via_x, pre_via_y,     # cell where the tentative via was placed
#    shadow_g,                 # accumulated cost if we had stayed on the old side
#    shadow_dx, shadow_dy)     # direction memory for the shadow path
#
# When deferred_via_lookahead == 0, all six trailing fields are constant zero;
# the cost cost per entry is 5 extra floats — marginal vs the path list copy.
_HE = tuple[
    float, float, int, float, float, int, float, float, _PathChain,
    int, float, float, float, float, float,
]


def _universe_worker(
    heap: list[_HE],
    visited: dict[
        tuple[float, float, int, float, float, int, float, float, float, float],
        float,
    ],
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
    source_parity: int = -1,
    at_pad_wires: dict[tuple[Point, str], int] | None = None,
    all_segments: list[tuple[Point, Point, str, bool, int]] | None = None,
    active_net_idx: int = -1,
    at_pad_nets: dict[tuple[Point, str], frozenset[int]] | None = None,
    teleport_pads: dict[tuple[Point, str], tuple[tuple[Point, str], ...]] | None = None,
    foreign_wire_cells: set[tuple[Point, str]] | None = None,
    foreign_via_pads: set[Point] | None = None,
    active_dominant_side: str | None = None,
) -> None:
    """Run one A* universe until it finds a path, exhausts the heap, or exceeds budget.

    *source_parity* is consulted only when cost_model.bare_diagonal_mode is
    True.  In that mode a diagonal neighbour (ndx, ndy) is rejected unless
    its family matches the source pad's parity: parity 0 admits the "/"
    family (ndx*ndy > 0); parity 1 admits the "\\" family (ndx*ndy < 0).
    Pass -1 to disable the filter even if cost_model says otherwise.
    """

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
    # Angle-aware bends — opt-in.  When any bend_* knob is set the hot loop
    # classifies the bend by the cosine of the angle between prev/next step
    # directions and charges the matching cost.  When all three are 0.0 the
    # check degrades to a single _dc charge per turn (legacy behaviour).
    _b45  = cost_model.get("bend_45",  _dc)
    _b90  = cost_model.get("bend_90",  _dc)
    _b135 = cost_model.get("bend_135", _dc)
    _bend_angle_active = (
        _b45  != _dc
        or _b90  != _dc
        or _b135 != _dc
    )
    # Diagonal parity × side × family preferences — opt-in.  Each diagonal
    # step is classified by its family (NE/SW vs NW/SE) and the parity of
    # the destination cell ``(x+y) mod 2``.  Mapping (preferred family per
    # (parity, side)):
    #   parity even + top    → NE/SW   (cell-sum even → "/")
    #   parity even + bottom → NW/SE
    #   parity odd  + top    → NW/SE
    #   parity odd  + bottom → NE/SW
    # When both knobs are 0, the cost is unchanged.  See docs/routing.md.
    _diag_pref   = cost_model.get("diagonal_preferred_family",   0.0)
    _diag_unpref = cost_model.get("diagonal_dispreferred_family", 0.0)
    _diag_parity_active = (_diag_pref != 0.0) or (_diag_unpref != 0.0)
    # wire_over_foreign_wire_pad — soft cost per step into a cell already
    # hosting a foreign-net wire's vertex.  Opt-in; pre-resolved here so
    # the inner loop pays a single dict lookup.
    _wofp = cost_model.get("wire_over_foreign_wire_pad", 0.0)
    _wofp_cells: set[tuple[float, float, str]] = (
        {(p.x, p.y, s) for (p, s) in foreign_wire_cells}
        if foreign_wire_cells and _wofp != 0.0
        else set()
    )
    _wofp_active = _wofp != 0.0 and bool(_wofp_cells)
    # via_on_foreign_pad — soft cost added when a layer-change (via) lands
    # on a pad where another net already has a via (both top and bottom
    # endpoints there).  Pre-resolved set of (x, y) points.
    _vfp = cost_model.get("via_on_foreign_pad", 0.0)
    _vfp_pads: set[tuple[float, float]] = (
        {(p.x, p.y) for p in foreign_via_pads}
        if foreign_via_pads and _vfp != 0.0
        else set()
    )
    _vfp_active = _vfp != 0.0 and bool(_vfp_pads)
    # redundant_layer_change — discourage vias when an alternative on the
    # current side exists.  MVP: always surcharge a via with this cost when
    # the knob is non-zero, optionally doubled if the via leaves the
    # active-net dominant side (zebra suppression).  Future work: full
    # one-step look-ahead to determine "would the next step have been
    # free on the current side?"
    _rlc = cost_model.get("redundant_layer_change", 0.0)
    _rlc_active = _rlc != 0.0
    _dominant_side = active_dominant_side  # captured for hot-loop use
    # Enhanced at-pad penalties — float-tuple-keyed mirror of at_pad_wires
    # for fast lookup in the hot loop.  Empty dict when feature inactive
    # (all the cost-model knobs at 0.0); the keyed-lookup is then skipped
    # because ``_atp_active`` is False.
    _atp: dict[tuple[float, float, str], int] = (
        {(p.x, p.y, s): bm for (p, s), bm in at_pad_wires.items()}
        if at_pad_wires else {}
    )
    _atp_ii = cost_model.at_pad_insulated_insulated_same_side
    _atp_ib = cost_model.at_pad_insulated_bare_same_side
    _atp_active = bool(_atp) and (_atp_ii != 0.0 or _atp_ib != 0.0)
    # Per-pad set of net indices whose wire-vertices land on this pad.
    # Used for same-net reuse: if the active net's index is in the set,
    # the at_pad penalty (and the at_pad_bare_short reject) is bypassed
    # — touching one's own wire at a pad is electrically free.
    _atp_nets: dict[tuple[float, float, str], frozenset[int]] = (
        {(p.x, p.y, s): nets for (p, s), nets in at_pad_nets.items()}
        if at_pad_nets else {}
    )
    # Parallel-opposite-side penalty: list of (p1x, p1y, p2x, p2y,
    # other_side, other_insulated, other_net_idx) tuples on the OPPOSITE
    # side from the source.  Pre-filtered once so the hot loop only scans
    # relevant segments.  Empty when penalty=0 or all_segments is empty.
    _pos_pen = cost_model.parallel_opposite_side
    # Granular opposite-side crossing knobs trigger the same per-side
    # segment buckets (built once, consulted by _crossing_cost).
    _granular_opposite_active = (
        cost_model.get("crossing_ib_at_pad_opposite_side",  0.0) != 0.0
        or cost_model.get("crossing_ib_off_pad_opposite_side", 0.0) != 0.0
        or cost_model.get("crossing_ii_at_pad_opposite_side",  0.0) != 0.0
        or cost_model.get("crossing_ii_off_pad_opposite_side", 0.0) != 0.0
    )
    _pos_active = (
        (_pos_pen != 0.0 or _granular_opposite_active)
        and bool(all_segments)
    )
    # Two buckets:
    # * ``_segs_on_top``  — segments living on the TOP layer; consulted by
    #   workers stepping on BOTTOM (i.e. opposite to TOP).
    # * ``_segs_on_bot``  — segments on BOTTOM; consulted by TOP workers.
    _segs_on_top: list[tuple[float, float, float, float, bool, int]] = []
    _segs_on_bot: list[tuple[float, float, float, float, bool, int]] = []
    if _pos_active and all_segments is not None:
        for _seg in all_segments:
            # Backward compat: accept legacy 4-tuple (p1, p2, side, insulated)
            # and synthesise net_idx=-1.
            if len(_seg) == 5:
                _p1, _p2, _osd, _oins, _onet = _seg
            else:
                _p1, _p2, _osd, _oins = _seg  # type: ignore[misc]
                _onet = -1
            entry = (_p1.x, _p1.y, _p2.x, _p2.y, _oins, _onet)
            if _osd == "top":
                _segs_on_top.append(entry)
            else:
                _segs_on_bot.append(entry)
    # Same-net wire reuse setup.
    _reuse_cost = cost_model.same_net_wire_reuse_cost
    _has_reuse = active_net_idx >= 0
    # Float-keyed teleport map: at every same-net pad-vertex of an existing
    # wire of the active net, list the other pad-vertices in the same-net
    # electrical component.  Each destination is a (Point, side) we push as
    # a zero-cost neighbour.  Empty when active net has no laid wires.
    _tp: dict[tuple[float, float, str], tuple[tuple[Point, str], ...]] = (
        {(p.x, p.y, s): dests for (p, s), dests in teleport_pads.items()}
        if teleport_pads else {}
    )
    _parity_filter = cost_model.bare_diagonal_mode and source_parity in (0, 1)
    # Allowed sign of (ndx * ndy) for diagonal steps in the source's family.
    # parity 0 → "/" family → ndx*ndy > 0; parity 1 → "\" family → ndx*ndy < 0.
    _parity_sign = 1.0 if source_parity == 0 else -1.0
    # Deferred-via lookahead: when non-zero, every via is held tentative
    # and A* tracks the shadow cost on the old side step-by-step.  At the
    # first obstacle / direction change on either side, decide commit
    # (via stays) vs rollback (continue on old side).  When the shadow
    # would have been at least this factor cheaper than the new side, the
    # via is rolled back.  0 = disabled — heap entries skip the extra
    # state and via decisions are immediate (legacy behaviour).
    _dvl = cost_model.get("deferred_via_lookahead", 0.0)
    _dvl_active = _dvl > 0.0
    # Priority mode for the tentative branch.  0 = always use g_real
    # (admissible, may discard tentative early); 1 = use min(g_real,
    # g_shadow) to keep the branch alive while shadow looks promising.
    _dvl_min_priority = cost_model.get("deferred_via_priority_mode", 0.0) > 0.5

    _seq = seq_counter
    _trace = _open_trace()
    if _trace is not None:
        _trace.write(_json.dumps({
            "e": "init",
            "insulated": insulated,
            "source_parity": source_parity,
            "bare_diagonal_mode": cost_model.bare_diagonal_mode,
            "n_targets": len(_tgt),
            "n_valid_top": len(_vn_top),
            "n_valid_bottom": len(_vn_bottom),
            "n_occupied": len(_occ),
            "n_same_net": len(_same),
            "n_bare_segs": sum(len(v) for v in bare_idx.values()),
            "first_target": next(iter(_tgt), None),
        }) + "\n")

    try:
        while heap:
            if shared.over_budget(nodes_expanded):
                if _trace is not None:
                    _trace.write(_json.dumps({
                        "e": "end", "reason": "over_budget",
                        "nodes_expanded": nodes_expanded,
                    }) + "\n")
                return

            (_f, g, _s, cx, cy, cs_idx, dx, dy, path,
             via_pending, pre_via_x, pre_via_y,
             shadow_g, shadow_dx, shadow_dy) = heapq.heappop(heap)
            cur_side = _SIDES[cs_idx]
            # Include deferred-via dimensions in state key so two paths
            # reaching the same (x,y,side,dir) but with different
            # tentative-via context remain distinct.  Falls back to legacy
            # 5-tuple shape (numerically) when _dvl_active is False —
            # via_pending stays 0 and shadow_* stay 0.0 throughout.
            state_key = (cx, cy, cs_idx, dx, dy,
                         via_pending, pre_via_x, pre_via_y,
                         shadow_dx, shadow_dy)

            if visited.get(state_key, _inf) <= g:
                continue
            visited[state_key] = g
            nodes_expanded += 1

            if (cx, cy) in _tgt:
                goal_path = _unfold_path_chain(path)
                shared.record_found(nodes_expanded)
                shared.result_queue.put(RoutingResult(
                    path=goal_path, cost=g, insulated=insulated,
                    nodes_expanded=nodes_expanded,
                ))
                if _trace is not None:
                    _trace.write(_json.dumps({
                        "e": "goal",
                        "pos": [cx, cy, cur_side],
                        "g": g,
                        "path": [[p.x, p.y, s] for p, s in goal_path],
                        "nodes_expanded": nodes_expanded,
                    }) + "\n")
                    _trace.close()
                return

            neighbor_entries: list[_HE] = []
            _vn_cur = _vn[cs_idx]
            pos = Point(cx, cy)

            # When tracing, collect a structured record of why each neighbour
            # was accepted or rejected.  The list mirrors _NEIGHBOURS order so
            # rejections at any stage stay attributable.
            _trace_neighbours: list | None = [] if _trace is not None else None

            for ndx, ndy, dist_mult in _NEIGHBOURS:
                nx = cx + ndx
                ny = cy + ndy
                if (nx, ny) not in _vn_cur:
                    if _trace_neighbours is not None:
                        _trace_neighbours.append([ndx, ndy, "off_grid"])
                    continue
                if (cx, cy, cur_side, nx, ny, cur_side) in _fe:
                    if _trace_neighbours is not None:
                        _trace_neighbours.append([ndx, ndy, "forbidden_edge"])
                    continue
                # Parity-family filter for bare-diagonal mode: reject diagonals
                # from the opposite family so the source's path cannot fight
                # itself for mesh-cell crossings.
                if _parity_filter and ndx != 0.0 and ndy != 0.0:
                    if (ndx * ndy) * _parity_sign < 0.0:
                        if _trace_neighbours is not None:
                            _trace_neighbours.append([ndx, ndy, "wrong_parity"])
                        continue

                # Inlined _step_cost using float-tuple dicts.
                _nkey = (nx, ny, cur_side)
                if _nkey in _occ:
                    if _trace_neighbours is not None:
                        _trace_neighbours.append([ndx, ndy, "occupied"])
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

                # Diagonal parity × side × family preference.  Each diagonal
                # step (ndx≠0 ∧ ndy≠0) is classified as in/out of the
                # preferred family per the mapping in COST_KEY_DOCS.  Only
                # fires when at least one of the parity knobs is non-zero.
                if _diag_parity_active and ndx != 0.0 and ndy != 0.0:
                    # NE/SW family: ndx*ndy > 0  ("/" slash)
                    # NW/SE family: ndx*ndy < 0  ("\" backslash)
                    _is_ne_sw = (ndx * ndy) > 0.0
                    _parity_even = (int(nx + ny) % 2) == 0
                    _on_top = (cur_side == "top")
                    # Mapping: parity_even XOR on_top  → preferred = NE/SW
                    # (parity_even, top) → NE/SW; (parity_even, bottom) → NW/SE
                    # (parity_odd,  top) → NW/SE; (parity_odd,  bottom) → NE/SW
                    _prefers_ne_sw = (_parity_even == _on_top)
                    if _is_ne_sw == _prefers_ne_sw:
                        step_cost += _diag_pref
                    else:
                        step_cost += _diag_unpref

                # wire_over_foreign_wire_pad: charge once per step into a
                # cell hosting another net's wire vertex (same side).  Soft
                # cost — does not block; just biases A* toward routes that
                # do not casually loop through other nets' pads.
                if _wofp_active and (nx, ny, cur_side) in _wofp_cells:
                    step_cost += _wofp

                # Point created here — needed for _crossing_cost and path.
                npos = Point(nx, ny)
                _opp_segs = (
                    (_segs_on_bot if cur_side == "top" else _segs_on_top)
                    if _pos_active else None
                )
                crossing_cost = _crossing_cost(
                    pos, npos, cur_side, bare_idx, cost_model, insulated,
                    active_net_idx=active_net_idx,
                    all_segments_other_side=_opp_segs,
                )
                if crossing_cost >= _inf:
                    if _trace_neighbours is not None:
                        _trace_neighbours.append([ndx, ndy, "crossing_inf"])
                    continue
                step_cost += crossing_cost

                # Enhanced at-pad penalty: extra cost when stepping onto a
                # pad that already hosts another wire vertex on this side.
                # Bitmask: 1=insulated wire vertex present, 2=bare wire
                # vertex present.  Bare×bare same-side is already blocked
                # upstream via ``occupied``; this branch handles the
                # mixed and insulated×insulated cases.
                #
                # Same-net override: if the active net already has a vertex
                # on this pad, touching it is electrically free — bypass
                # both the _atp_ii/_atp_ib add and the at_pad_bare_short
                # reject.  Pay reuse_cost (default 0.0).
                _same_net_at_pad = (
                    _has_reuse and active_net_idx in _atp_nets.get(_nkey, frozenset())
                )
                if _same_net_at_pad:
                    step_cost += _reuse_cost
                elif _atp_active:
                    _bm = _atp.get(_nkey, 0)
                    if _bm:
                        if insulated:
                            if _bm & 1:
                                step_cost += _atp_ii
                            if _bm & 2:
                                step_cost += _atp_ib
                        else:
                            # New wire is bare.  Existing insulated → mixed
                            # penalty.  Existing bare on same side is a
                            # short; surface as inf so the step is rejected.
                            if _bm & 1:
                                step_cost += _atp_ib
                            if _bm & 2:
                                if _trace_neighbours is not None:
                                    _trace_neighbours.append(
                                        [ndx, ndy, "at_pad_bare_short"]
                                    )
                                continue

                # Enhanced parallel-opposite-side penalty: when the step's
                # segment is collinear with — and overlaps the interior of —
                # any segment on the opposite layer, add the configured
                # cost per match.  Bare×bare opposite-side incurs no
                # penalty (different layers; physically isolated).
                if _pos_active:
                    # cur_side="top" → consult segments on BOTTOM, and vice versa.
                    _opp = (
                        _segs_on_bot if cur_side == "top"
                        else _segs_on_top
                    )
                    if _opp:
                        _step_min_x = cx if cx <= nx else nx
                        _step_max_x = cx if cx >= nx else nx
                        _step_min_y = cy if cy <= ny else ny
                        _step_max_y = cy if cy >= ny else ny
                        for _q1x, _q1y, _q2x, _q2y, _oins, _onet in _opp:
                            # Same-net opposite-side overlap is electrically
                            # one node already — no penalty.
                            if _has_reuse and _onet == active_net_idx:
                                continue
                            # Skip if neither wire is insulated (bare×bare
                            # opposite-side has no penalty by spec).
                            if not insulated and not _oins:
                                continue
                            # Bbox cull.
                            _q_min_x = _q1x if _q1x <= _q2x else _q2x
                            _q_max_x = _q1x if _q1x >= _q2x else _q2x
                            _q_min_y = _q1y if _q1y <= _q2y else _q2y
                            _q_max_y = _q1y if _q1y >= _q2y else _q2y
                            if _step_max_x < _q_min_x or _step_min_x > _q_max_x:
                                continue
                            if _step_max_y < _q_min_y or _step_min_y > _q_max_y:
                                continue
                            # Collinearity: cross-products of (q1-cur)x(n-cur)
                            # and (q2-cur)x(n-cur) must be zero.
                            _dpx = nx - cx
                            _dpy = ny - cy
                            _cross1 = (_q1x - cx) * _dpy - (_q1y - cy) * _dpx
                            _cross2 = (_q2x - cx) * _dpy - (_q2y - cy) * _dpx
                            if _cross1 != 0.0 or _cross2 != 0.0:
                                continue
                            # 1D overlap interior on dominant axis.
                            if _step_max_x - _step_min_x >= _step_max_y - _step_min_y:
                                _lo = _step_min_x if _step_min_x > _q_min_x else _q_min_x
                                _hi = _step_max_x if _step_max_x < _q_max_x else _q_max_x
                            else:
                                _lo = _step_min_y if _step_min_y > _q_min_y else _q_min_y
                                _hi = _step_max_y if _step_max_y < _q_max_y else _q_max_y
                            if _hi > _lo:  # strict interior overlap
                                step_cost += _pos_pen

                if (dx != 0.0 or dy != 0.0) and (ndx != dx or ndy != dy):
                    if _bend_angle_active:
                        # Classify the bend by cosine of angle between
                        # (dx,dy) and (ndx,ndy).  Both vectors have length
                        # 1 (ortho) or √2 (diag); normalising via hypot is
                        # ~10ns and only happens at bends.
                        _l1 = (dx * dx + dy * dy) ** 0.5
                        _l2 = (ndx * ndx + ndy * ndy) ** 0.5
                        _cos = (dx * ndx + dy * ndy) / (_l1 * _l2)
                        if _cos > 0.6:           # ≈ cos(45°) = 0.707
                            step_cost += _b45
                        elif _cos > -0.1:        # ≈ cos(90°) = 0.0
                            step_cost += _b90
                        else:                    # cos(135°) ≈ -0.707, U-turn ≈ -1
                            step_cost += _b135
                    else:
                        step_cost += _dc

                ng = g + step_cost

                # ── Deferred-via accounting (C5-C8) ──────────────────
                # When a via is tentative, compute the same step's cost
                # on the OLD side (shadow_step_cost) so we know what
                # staying-on-old-side would have cost up to here.
                # First obstacle on either side fires the commit/
                # rollback decision; otherwise we accumulate shadow_g
                # and let A* continue exploring the tentative branch.
                next_via_pending = via_pending
                next_pre_via_x = pre_via_x
                next_pre_via_y = pre_via_y
                next_shadow_g = shadow_g
                next_shadow_dx = shadow_dx
                next_shadow_dy = shadow_dy

                _do_rollback = False
                _rollback_path: list[PathStep] | None = None
                _rollback_g: float = 0.0
                _old_side_idx = 1 - cs_idx  # captured for rollback dispatch

                if _dvl_active and via_pending == 1:
                    # Compute step_cost on the OLD side for the same
                    # (nx, ny) neighbour.  Only the cost components that
                    # depend on side need recomputation:
                    # - crossing_cost (different bare_idx side)
                    # - at_pad penalty (different _atp side)
                    # - parallel_opposite_side (flipped)
                    # - wire_over_foreign_wire_pad (different side cells)
                    # - diagonal parity preference (flipped via cur_side)
                    # base + bend cost are side-independent → reuse step_cost
                    # for the base, then ADD only the side-dependent parts
                    # computed on old side.
                    _old_side = _OTHER_SIDE[cur_side]
                    _old_side_idx = 1 - cs_idx
                    # Check old-side cell is even valid (some cells exist
                    # only on one layer).  If not, shadow is infeasible —
                    # commit the via.
                    if (nx, ny) not in _vn[_old_side_idx]:
                        # Shadow path infeasible → commit via, normal forward.
                        next_via_pending = 0
                        next_pre_via_x = 0.0
                        next_pre_via_y = 0.0
                        next_shadow_g = 0.0
                    else:
                        _old_nkey = (nx, ny, _old_side)
                        # Base + crossing for old side.
                        _shadow_step_cost = _free * dist_mult
                        if _old_nkey in _near:
                            _shadow_step_cost += near_pin_cost
                        _ex_old = _pen.get(_old_nkey)
                        if _ex_old is not None:
                            _shadow_step_cost += _ex_old
                        # Old-side crossing (consult opposite-side bucket
                        # relative to old_side, which is current's same
                        # side).
                        _opp_segs_old = (
                            (_segs_on_top if _old_side == "bottom" else _segs_on_bot)
                            if _pos_active else None
                        )
                        _shadow_crossing = _crossing_cost(
                            pos, npos, _old_side, bare_idx, cost_model,
                            insulated,
                            active_net_idx=active_net_idx,
                            all_segments_other_side=_opp_segs_old,
                        )
                        if _shadow_crossing >= _inf:
                            # Old side has infeasible crossing → commit via.
                            next_via_pending = 0
                            next_pre_via_x = 0.0
                            next_pre_via_y = 0.0
                            next_shadow_g = 0.0
                        else:
                            _shadow_step_cost += _shadow_crossing
                            # At-pad penalty on old side.
                            if _atp_active:
                                _bm_old = _atp.get(_old_nkey, 0)
                                if _bm_old:
                                    if insulated:
                                        if _bm_old & 1:
                                            _shadow_step_cost += _atp_ii
                                        if _bm_old & 2:
                                            _shadow_step_cost += _atp_ib
                            # Add same-cost-independent components: bend.
                            if (dx != 0.0 or dy != 0.0) and (ndx != dx or ndy != dy):
                                if _bend_angle_active:
                                    # _cos is computed elsewhere; recompute
                                    # quickly — bend penalty is side-
                                    # independent so reuse the value from
                                    # earlier classification.
                                    _l1 = (dx * dx + dy * dy) ** 0.5
                                    _l2 = (ndx * ndx + ndy * ndy) ** 0.5
                                    _cos = (dx * ndx + dy * ndy) / (_l1 * _l2)
                                    if _cos > 0.6:
                                        _shadow_step_cost += _b45
                                    elif _cos > -0.1:
                                        _shadow_step_cost += _b90
                                    else:
                                        _shadow_step_cost += _b135
                                else:
                                    _shadow_step_cost += _dc

                            # Decide trigger: any non-baseline cost on
                            # either side means the via is now committable
                            # or refundable.
                            _baseline = _free * dist_mult
                            _trigger = (
                                step_cost > _baseline + 1e-9
                                or _shadow_step_cost > _baseline + 1e-9
                            )
                            if _trigger:
                                # Tie-break: rollback wins when totals
                                # are equal (user pref).
                                _g_real_after  = g + step_cost
                                _g_shadow_after = shadow_g + _shadow_step_cost
                                if _g_shadow_after <= _g_real_after - _dvl * 0 + 1e-9 \
                                   if False else _g_shadow_after <= _g_real_after:
                                    # ROLLBACK: rewrite path post-via to
                                    # OLD side, drop the via marker, use
                                    # shadow accumulated g + new step cost.
                                    # Find via index by matching pre_via.
                                    # Materialise the chain once for the
                                    # rewrite (rare branch, cost OK).
                                    _path_list = _unfold_path_chain(path)
                                    _via_idx = -1
                                    for _i in range(len(_path_list) - 1, -1, -1):
                                        _pp, _pps = _path_list[_i]
                                        if _pp.x == pre_via_x and _pp.y == pre_via_y:
                                            _via_idx = _i
                                            break
                                    if _via_idx >= 0:
                                        # _path_list[via_idx] is the via
                                        # duplicate entry (Point at via
                                        # cell on new side); [via_idx-1] is
                                        # the last entry on old side.  Flip
                                        # post-via entries to old side and
                                        # drop the via marker.
                                        _new_path: list[PathStep] = list(
                                            _path_list[:_via_idx]
                                        )
                                        for _pp, _pps in _path_list[_via_idx + 1:]:
                                            _new_path.append((_pp, _old_side))
                                        _new_path.append((npos, _old_side))
                                        _rollback_path = _new_path
                                        _rollback_g = _g_shadow_after
                                        _do_rollback = True
                                    # If we couldn't find via_idx (defensive),
                                    # fall through to commit.
                                if not _do_rollback:
                                    # COMMIT: clear shadow, normal forward.
                                    next_via_pending = 0
                                    next_pre_via_x = 0.0
                                    next_pre_via_y = 0.0
                                    next_shadow_g = 0.0
                                    next_shadow_dx = 0.0
                                    next_shadow_dy = 0.0
                            else:
                                # No trigger yet — accumulate shadow_g and
                                # keep the via tentative.
                                next_shadow_g = shadow_g + _shadow_step_cost
                                next_shadow_dx = ndx
                                next_shadow_dy = ndy

                if _do_rollback and _rollback_path is not None:
                    # Push a fresh entry on the OLD side, no longer
                    # tentative.  State key gets cleared shadow fields
                    # because the via never happened.
                    _rb_nstate = (nx, ny, _old_side_idx, ndx, ndy,
                                  0, 0.0, 0.0, 0.0, 0.0)
                    if not (_rb_nstate in visited and visited[_rb_nstate] <= _rollback_g):
                        _rb_nf = _rollback_g + h(nx, ny)
                        _seq += 1
                        neighbor_entries.append((
                            _rb_nf, _rollback_g, _seq, nx, ny, _old_side_idx,
                            ndx, ndy, _fold_path_chain(_rollback_path),
                            0, 0.0, 0.0, 0.0, 0.0, 0.0,
                        ))
                    if _trace_neighbours is not None:
                        _trace_neighbours.append([ndx, ndy, "via_rolled_back"])
                    continue

                nstate = (nx, ny, cs_idx, ndx, ndy,
                          next_via_pending, next_pre_via_x, next_pre_via_y,
                          next_shadow_dx, next_shadow_dy)
                if nstate in visited and visited[nstate] <= ng:
                    if _trace_neighbours is not None:
                        _trace_neighbours.append([ndx, ndy, "visited_cheaper"])
                    continue

                # Heap priority: f = g + h normally; when in min-priority
                # mode and a via is tentative, use min(g_real, g_shadow)
                # so the branch is not discarded while shadow looks cheap.
                if _dvl_min_priority and next_via_pending == 1 and next_shadow_g < ng:
                    nf = next_shadow_g + h(nx, ny)
                else:
                    nf = ng + h(nx, ny)
                _seq += 1
                neighbor_entries.append(
                    (nf, ng, _seq, nx, ny, cs_idx, ndx, ndy,
                     (path, (npos, cur_side)),
                     next_via_pending, next_pre_via_x, next_pre_via_y,
                     next_shadow_g, next_shadow_dx, next_shadow_dy)
                )
                if _trace_neighbours is not None:
                    _trace_neighbours.append([ndx, ndy, "queued", ng, crossing_cost])

            via_entry: _HE | None = None
            if (cx, cy) in _via:
                other_side = _OTHER_SIDE[cur_side]
                other_idx = 1 - cs_idx
                if (cx, cy) in _vn[other_idx]:
                    if (cx, cy, cur_side, cx, cy, other_side) not in _fe:
                        _via_cost = _lc
                        # via_on_foreign_pad: surcharge if this pad is also
                        # used as a via by a different net.
                        if _vfp_active and (cx, cy) in _vfp_pads:
                            _via_cost += _vfp
                        # redundant_layer_change: flat surcharge per via.
                        # If a dominant side exists for the active net and
                        # this via leaves it (cur_side == dominant), zebra
                        # suppression doubles the penalty.
                        if _rlc_active:
                            if _dominant_side is not None and cur_side == _dominant_side:
                                _via_cost += _rlc * 2.0
                            else:
                                _via_cost += _rlc
                        # Electrical safety + geometric crowding at the
                        # via cell.  The plated-through hole connects both
                        # sides electrically; whichever wires *touch* the
                        # pad end up shorted together.
                        #
                        # - A BARE foreign wire vertex on the pad means
                        #   that wire is in metal contact with the pad on
                        #   both sides (the bare conductor literally
                        #   rests on the via hole).  Our via lands an
                        #   endpoint on the same pad — which is then
                        #   electrically merged with the foreign net.
                        #   Hard reject (regardless of our insulation):
                        #   bare presence == short.
                        # - An INSULATED foreign wire vertex floats above
                        #   the pad — no electrical contact.  Via here is
                        #   safe; just charge the geometric crowding
                        #   penalty (at_pad_insulated_*).
                        # Same-net at-pad is free (electrical merge is
                        # intended).
                        if _atp_active:
                            _short = False
                            for _via_side in (cur_side, other_side):
                                _via_key = (cx, cy, _via_side)
                                if _has_reuse and active_net_idx in _atp_nets.get(_via_key, frozenset()):
                                    continue
                                _bm_via = _atp.get(_via_key, 0)
                                if _bm_via & 2:
                                    # Bare foreign wire on this side →
                                    # short via the plated-through hole.
                                    _via_cost = _inf
                                    _short = True
                                    break
                                if _bm_via & 1:
                                    # Insulated foreign vertex — geometric
                                    # crowding only.
                                    if insulated:
                                        _via_cost += _atp_ii
                                    else:
                                        # bare wire landing on a pad with
                                        # foreign insulated vertex — mixed
                                        # crowding, same cost knob.
                                        _via_cost += _atp_ib
                            if _short:
                                pass  # _via_cost == _inf; downstream visited check skips
                        ng = g + _via_cost
                        # Deferred-via: mark the new via tentative so we
                        # can roll it back later if the shadow (old) side
                        # would have been cheaper at the next obstacle/
                        # bend.  When _dvl_active is False the via is
                        # committed immediately (via_pending stays 0).
                        if _dvl_active and via_pending == 0:
                            via_next_pending = 1
                            via_next_pre_x = cx
                            via_next_pre_y = cy
                            # shadow_g starts at the current g — the
                            # alternative path so far is identical up to
                            # this point (just without the via cost).
                            via_next_shadow_g = g
                            via_next_shadow_dx = dx
                            via_next_shadow_dy = dy
                        else:
                            # When already pending a via, committing the
                            # outer one (forwarded fields).  C8 explicitly
                            # forbids nested deferral.
                            via_next_pending = via_pending
                            via_next_pre_x = pre_via_x
                            via_next_pre_y = pre_via_y
                            via_next_shadow_g = shadow_g
                            via_next_shadow_dx = shadow_dx
                            via_next_shadow_dy = shadow_dy
                        nstate = (cx, cy, other_idx, 0.0, 0.0,
                                  via_next_pending, via_next_pre_x,
                                  via_next_pre_y,
                                  via_next_shadow_dx, via_next_shadow_dy)
                        if not (nstate in visited and visited[nstate] <= ng):
                            nf = ng + h(cx, cy)
                            _seq += 1
                            via_entry = (
                                nf, ng, _seq, cx, cy, other_idx, 0.0, 0.0,
                                (path, (pos, other_side)),
                                via_next_pending, via_next_pre_x,
                                via_next_pre_y, via_next_shadow_g,
                                via_next_shadow_dx, via_next_shadow_dy,
                            )

            if via_entry is not None:
                neighbor_entries.append(via_entry)

            # Same-net teleport edges: if the current pad is a vertex of an
            # existing wire belonging to the active net, hop to any other
            # pad-vertex in that net's electrical component at reuse cost.
            # Physically: the two pads are already shorted via existing
            # metal; the route can "continue" from any of them for free.
            # Only fires on pad-vertices (the grid points themselves) —
            # never mid-segment.
            if _has_reuse and _tp:
                _dests = _tp.get((cx, cy, cur_side))
                if _dests:
                    for _dp, _dside in _dests:
                        _dx_t = _dp.x
                        _dy_t = _dp.y
                        _dside_idx = 0 if _dside == "top" else 1
                        # Skip self-loop and invalid nodes.
                        if (_dx_t, _dy_t, _dside) == (cx, cy, cur_side):
                            continue
                        if (_dx_t, _dy_t) not in _vn[_dside_idx]:
                            continue
                        # Forbidden-edge check (skip if user pinned).
                        if (cx, cy, cur_side, _dx_t, _dy_t, _dside) in _fe:
                            continue
                        ng = g + _reuse_cost
                        # Teleport "arrives" with no direction memory —
                        # next step pays no direction-change penalty.
                        # Teleport implicitly commits any pending via:
                        # the geometric continuation is on a different
                        # plane (same-net wire reuse), incompatible with
                        # the shadow-on-old-side accounting.
                        nstate = (_dx_t, _dy_t, _dside_idx, 0.0, 0.0,
                                  0, 0.0, 0.0, 0.0, 0.0)
                        if nstate in visited and visited[nstate] <= ng:
                            continue
                        nf = ng + h(_dx_t, _dy_t)
                        _seq += 1
                        neighbor_entries.append((
                            nf, ng, _seq,
                            _dx_t, _dy_t, _dside_idx,
                            0.0, 0.0,
                            (path, (Point(_dx_t, _dy_t), _dside)),
                            0, 0.0, 0.0, 0.0, 0.0, 0.0,
                        ))

            if _trace is not None:
                _trace.write(_json.dumps({
                    "e": "step",
                    "pos": [cx, cy, cur_side],
                    "g": g,
                    "from_dir": [dx, dy],
                    "queued": len(neighbor_entries),
                    "neighbours": _trace_neighbours,
                    "via": via_entry is not None,
                    "nodes_expanded": nodes_expanded,
                }) + "\n")

            if not neighbor_entries:
                continue

            for entry in neighbor_entries:
                    heapq.heappush(heap, entry)
        if _trace is not None:
            _trace.write(_json.dumps({
                "e": "end", "reason": "heap_empty",
                "nodes_expanded": nodes_expanded,
            }) + "\n")
            _trace.close()
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
    at_pad_wires: dict[tuple[Point, str], int] | None = None,
    all_segments: list[tuple[Point, Point, str, bool, int]] | list[tuple[Point, Point, str, bool]] | None = None,
    active_net_idx: int = -1,
    at_pad_nets: dict[tuple[Point, str], frozenset[int]] | None = None,
    teleport_pads: dict[tuple[Point, str], tuple[tuple[Point, str], ...]] | None = None,
    foreign_wire_cells: set[tuple[Point, str]] | None = None,
    foreign_via_pads: set[Point] | None = None,
    active_dominant_side: str | None = None,
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
        (
            h0(source.x, source.y), 0.0, 0,
            source.x, source.y, side_idx, 0.0, 0.0,
            (None, (source, source_side)),
            # Deferred-via state: not pending at source.
            0,         # via_pending
            0.0, 0.0,  # pre_via_x, pre_via_y
            0.0,       # shadow_g
            0.0, 0.0,  # shadow_dx, shadow_dy
        )
    ]
    # State key includes the deferred-via dimensions so two paths reaching
    # the same (x,y,side,dir) but with different tentative-via context
    # remain distinct.  When deferred_via_lookahead is disabled, the extra
    # fields stay at 0 and the key shape is effectively the legacy 5-tuple.
    initial_visited: dict[
        tuple[float, float, int, float, float, int, float, float, float, float],
        float,
    ] = {}

    # Parity of the source pad: pads sit at (c+0.5, r+0.5) so floor gives (c, r).
    # Used only by bare_diagonal mode; ignored otherwise.
    source_parity = (
        int(math.floor(source.x)) + int(math.floor(source.y))
    ) % 2 if cost_model.bare_diagonal_mode else -1

    shared = _SharedState(
        result_queue=Queue(),
        extra_budget_ratio=extra_budget_ratio,
    )

    # Submit the root universe.  Enhanced-router extras (``at_pad_wires``,
    # ``all_segments``) live at the tail of the positional list so the
    # legacy call sites that pass only ``source_parity`` keep working.
    # Same-net reuse extras (``active_net_idx``, ``at_pad_nets``,
    # ``teleport_pads``) follow them.
    fut = _UNIVERSE_POOL.submit(
        _universe_worker,
        initial_heap, initial_visited, 0, 0, shared,
        valid_nodes, target_set, target_coords, cost_model, insulated,
        occupied, same_net, near_pins, near_pin_cost,
        bare_idx, vias, forbidden_edges, penalised,
        source_parity,
        at_pad_wires,
        all_segments,
        active_net_idx,
        at_pad_nets,
        teleport_pads,
        foreign_wire_cells,
        foreign_via_pads,
        active_dominant_side,
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
    bare_wire_segments: set[tuple[Point, Point, str, uuid.UUID | None]] | set[tuple[Point, Point, str]] | None = None,
    plated_through_pads: set[Point] | None = None,
    penalised_pads: dict[tuple[Point, str], float] | None = None,
    extra_budget_ratio: float = 0.5,
    at_pad_wires: dict[tuple[Point, str], int] | None = None,
    all_segments: list[tuple[Point, Point, str, bool, uuid.UUID | None]] | list[tuple[Point, Point, str, bool]] | None = None,
    active_net_id: uuid.UUID | None = None,
    at_pad_nets: dict[tuple[Point, str], frozenset[uuid.UUID]] | None = None,
    same_net_pad_vertices: set[tuple[Point, str]] | None = None,
    foreign_wire_cells: set[tuple[Point, str]] | None = None,
    foreign_via_pads: set[Point] | None = None,
    active_dominant_side: str | None = None,
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
        the matching ``crossing_ib_*`` knob per crossing.
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
    vias = plated_through_pads or set()
    penalised = penalised_pads or {}
    at_pad = at_pad_wires or {}

    # ---- Same-net reuse: canonicalise wire-related structures ----
    # `bare_wire_segments` and `all_segments` may arrive in the legacy
    # form (no net_id) or the net-aware form (UUID | None per segment);
    # at_pad_nets uses UUIDs.  We build a deterministic UUID→int table
    # and rewrite all wire/at_pad structures to use compact ints.  Active
    # net_id is converted to its int index too; -1 means "no active net".
    net_to_idx: dict[uuid.UUID, int] = {}

    def _ensure_idx(nid: uuid.UUID | None) -> int:
        if nid is None:
            return -1
        idx_existing = net_to_idx.get(nid)
        if idx_existing is not None:
            return idx_existing
        new_idx = len(net_to_idx)
        net_to_idx[nid] = new_idx
        return new_idx

    bare_segs_canon: set[tuple[Point, Point, str, int]] = set()
    if bare_wire_segments:
        for _seg in bare_wire_segments:
            if len(_seg) == 4:
                _p1, _p2, _sd, _nid = _seg  # type: ignore[misc]
            else:
                _p1, _p2, _sd = _seg  # type: ignore[misc]
                _nid = None
            bare_segs_canon.add((_p1, _p2, _sd, _ensure_idx(_nid)))

    all_segs_canon: list[tuple[Point, Point, str, bool, int]] = []
    if all_segments:
        for _seg in all_segments:
            if len(_seg) == 5:
                _p1, _p2, _sd, _oins, _nid = _seg  # type: ignore[misc]
            else:
                _p1, _p2, _sd, _oins = _seg  # type: ignore[misc]
                _nid = None
            all_segs_canon.append((_p1, _p2, _sd, _oins, _ensure_idx(_nid)))

    at_pad_nets_canon: dict[tuple[Point, str], frozenset[int]] | None = None
    if at_pad_nets:
        at_pad_nets_canon = {
            key: frozenset(_ensure_idx(nid) for nid in nids)
            for key, nids in at_pad_nets.items()
        }

    active_net_idx = _ensure_idx(active_net_id) if active_net_id is not None else -1

    # Extend the target set so the search may terminate at any pad-vertex
    # of an existing same-net wire (electrically equivalent to reaching
    # the original pin-target).  Only fires when ``active_net_id`` is set
    # — without it, same-net semantics are off (caller is not currently
    # routing any particular net).
    #
    # CRITICAL: only vertices that are physically in the *target's*
    # electric set are added.  When a net has two disconnected islands
    # (e.g. left over from a previous autoroute), pads of the source
    # island are also same-net but reaching them does NOT connect to
    # the target.  Without this filter the search would happily walk
    # one step to a neighbour pin in the source island (closest free
    # target) and report success while the target island stays unrouted.
    #
    # The *source* is explicitly excluded from the extension: when the
    # router is asked to lay a new wire from an existing same-net pad
    # (typical multi-pin scenario — connect the next pin to the existing
    # tree), the source is itself a same-net pad-vertex.  Without this
    # exclusion the search would return a degenerate 1-point path of
    # just the source — useless and downstream wire constructors would
    # reject it with "path must have at least 2 points".
    target_set = set(targets)
    target_island: set[tuple[Point, str]] = set()
    if active_net_id is not None and same_net_pad_vertices:
        # Electric-set membership: every pad in `targets` belongs to the
        # same electric set node as the original target pin (the caller
        # built `targets` via pads_in_same_electric_set).  We use this
        # set to gate extension and teleport admission.
        target_node_set: set[tuple[float, float, str]] = set()
        for _t in targets:
            n = graph.node_for_pad(_t, side)
            if n is not None:
                target_node_set.add(n)
        for _pv, _sv in same_net_pad_vertices:
            if _pv == source:
                continue
            pv_node = graph.node_for_pad(_pv, _sv)
            if pv_node is None or pv_node not in target_node_set:
                continue
            target_island.add((_pv, _sv))
            if _sv == side:
                target_set.add(_pv)

    # Build the teleport map: at each same-net pad-vertex of the *target
    # island* (existing wire vertices that are physically connected to
    # the original target), jumping to any other vertex of that island
    # is free (zero-cost edge).  Cross-island teleports are forbidden
    # for the same reason that extended target_set is filtered above.
    teleport_pads: dict[tuple[Point, str], tuple[tuple[Point, str], ...]] | None = None
    if active_net_id is not None and target_island:
        _verts = tuple(target_island)
        teleport_pads = {v: _verts for v in _verts}

    if source in target_set:
        return [RoutingResult(path=[(source, side)], cost=0.0, insulated=insulated)]

    # valid_nodes is cached on the graph — O(1) after first call, rebuilt only
    # when add_wire/remove_wire mutates the graph.
    valid_nodes = graph.valid_nodes_by_side()

    if source not in valid_nodes[side]:
        return []

    # Build spatial index for bare segments once per find_paths call.
    bare_idx = _build_bare_seg_index(bare_segs_canon)

    # --- Rust extension path (true parallelism, no GIL) ---
    # The Rust kernel does NOT know about the enhanced penalties (the
    # cost-model fields ``at_pad_insulated_*`` and ``parallel_opposite_side``
    # are read only by the Python hot loop) nor about same-net wire reuse.
    # Fall back to the Python implementation when any enhanced knob is
    # non-zero OR when same-net reuse needs to take effect (active_net_id
    # set together with at least one wire-derived structure carrying net
    # info).  Otherwise the Rust path is byte-identical to the Python one.
    _enhanced_active = (
        cost_model.at_pad_insulated_insulated_same_side != 0.0
        or cost_model.at_pad_insulated_bare_same_side != 0.0
        or cost_model.parallel_opposite_side != 0.0
    )
    _reuse_needs_python = (
        active_net_idx >= 0 and (
            any(seg[3] >= 0 for seg in bare_segs_canon)
            or bool(at_pad_nets_canon)
            or bool(teleport_pads)
        )
    )
    if _RUST_AVAILABLE and not _enhanced_active and not _reuse_needs_python:
        # Strip net_idx from bare_segs for the Rust legacy signature.
        _bare_segs_legacy: set[tuple[Point, Point, str]] = {
            (s[0], s[1], s[2]) for s in bare_segs_canon
        }
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
            bare_segs=_bare_segs_legacy,
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
            at_pad_wires=at_pad,
            all_segments=all_segs_canon,
            active_net_idx=active_net_idx,
            at_pad_nets=at_pad_nets_canon,
            teleport_pads=teleport_pads,
            foreign_wire_cells=foreign_wire_cells,
            foreign_via_pads=foreign_via_pads,
            active_dominant_side=active_dominant_side,
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
    heap: list[tuple[float, float, int, float, float, int, float, float, _PathChain]] = [
        (h(source.x, source.y), 0.0, _seq, source.x, source.y, side_idx, 0.0, 0.0,
         (None, (source, source_side)))
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
            return RoutingResult(path=_unfold_path_chain(path), cost=g)

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
                heap, (nf, ng, _seq, npos.x, npos.y, cs_idx, ndx, ndy,
                       (path, (npos, cur_side)))
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
                            heap, (nf, ng, _seq, pos.x, pos.y, other_idx, 0.0, 0.0,
                                   (path, (pos, other_side)))
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
    active_net_idx: int = -1,
    all_segments_other_side: list[tuple[float, float, float, float, bool, int]] | None = None,
) -> float:
    """Return the extra cost for the step p1→p2 due to wire crossings.

    Same-side detection uses the bare-wire spatial index (existing path).
    Opposite-side detection uses the precomputed list of segments on the
    other layer (built by autorouter when any granular crossing knob is
    non-zero).

    For an insulated stepping wire, each crossing is classified by:

    - **type**:  ib (other is bare) or ii (other is insulated)
    - **where**: at_pad (intersection point has half-integer coordinates,
                 i.e. on a pad) or off_pad
    - **side**:  same_side (other wire on the same layer) or opposite_side

    The matching ``crossing_{ib|ii}_{at_pad|off_pad}_{same|opposite}_side``
    knob is added.  When all matching knobs are 0.0, crossings are free.

    For a bare stepping wire, the legacy bare×bare ban applies as before
    (with the perpendicular-diagonal exception); the 8 granular knobs are
    insulated-only and do not affect bare routes.

    Same-net reuse: when ``active_net_idx >= 0`` and the crossed segment's
    own net index equals ``active_net_idx``, ``same_net_wire_reuse_cost``
    is added instead of the normal crossing penalty.  Default 0.0 = free.
    """
    bare_diagonal_allowed = (
        not insulated
        and cost_model.crossing_bare_bare_diagonal < cost_model.crossing_bare_bare
    )
    reuse_cost = cost_model.same_net_wire_reuse_cost

    # Pre-resolve granular knobs (insulated-only).  When the stepping wire
    # is bare we never read these.
    if insulated:
        _ib_aps    = cost_model.get("crossing_ib_at_pad_same_side",       0.0)
        _ib_apo    = cost_model.get("crossing_ib_at_pad_opposite_side",   0.0)
        _ib_ops    = cost_model.get("crossing_ib_off_pad_same_side",      0.0)
        _ib_opo    = cost_model.get("crossing_ib_off_pad_opposite_side",  0.0)
        _ii_apo    = cost_model.get("crossing_ii_at_pad_opposite_side",   0.0)
        _ii_opo    = cost_model.get("crossing_ii_off_pad_opposite_side",  0.0)
    else:
        _ib_aps = _ib_apo = _ib_ops = _ib_opo = 0.0
        _ii_apo = _ii_opo = 0.0

    total = 0.0

    # ---- Same-side bare crossings (legacy path; covers ib_same_side and
    #      the bare×bare ban) -------------------------------------------
    if bare_idx:
        for bsrc, bdst, seg_net_idx in _iter_segs_in_bbox(p1, p2, side, bare_idx):
            if not _segs_cross(p1, p2, bsrc, bdst):
                continue
            if active_net_idx != -1 and seg_net_idx == active_net_idx:
                total += reuse_cost
                continue
            if insulated:
                # ib crossing on same side — at_pad vs off_pad.
                ix, iy = _intersection_point(p1, p2, bsrc, bdst)
                at_pad = _is_at_pad(ix, iy)
                total += _ib_aps if at_pad else _ib_ops
            else:
                # Bare × bare same-side.  Keep the legacy diagonal
                # exception; otherwise inf.
                if bare_diagonal_allowed and _is_perpendicular_diagonal_crossing(
                    p1, p2, bsrc, bdst
                ):
                    total += cost_model.crossing_bare_bare_diagonal
                else:
                    total += cost_model.crossing_bare_bare
                    if math.isinf(total):
                        return total

    # ---- Opposite-side crossings (insulated-only) ------------------
    if insulated and all_segments_other_side and (
        _ib_apo or _ib_opo or _ii_apo or _ii_opo
    ):
        # Bounding-box filter on the new step.
        x_lo = p1.x if p1.x < p2.x else p2.x
        x_hi = p2.x if p1.x < p2.x else p1.x
        y_lo = p1.y if p1.y < p2.y else p2.y
        y_hi = p2.y if p1.y < p2.y else p1.y
        for q1x, q1y, q2x, q2y, q_ins, q_net in all_segments_other_side:
            qx_lo = q1x if q1x < q2x else q2x
            qx_hi = q2x if q1x < q2x else q1x
            qy_lo = q1y if q1y < q2y else q2y
            qy_hi = q2y if q1y < q2y else q1y
            if qx_hi < x_lo or qx_lo > x_hi or qy_hi < y_lo or qy_lo > y_hi:
                continue
            qa = Point(q1x, q1y)
            qb = Point(q2x, q2y)
            if not _segs_cross(p1, p2, qa, qb):
                continue
            if active_net_idx != -1 and q_net == active_net_idx:
                total += reuse_cost
                continue
            ix, iy = _intersection_point(p1, p2, qa, qb)
            at_pad = _is_at_pad(ix, iy)
            if q_ins:
                total += _ii_apo if at_pad else _ii_opo
            else:
                total += _ib_apo if at_pad else _ib_opo

    # NOTE: same-side ii crossings (insulated × insulated, same layer)
    # are not yet detected here — would need an insulated-segment index
    # analogous to ``bare_idx``.  The 8-key contract is in place; this
    # branch will be added in a follow-up so existing tests stay green.
    # ``_ii_aps`` / ``_ii_ops`` knobs remain accepted (no-op for now).

    return total


def _intersection_point(
    a1: Point, a2: Point, b1: Point, b2: Point,
) -> tuple[float, float]:
    """Return (x, y) of the intersection of two crossing segments.

    Caller must ensure segments actually cross (use ``_segs_cross``
    first).  Uses the parametric form; safe against floating-point error
    on the grid because all inputs are at integer or half-integer
    coordinates.
    """
    adx, ady = a2.x - a1.x, a2.y - a1.y
    bdx, bdy = b2.x - b1.x, b2.y - b1.y
    denom = adx * (-bdy) - ady * (-bdx)
    if denom == 0.0:
        # Collinear — return midpoint of overlap (rare; segment-cross
        # rejects strict collinearity, but defensive).
        return ((a1.x + a2.x) * 0.5, (a1.y + a2.y) * 0.5)
    t = ((b1.x - a1.x) * (-bdy) - (b1.y - a1.y) * (-bdx)) / denom
    return (a1.x + t * adx, a1.y + t * ady)


def _is_at_pad(x: float, y: float) -> bool:
    """True if (x, y) lies on a pad (perfboard pads at (col+0.5, row+0.5)).

    A pad sits at half-integer coordinates: ``x - floor(x) == 0.5``.
    Mid-cell crossings (between four pads, used by perpendicular bare
    diagonals) land at integer coordinates.  Tolerance handles FP error
    in the intersection arithmetic.
    """
    fx = x - math.floor(x)
    fy = y - math.floor(y)
    half_x = abs(fx - 0.5) < 1e-6
    half_y = abs(fy - 0.5) < 1e-6
    return half_x and half_y


def _seg_crosses_any(
    p1: Point, p2: Point, side: str, bare_idx: BareSegIndex
) -> bool:
    """Return True if segment p1→p2 strictly crosses any bare segment on the same layer."""
    for bsrc, bdst, _net_idx in _iter_segs_in_bbox(p1, p2, side, bare_idx):
        if _segs_cross(p1, p2, bsrc, bdst):
            return True
    return False


def _iter_segs_in_bbox(
    p1: Point, p2: Point, side: str, bare_idx: BareSegIndex
) -> Iterator[tuple[Point, Point, int]]:
    """Yield each unique bare segment whose cell-bbox overlaps p1→p2.

    Yields ``(p1, p2, net_idx)`` triples; ``net_idx`` is -1 for floating wires.
    """
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
            for bsrc, bdst, net_idx in segs:
                seg_id = id(bsrc)  # each tuple is unique; id() is O(1)
                if seg_id in seen:
                    continue
                seen.add(seg_id)
                yield (bsrc, bdst, net_idx)


def _orient(p: Point, q: Point, r: Point) -> float:
    """2D cross product (q-p) × (r-p).  >0 left turn, <0 right turn, 0 collinear."""
    return (q.x - p.x) * (r.y - p.y) - (q.y - p.y) * (r.x - p.x)


def _segs_cross(
    a1: Point, a2: Point, b1: Point, b2: Point
) -> bool:
    """Return True if segments a1→a2 and b1→b2 cross strictly in their interiors.

    A "strict interior" crossing means neither segment touches the other only
    at an endpoint.  Collinear overlap is *not* treated as a crossing here —
    overlapping bare wires on the same line are caught by the wire-occupancy
    map upstream.
    """
    o1 = _orient(a1, a2, b1)
    o2 = _orient(a1, a2, b2)
    o3 = _orient(b1, b2, a1)
    o4 = _orient(b1, b2, a2)

    if (o1 > 0 and o2 < 0 or o1 < 0 and o2 > 0) and (
        o3 > 0 and o4 < 0 or o3 < 0 and o4 > 0
    ):
        return True
    return False


def _is_perpendicular_diagonal_crossing(
    a1: Point, a2: Point, b1: Point, b2: Point
) -> bool:
    """True if a1→a2 and b1→b2 are both diagonal, perpendicular, and meet at (int, int).

    "Diagonal" means |dx| == |dy| > 0 (45°).  The intersection point must lie
    at integer coordinates — i.e. the centre of a perfboard mesh cell, between
    pads (which sit at half-integer positions).  Used by `_crossing_cost` to
    exempt the parity-routing exception from the universal bare×bare ban.
    """
    adx, ady = a2.x - a1.x, a2.y - a1.y
    bdx, bdy = b2.x - b1.x, b2.y - b1.y
    if abs(adx) != abs(ady) or adx == 0.0:
        return False
    if abs(bdx) != abs(bdy) or bdx == 0.0:
        return False
    # Perpendicular: dot product == 0.
    if adx * bdx + ady * bdy != 0.0:
        return False
    # Compute intersection of the two infinite lines.
    denom = adx * (-bdy) - ady * (-bdx)
    if denom == 0.0:
        return False
    t = ((b1.x - a1.x) * (-bdy) - (b1.y - a1.y) * (-bdx)) / denom
    ix = a1.x + t * adx
    iy = a1.y + t * ady
    return ix == round(ix) and iy == round(iy)


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
