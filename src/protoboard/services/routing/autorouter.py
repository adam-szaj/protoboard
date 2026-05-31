# SPDX-License-Identifier: MIT
"""Full-netlist autorouter using A* with rip-up & retry.

Strategy:
1. Sort nets by pin-count (smallest first — easier nets first).
2. For each unrouted connection in a net (Steiner-like spanning route):
   a. Pick the nearest already-connected pad as source.
   b. Run A* to any unconnected target pad in the same net.
   c. If the path passes through a same-net wire segment before reaching
      the target, truncate it there (same-net wire termination).
   d. If no path found, rip-up the lowest-priority previously routed net
      that overlaps the failed path region and retry.
3. Return a list of new Wire objects (caller adds them to layout).
4. Return a list of nets that could not be routed (unrouted report).

Multi-layer routing:
  On perfboard (plated-through holes), the router may switch layers at any
  pad.  Each layer change costs `cost_model.layer_change`.  The resulting
  path is split into per-layer Wire objects.
"""

from __future__ import annotations

import threading
import csv
import datetime
import io
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TYPE_CHECKING

import structlog

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import PadAtom, StripAtom
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.connectivity.graph import ConnectivityGraph
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import component_bbox, pin_world_position
from protoboard.domain.layout.wire import Wire
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from protoboard.services.routing.astar import PathStep, find_paths, split_path_by_side
from protoboard.services.routing.congestion import build_congestion_map
from protoboard.services.routing.cost_model import RoutingCostModel

if TYPE_CHECKING:
    from protoboard.services.drc.engine import DRCIssue
    from protoboard.services.routing.postprocess import SimplifyMode

# Higher direction_change discourages long detours and keeps wires closer
# to straight-line paths, leaving more corridors free for other nets.
_DEFAULT_COST = RoutingCostModel(direction_change=1.0)

# Sentinel net-id used for floating pins (not in any net).
_FLOATING = uuid.UUID("00000000-0000-0000-0000-000000000002")

_log = structlog.get_logger(__name__)

# Set to True to enable verbose per-segment routing logs.
# Useful for diagnosing suboptimal paths; too noisy for normal use.
_DEEP_DEBUG = False

import atexit as _atexit
import os as _os
_CPU_COUNT = max(2, (_os.cpu_count() or 2))

# Live short-detection: after every add_wire, check whether two distinct
# schematic nets now share an electric set.  Enabled by default while we
# track down the lingering fusion bug; flip the env var to "0" to disable.
_SHORT_DETECTION_ENABLED = _os.environ.get("PROTOBOARD_SHORT_DETECTION", "1") != "0"
_FAIL_ON_SHORT = _os.environ.get("PROTOBOARD_FAIL_ON_SHORT", "0") == "1"

# Per-autoroute-run set of fused-net pairs that have already been reported.
# Reset at the top of every ``autoroute()`` call.  Read by
# ``_check_no_net_fusion`` to dedup repeat warnings about the same persistent
# fusion (one report per pair per run, not one per add_wire).
_SEEN_FUSIONS: set[frozenset[str]] = set()


def _check_no_net_fusion(
    graph: "ConnectivityGraph",
    schematic: "Schematic",
    current_net: "Net",
    just_added: "Wire",
    fail: bool = False,
    seen_fusions: set[frozenset[str]] | None = None,
) -> None:
    """Verify no electric set holds pins from two different schematic nets.

    Run after each add_wire — if the just-added wire fused two nets,
    flag it immediately so the trace can be matched to a specific
    routing decision.  Cost: O(total_pins) per call.  Disable via
    PROTOBOARD_SHORT_DETECTION=0 in production.

    *seen_fusions*: when provided, each unique fused-net pair is logged
    only once per autoroute run.  This stops a single persistent fusion
    from flooding the console with one ERROR per subsequent wire.  The
    caller owns the set lifetime (reset on each ``autoroute()`` call).
    """
    from collections import defaultdict as _dd
    key_to_nets: dict[object, set[str]] = _dd(set)
    for net in schematic.nets.values():
        for ref in net.pins:
            ck = graph.node_for_pin(ref)
            if ck is not None:
                key_to_nets[ck].add(net.name)
    fused = {ck: names for ck, names in key_to_nets.items() if len(names) > 1}
    if not fused:
        return
    # Report each fusion and the wire that triggered it.  Skip pairs
    # already reported in this run (dedup) — keeps per-wire trigger
    # info for genuinely new fusions while suppressing repeats.
    for ck, names in fused.items():
        pair_key = frozenset(names)
        if seen_fusions is not None:
            if pair_key in seen_fusions:
                continue
            seen_fusions.add(pair_key)
        _log.error(
            "autoroute.short_detected",
            current_net=current_net.name,
            triggered_by_wire=str(just_added.id)[:8],
            wire_path=[(p.x, p.y) for p in just_added.path],
            wire_side=just_added.side,
            wire_insulated=just_added.insulated,
            fused_nets=sorted(names),
            canonical=str(ck),
        )
    if fail:
        raise RuntimeError(
            f"Net fusion detected after routing {current_net.name}: "
            f"wire {str(just_added.id)[:8]} merged {sorted(next(iter(fused.values())))}"
        )


def _log_preexisting_fusions(
    graph: "ConnectivityGraph",
    schematic: "Schematic",
    user_wires: list["Wire"],
) -> set[frozenset[str]]:
    """One-shot diagnostic: log every electric-set fusion that already exists
    before autoroute starts adding wires.

    Returns the set of fused-net pairs found — caller passes this as
    *seen_fusions* into per-wire ``_check_no_net_fusion`` calls so the
    same pairs aren't re-reported as autoroute progresses.

    A pre-existing fusion means the project's *data* (component placements
    + footprints + user wires) already short-circuits two schematic nets,
    independently of what autoroute is about to do.  Common causes:
    overlapping pin pads between two different nets, or user wires that
    already bridge nets.
    """
    from collections import defaultdict as _dd
    key_to_nets: dict[object, set[str]] = _dd(set)
    for net in schematic.nets.values():
        for ref in net.pins:
            ck = graph.node_for_pin(ref)
            if ck is not None:
                key_to_nets[ck].add(net.name)
    fused = {ck: names for ck, names in key_to_nets.items() if len(names) > 1}
    seen: set[frozenset[str]] = set()
    for ck, names in fused.items():
        seen.add(frozenset(names))
        _log.info(
            "autoroute.preexisting_fusion",
            fused_nets=sorted(names),
            canonical=str(ck),
            existing_wires=len(user_wires),
            hint=(
                "Two schematic nets already share an electric set BEFORE "
                "autoroute begins. Likely cause: overlapping pin pads in "
                "the placement, or a user-placed wire that bridges nets. "
                "Subsequent per-wire short_detected reports for this pair "
                "will be suppressed."
            ),
        )
    return seen

class _SequentialExecutor:
    """Drop-in replacement for ThreadPoolExecutor that runs work inline.

    Use case 1 — tests: hypothesis fuzz needs deterministic A* output for
    shrinking; tied-cost universes finish in non-deterministic order under
    a real thread pool.
    Use case 2 — diagnostics: set PROTOBOARD_ASTAR_SEQUENTIAL=1 to bypass
    the thread pool entirely, useful when a worker thread crashes in
    third-party native code (Qt+FreeType, CPython GC) and we need to
    rule out concurrency as the cause.
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


# Module-level executor for per-side A* evaluation.  Default is SEQUENTIAL
# — see project_silent_crash_post_upgrade memory for the gory details:
# the ThreadPool path can segfault inside CPython 3.14 GC while a worker
# thread is mark_stacks-ing the heap concurrently with Qt's paintEvent on
# the main thread.  The crash is intermittent, hard to repro under gdb,
# and tied to native Qt+FreeType state we don't own.  Opt back into the
# thread pool with PROTOBOARD_ASTAR_PARALLEL=1 once you've verified your
# environment is unaffected (small projects + xcb platform are usually
# fine).  The legacy PROTOBOARD_ASTAR_SEQUENTIAL=1 still works for
# scripts that explicitly want sequential (e.g. test fixtures).
_ASTAR_PARALLEL = _os.environ.get("PROTOBOARD_ASTAR_PARALLEL", "0") == "1"
_ASTAR_SEQUENTIAL = _os.environ.get("PROTOBOARD_ASTAR_SEQUENTIAL", "0") == "1"
if _ASTAR_PARALLEL and not _ASTAR_SEQUENTIAL:
    _ASTAR_POOL: ThreadPoolExecutor | _SequentialExecutor = ThreadPoolExecutor(
        max_workers=_CPU_COUNT, thread_name_prefix="astar",
    )
    _atexit.register(lambda: _ASTAR_POOL.shutdown(wait=False, cancel_futures=True))
    _log.info("astar.pool", mode="parallel", reason="PROTOBOARD_ASTAR_PARALLEL=1")
else:
    _ASTAR_POOL = _SequentialExecutor()
    _log.info(
        "astar.pool",
        mode="sequential",
        reason=("PROTOBOARD_ASTAR_SEQUENTIAL=1" if _ASTAR_SEQUENTIAL else "default"),
    )


class AutorouteCancelled(Exception):
    """Raised when the cancel_flag is set during routing."""


class AutoroutePreconditionError(Exception):
    """Raised when the input layout fails DRC checks that autoroute requires.

    Carries the offending DRC issues so the caller (GUI worker) can present
    a meaningful dialog: "fix these problems before running autoroute".

    Junk preconditions produce junk autoroute output: if two pins of
    different nets already share a physical pad, or if a user wire
    already shorts two nets, the router cannot recover — it would just
    flood the console with `short_detected` warnings and produce wires
    that DRC would reject anyway.  Bail out cleanly instead.
    """

    def __init__(self, issues: "list[DRCIssue]") -> None:  # noqa: F821
        self.issues = issues
        codes = sorted({iss.code for iss in issues})
        super().__init__(
            f"Autoroute preconditions failed: {len(issues)} DRC issue(s) — "
            f"codes={codes}. Resolve them (Run → DRC) before re-running autoroute."
        )


# Codes from DRC that prevent autoroute from running.  Other DRC errors
# (e.g. BARE_WIRE_INTERIOR_CROSS) are *consequences* of routing, not
# preconditions — they can't occur on an unrouted board.  Keep this set
# narrow so we don't accidentally block on warnings.
#
# Note: ``OVERLAP`` alone (two component pins sharing a physical pad) is
# legal on perfboards — e.g. a floating pin of one component intentionally
# touching a routed pin of another.  It only becomes blocking when the
# shared pad fuses two different schematic nets, which DRC reports
# separately as ``SHORT``.  So we block on SHORT only.
_AUTOROUTE_BLOCKING_DRC_CODES: frozenset[str] = frozenset(
    {"SHORT", "GROUP_MISSING_EXPORT", "OFF_BOARD_PIN"}
)


@dataclass
class NetStats:
    """Per-net routing statistics for one pass."""

    net_name: str
    routed: bool
    nodes_expanded: int        # total A* nodes expanded across all segments/attempts
    over_budget_hits: int      # times over_budget cutoff fired
    rip_up_attempts: int       # number of rip-up retries (0 = routed on first try)
    path_hops_min: int         # shortest segment path (in raster hops)
    path_hops_max: int         # longest segment path
    path_hops_final: int       # total hops in final routed path
    segments_found: int        # number of wire segments placed


@dataclass
class PassStats:
    """Statistics for one full routing pass (greedy or optimize iteration)."""

    pass_index: int            # 0 = greedy, 1..N = optimize iterations
    seed: int                  # net-order seed (0 for greedy)
    routed_count: int
    unrouted_count: int
    total_wires: int
    total_nodes_expanded: int
    total_over_budget_hits: int
    total_rip_up_attempts: int
    nets: list[NetStats] = field(default_factory=list)


@dataclass
class AutorouteResult:
    """Result returned by :func:`autoroute`."""

    wires: list[Wire] = field(default_factory=list)
    unrouted_nets: list[str] = field(default_factory=list)  # net names
    pass_stats: list[PassStats] = field(default_factory=list)


_ORPHAN_EPS = 1e-4


def _drop_orphan_wires(
    layout: "Layout",
    board: "Board",
    pin_positions: "dict[PinRef, tuple[Point, str]]",
) -> int:
    """Remove wires whose endpoints aren't anchored to anything meaningful.

    An endpoint is considered "anchored" when it lies on one of:
    - a board pad (any atom centre),
    - a component pin position,
    - another wire's endpoint (a branch point — two wires sharing a
      junction node are both anchored even if neither endpoint sits on a
      pad, which can happen with mid-air jumpers).

    A wire with *any* unanchored endpoint is dropped — it represents a
    dangling fragment that survived a partial wire-delete or component
    move.  Returns the number of dropped wires.  Mutates ``layout.wires``
    in place.
    """
    if not layout.wires:
        return 0

    def _round(p: Point) -> tuple[float, float]:
        return (round(p.x, 4), round(p.y, 4))

    anchored: set[tuple[float, float]] = set()
    for _atom, pos in board.atoms():
        anchored.add(_round(pos))
    for pos, _side in pin_positions.values():
        anchored.add(_round(pos))

    # Count how many wires touch each endpoint; ≥ 2 wires sharing an
    # endpoint anchors that endpoint as a junction node.
    endpoint_counts: dict[tuple[float, float], int] = {}
    for w in layout.wires:
        if len(w.path) < 2:
            continue
        for endpoint in (w.path[0], w.path[-1]):
            key = _round(endpoint)
            endpoint_counts[key] = endpoint_counts.get(key, 0) + 1

    survivors: list[Wire] = []
    n_dropped = 0
    for w in layout.wires:
        if len(w.path) < 2:
            n_dropped += 1
            continue
        ok = True
        for endpoint in (w.path[0], w.path[-1]):
            key = _round(endpoint)
            if key in anchored:
                continue
            if endpoint_counts.get(key, 0) >= 2:
                continue
            ok = False
            break
        if ok:
            survivors.append(w)
        else:
            n_dropped += 1

    if n_dropped:
        layout.wires[:] = survivors
    return n_dropped


def autoroute(
    board: Board,
    layout: Layout,
    schematic: Schematic,
    components: dict[str, ComponentDefinition],
    instances: dict[uuid.UUID, ComponentInstance],
    side: Literal["top", "bottom"] = "top",
    cost_model: RoutingCostModel = _DEFAULT_COST,
    max_rip_up_passes: int = 3,
    insulated: bool = True,
    allow_layer_change: bool = True,
    max_optimize_iterations: int = 10,
    preferred_side: Literal["top", "bottom"] = "bottom",
    wire_mode: Literal["bare", "insulated", "auto", "bare_diagonal"] = "insulated",
    max_detour_factor: float = 0.0,
    progress_callback: Callable[[int, str, int], None] | None = None,
    optimize_progress_callback: Callable[[int, int], None] | None = None,
    cancel_flag: threading.Event | None = None,
    net_ids: set[uuid.UUID] | None = None,
    report_path: "Path | None" = None,
    push_and_shove: bool = True,
    enable_simplify_pass: bool = True,
    simplify_mode: "SimplifyMode | None" = None,
) -> AutorouteResult:
    """Route all unrouted nets in *schematic* and return new wire segments.

    Parameters
    ----------
    wire_mode:
        Strategy for choosing wire insulation:
        ``"bare"`` — always route as bare wire;
        ``"insulated"`` — always route as insulated wire (legacy default);
        ``"auto"`` — try bare first, fall back to insulated if no bare path exists.
        The legacy ``insulated`` parameter is ignored when ``wire_mode`` is set
        to anything other than its default ``"insulated"``.
    max_detour_factor:
        If > 0, reject any routed path whose length exceeds
        ``manhattan_distance * max_detour_factor``.  Rejected paths are treated
        as unrouted (rip-up retry applies).  ``0.0`` means no limit.
    insulated:
        Deprecated — use ``wire_mode`` instead.  Kept for backward compatibility.
    allow_layer_change:
        When True (default), the router may route through the bottom layer when
        it reduces total wire length or avoids obstacles — using plated-through
        pads as vias.  Set to False to restrict to a single layer.
    max_optimize_iterations:
        Number of additional re-routing passes after the initial greedy pass.
        Each pass re-routes all (successfully routed) nets from scratch using
        the global wire state from the previous iteration, so early nets benefit
        from knowing where later nets actually ended up.  Stops early when the
        wire layout converges (no path changed).  Set to 0 to disable.
    preferred_side:
        When two paths have equal cost (one on top, one on bottom), the one on
        this side wins the tie-break.  Defaults to ``"bottom"`` — bottom-layer
        routing is generally preferred because it leaves the top layer clear for
        component inspection and rework.

    net_ids:
        When provided, only route nets whose UUID is in this set.  All other
        nets are left untouched (existing wires for those nets are preserved as
        obstacles).  ``None`` (default) routes all nets.

    Already-routed connections (where a physical path already exists) are
    skipped.  Only completely *missing* connections are routed.

    The caller is responsible for adding the returned wires to the layout
    and running DRC.
    """
    result = AutorouteResult()

    # ------------------------------------------------------------------
    # Precondition check: run DRC and abort on errors that autoroute
    # cannot recover from (SHORT, OVERLAP).  Junk preconditions produce
    # junk autoroute output — better to fail fast with a clear error
    # than to flood the log with shorts and emit broken wires.
    # ------------------------------------------------------------------
    from protoboard.services.drc.engine import DRCEngine
    _drc_issues = DRCEngine().check(
        board=board, schematic=schematic, layout=layout,
        components=components, instances=instances,
    )
    _blocking = [
        iss for iss in _drc_issues
        if iss.severity == "error"
        and iss.code in _AUTOROUTE_BLOCKING_DRC_CODES
    ]
    if _blocking:
        _log.error(
            "autoroute.precondition_failed",
            blocking_count=len(_blocking),
            codes=sorted({iss.code for iss in _blocking}),
        )
        raise AutoroutePreconditionError(_blocking)

    # Build pin-position lookup: PinRef → (world_position, side).
    pin_positions = _build_pin_positions(layout, components, instances)

    # Off-board precondition: pins outside the board grid have no valid
    # node in the routing graph.  A* started from such a pin would
    # explore the entire grid trying to reach an unreachable target —
    # with ``max_detour_factor=0.0`` (no budget cap) this exhausts memory
    # on dense boards.  Bail out before A* sees anything.
    from protoboard.services.drc.engine import DRCIssue as _DRCIssue
    from protoboard.domain.layout.wire_utils import board_pads_for_side
    _board_pad_set = (
        board_pads_for_side(board, "top")
        | board_pads_for_side(board, "bottom")
    )
    if net_ids is not None:
        _nets_to_check = [n for n in schematic.nets.values() if n.id in net_ids]
    else:
        _nets_to_check = list(schematic.nets.values())
    _off_board_issues: list[_DRCIssue] = []
    for _net in _nets_to_check:
        for _ref in _net.pins:
            _ps = pin_positions.get(_ref)
            if _ps is None:
                continue
            _pt, _side = _ps
            if _pt not in _board_pad_set:
                _off_board_issues.append(_DRCIssue(
                    severity="error",
                    code="OFF_BOARD_PIN",
                    message=(
                        f"Pin {_ref.pin_number} of component {_ref.component_id} "
                        f"on net '{_net.name}' is outside the board grid "
                        f"({_pt}).  Move the component onto the board before "
                        f"autorouting."
                    ),
                    locations=[_pt],
                    affected_nets=[_net.id],
                    affected_pins=[_ref],
                    affected_components=[_ref.component_id],
                ))
    if _off_board_issues:
        _log.error(
            "autoroute.precondition_failed",
            blocking_count=len(_off_board_issues),
            codes=["OFF_BOARD_PIN"],
        )
        raise AutoroutePreconditionError(_off_board_issues)

    # Build a map: (world_position, side) → net_id for every placed pin that
    # belongs to a net, registered on BOTH layers (plated-through hole).
    point_side_to_net: dict[tuple[Point, str], uuid.UUID] = {}
    for net in schematic.nets.values():
        for ref in net.pins:
            ps = pin_positions.get(ref)
            if ps is not None:
                pt, _s = ps
                point_side_to_net[(pt, "top")] = net.id
                point_side_to_net[(pt, "bottom")] = net.id

    # Collect ALL placed pin positions — including floating pins — on BOTH layers.
    # A component pin occupies a physical hole; routing through that hole on
    # either layer creates an unintended electrical connection.  Therefore we
    # block (Point, "top") AND (Point, "bottom") for every placed pin.
    all_pin_positions: dict[tuple[Point, str], uuid.UUID] = {}
    for ps in pin_positions.values():
        pt, _side = ps
        net_id = point_side_to_net.get(ps, _FLOATING)
        all_pin_positions[(pt, "top")] = net_id
        all_pin_positions[(pt, "bottom")] = net_id

    # Collect plated-through pad positions from the board for via routing.
    plated_through_pads = _collect_plated_through_pads(board) if allow_layer_change else set()

    # Build a penalty map for pads on the top layer that lie under a component
    # body.  Routing there is legal but physically awkward; bottom is preferred.
    under_component_penalty = _build_under_component_penalty(
        board=board,
        layout=layout,
        components=components,
        instances=instances,
        penalty=cost_model.under_component_top,
    )

    # Pre-route congestion estimator: lay trivial L-shape paths over each net's
    # MST and tally per-cell vote counts.  These extra per-cell costs nudge
    # long nets routed first to detour around clusters where short nets will
    # later need corridor space.  Disabled when congestion_weight <= 0.
    congestion_weight = cost_model.get("congestion_weight", 0.0)
    if congestion_weight > 0.0:
        all_nets_for_map = (
            [n for n in schematic.nets.values() if n.id in net_ids]
            if net_ids is not None
            else list(schematic.nets.values())
        )
        congestion_map = build_congestion_map(all_nets_for_map, pin_positions)
        # Merge into under_component_penalty (side-agnostic → both layers).
        # Sum, not max: under-component and corridor congestion are independent
        # physical effects and should accumulate.
        for cell, votes in congestion_map.items():
            extra = congestion_weight * votes
            for side_key in ("top", "bottom"):
                key = (cell, side_key)
                under_component_penalty[key] = under_component_penalty.get(key, 0.0) + extra

    # Sort nets: longest manhattan distance first.  Long nets need more board
    # space and should be planned while the board is still empty; short nets
    # have more alternative paths and are easier to squeeze in last.
    # Within the same distance, sort by descending pin count for stability.
    def _net_sort_key(net: Net) -> tuple[int, float, int]:
        # Global nets (GND, VCC, etc.) are sorted last — they typically have many
        # pins spread across the board and auto-route poorly without manual help.
        global_penalty = 1 if net.global_net else 0
        pts = [pin_positions[r][0] for r in net.pins if r in pin_positions]
        if len(pts) < 2:
            return (global_penalty, 0.0, -len(pts))
        max_d = max(
            abs(a.x - b.x) + abs(a.y - b.y)
            for i, a in enumerate(pts)
            for b in pts[i + 1:]
        )
        return (global_penalty, -max_d, -len(pts))

    all_nets = (
        [n for n in schematic.nets.values() if n.id in net_ids]
        if net_ids is not None
        else list(schematic.nets.values())
    )
    sorted_nets = sorted(all_nets, key=_net_sort_key)

    # Drop wires whose endpoint isn't anchored to a pad, a component pin or
    # another wire's endpoint.  Such "orphan" segments (e.g. left behind
    # after a component move or a partial wire-delete) confuse the
    # ConnectivityGraph and bias rip-up scoring — better to discard them
    # before snapshotting.  The dropped wires are also removed from
    # ``layout.wires`` so the post-autoroute state is consistent.
    n_dropped = _drop_orphan_wires(layout, board, pin_positions)

    # Snapshot of user-placed wires — never mutated; each pass starts from here.
    user_wires: list[Wire] = list(layout.wires)

    _log.info(
        "autoroute.start",
        nets=len(sorted_nets),
        existing_wires=len(user_wires),
        orphans_dropped=n_dropped,
        allow_layer_change=allow_layer_change,
        max_optimize_iterations=max_optimize_iterations,
        net_order=[n.name for n in sorted_nets],
    )

    # Reset the per-run dedup set so each autoroute invocation starts fresh.
    # Then build a one-shot ConnectivityGraph that includes the user_wires
    # snapshot but no autoroute output, and log every fusion that already
    # exists.  Pairs reported here are also added to ``_SEEN_FUSIONS`` so
    # the per-wire detector won't re-report them 300 times during the run.
    _SEEN_FUSIONS.clear()
    if _SHORT_DETECTION_ENABLED:
        _preautoroute_graph = ConnectivityGraph(
            board=board,
            layout=layout,
            components=components,
            instances=instances,
        )
        for _w in user_wires:
            _preautoroute_graph.add_wire(_w)
        _SEEN_FUSIONS.update(
            _log_preexisting_fusions(_preautoroute_graph, schematic, user_wires)
        )

    n_nets = len(sorted_nets)

    # --- Initial greedy pass ---
    current_wires, unrouted_names, _greedy_ps = _run_one_pass(
        sorted_nets=sorted_nets,
        pin_positions=pin_positions,
        all_pin_positions=all_pin_positions,
        schematic=schematic,
        board=board,
        layout=layout,
        user_wires=user_wires,
        cost_model=cost_model,
        max_rip_up_passes=max_rip_up_passes,
        side=side,
        insulated=insulated,
        wire_mode=wire_mode,
        max_detour_factor=max_detour_factor,
        plated_through_pads=plated_through_pads,
        under_component_penalty=under_component_penalty,
        skip_net_ids=set(),
        components=components,
        instances=instances,
        preferred_side=preferred_side,
        progress_callback=progress_callback,
        cancel_flag=cancel_flag,
        pass_num=0,
        n_nets=n_nets,
        push_and_shove=push_and_shove,
    )
    result.pass_stats.append(_greedy_ps)

    # Nets that failed the initial pass are permanently skipped in optimization —
    # the board state only gets busier in subsequent iterations.
    failed_net_ids: set[uuid.UUID] = {
        net.id for net in sorted_nets if net.name in unrouted_names
    }

    # --- Iterative optimization passes (parallel across iterations) ---
    # Each optimization pass is independent — it starts from user_wires and
    # explores different net orderings. Run them in parallel using processes
    # (bypasses GIL) and keep the result with fewest unrouted nets.
    if max_optimize_iterations > 0:
        _pass_args = (
            sorted_nets, pin_positions, all_pin_positions, schematic, board,
            layout, user_wires, cost_model, max_rip_up_passes, side, insulated,
            wire_mode, max_detour_factor,
            plated_through_pads, under_component_penalty, failed_net_ids,
            components, instances, preferred_side,
        )
        n_parallel = min(max_optimize_iterations, _CPU_COUNT)
        _log.info("autoroute.optimize.parallel", passes=max_optimize_iterations, workers=n_parallel)

        from concurrent.futures import ProcessPoolExecutor
        best_wires = current_wires
        best_unrouted_count = len(unrouted_names)

        try:
            _log.info("autoroute.optimize.pool_start",
                      n_parallel=n_parallel, iterations=max_optimize_iterations)
            pool = ProcessPoolExecutor(max_workers=n_parallel)
            try:
                _log.info("autoroute.optimize.pool_created")
                futures = [
                    pool.submit(_run_opt_pass_in_process, _pass_args + (seed,))
                    for seed in range(max_optimize_iterations)
                ]
                _log.info("autoroute.optimize.futures_submitted", count=len(futures))
                n_futures = len(futures)
                if optimize_progress_callback is not None:
                    optimize_progress_callback(0, n_futures)
                for done_count, fut in enumerate(as_completed(futures), start=1):
                    if cancel_flag is not None and cancel_flag.is_set():
                        raise AutorouteCancelled
                    if optimize_progress_callback is not None:
                        optimize_progress_callback(done_count, n_futures)
                    try:
                        opt_wires, opt_unrouted, opt_ps = fut.result()
                        result.pass_stats.append(opt_ps)
                        _log.info("autoroute.optimize.future_done",
                                  wires=len(opt_wires), unrouted=len(opt_unrouted))
                    except Exception as _fe:
                        _log.warning("autoroute.optimize.future_error", error=str(_fe))
                        continue
                    if len(opt_unrouted) < best_unrouted_count or (
                        len(opt_unrouted) == best_unrouted_count
                        and len(opt_wires) >= len(best_wires)
                    ):
                        best_wires = opt_wires
                        best_unrouted_count = len(opt_unrouted)
                        unrouted_names = opt_unrouted
                _log.info("autoroute.optimize.pool_done",
                          best_wires=len(best_wires), best_unrouted=best_unrouted_count)
            finally:
                # Always tear the pool down without blocking on running tasks.
                # On cancel we additionally SIGTERM the worker processes so
                # they don't keep burning CPU after the user clicked Cancel.
                _shutdown_pool_fast(pool, cancel_flag)
        except AutorouteCancelled:
            # User cancellation is intentional — propagate without falling back.
            raise
        except Exception as e:
            # ProcessPoolExecutor may fail in some environments (e.g. no fork,
            # pickle error). Catch ALL exceptions and fall back to sequential.
            _log.warning("autoroute.optimize.fallback_sequential",
                         exc_type=type(e).__name__, reason=str(e))
            prev_fp = _wire_fingerprint(current_wires)
            for opt_iter in range(max_optimize_iterations):
                if cancel_flag is not None and cancel_flag.is_set():
                    raise AutorouteCancelled
                next_wires, next_unrouted, _opt_ps = _run_one_pass(
                    sorted_nets=sorted_nets,
                    pin_positions=pin_positions,
                    all_pin_positions=all_pin_positions,
                    schematic=schematic,
                    board=board,
                    layout=layout,
                    user_wires=user_wires,
                    cost_model=cost_model,
                    max_rip_up_passes=max_rip_up_passes,
                    side=side,
                    insulated=insulated,
                    wire_mode=wire_mode,
                    max_detour_factor=max_detour_factor,
                    plated_through_pads=plated_through_pads,
                    under_component_penalty=under_component_penalty,
                    skip_net_ids=failed_net_ids,
                    components=components,
                    instances=instances,
                    preferred_side=preferred_side,
                    progress_callback=progress_callback,
                    cancel_flag=cancel_flag,
                    pass_num=opt_iter + 1,
                    n_nets=n_nets,
                )
                result.pass_stats.append(_opt_ps)
                new_fp = _wire_fingerprint(next_wires)
                if len(next_unrouted) < best_unrouted_count or (
                    len(next_unrouted) == best_unrouted_count
                    and new_fp != prev_fp
                ):
                    best_wires = next_wires
                    best_unrouted_count = len(next_unrouted)
                    unrouted_names = next_unrouted
                if new_fp == prev_fp:
                    break
                prev_fp = new_fp

        current_wires = best_wires

    # Post-process: shorten wires via the SimplifyOptimizer.  Kept here
    # for backward-compat with callers passing the legacy
    # `enable_simplify_pass` kwarg; the optimizer is the same one the
    # plugin-aware worker invokes via the registry.
    if wire_mode == "bare_diagonal" and enable_simplify_pass and current_wires:
        from protoboard.services.routing.builtin_optimizers import SimplifyOptimizer
        from protoboard.services.routing.protocol import ProgressSink
        from protoboard.services.routing.snapshot import RouteRequest

        # Build a snapshot of the current (post-routing) state.  The
        # routing_settings field is unused by SimplifyOptimizer but the
        # snapshot constructor requires it, so reach for a minimal one.
        from protoboard.domain.routing_settings import RoutingSettings
        _req = RouteRequest.from_session(
            board=board,
            layout=layout,
            schematic=schematic,
            components=components,
            instances=instances,
            routing_settings=RoutingSettings(),
        )
        _intermediate = AutorouteResult(
            wires=current_wires,
            unrouted_nets=list(unrouted_names),
            pass_stats=list(result.pass_stats),
        )
        _opt = SimplifyOptimizer(mode=simplify_mode)
        _optimized = _opt.optimize(_req, _intermediate, ProgressSink(cancel_flag=cancel_flag))
        current_wires = _optimized.wires

    result.wires = current_wires
    result.unrouted_nets = unrouted_names
    _log.info(
        "autoroute.done",
        routed_wires=len(current_wires),
        unrouted_nets=unrouted_names,
    )
    if report_path is not None:
        _write_report(result, report_path)
    return result


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------

def _write_report(result: "AutorouteResult", path: Path) -> None:
    """Append a CSV report for this autoroute run to *path*.

    Format (append mode, UTF-8):
      # run <timestamp>
      ## passes
      pass_index, seed, routed_count, unrouted_count, total_wires,
        total_nodes_expanded, total_over_budget_hits, total_rip_up_attempts
      <one row per pass>
      ## nets  pass=<pass_index>
      net_name, routed, nodes_expanded, over_budget_hits, rip_up_attempts,
        path_hops_min, path_hops_max, path_hops_final, segments_found
      <one row per net>
      ... repeated for each pass
    """
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    buf = io.StringIO()
    w = csv.writer(buf)

    buf.write(f"# run {ts}\n")

    # --- passes summary ---
    buf.write("## passes\n")
    w.writerow([
        "pass_index", "seed",
        "routed_count", "unrouted_count", "total_wires",
        "total_nodes_expanded", "total_over_budget_hits", "total_rip_up_attempts",
    ])
    for ps in result.pass_stats:
        w.writerow([
            ps.pass_index, ps.seed,
            ps.routed_count, ps.unrouted_count, ps.total_wires,
            ps.total_nodes_expanded, ps.total_over_budget_hits, ps.total_rip_up_attempts,
        ])

    # --- per-net details per pass ---
    for ps in result.pass_stats:
        buf.write(f"## nets pass={ps.pass_index}\n")
        w.writerow([
            "net_name", "routed",
            "nodes_expanded", "over_budget_hits", "rip_up_attempts",
            "path_hops_min", "path_hops_max", "path_hops_final", "segments_found",
        ])
        for ns in ps.nets:
            w.writerow([
                ns.net_name, int(ns.routed),
                ns.nodes_expanded, ns.over_budget_hits, ns.rip_up_attempts,
                ns.path_hops_min, ns.path_hops_max, ns.path_hops_final, ns.segments_found,
            ])

    buf.write("\n")

    try:
        with path.open("a", encoding="utf-8", newline="") as f:
            f.write(buf.getvalue())
        _log.info("autoroute.report_written", path=str(path))
    except OSError as e:
        _log.warning("autoroute.report_write_failed", path=str(path), error=str(e))


# ---------------------------------------------------------------------------
# Process-pool helpers (module-level so pickle works)
# ---------------------------------------------------------------------------


def _shutdown_pool_fast(
    pool: "ProcessPoolExecutor",  # type: ignore[name-defined]
    cancel_flag: "threading.Event | None",
) -> None:
    """Tear down a ProcessPoolExecutor without waiting for running tasks.

    Normal `ProcessPoolExecutor.shutdown(wait=True)` blocks until every
    worker finishes its current task — useless when the user just hit
    Cancel and we want the CPU back now.  Steps:
      1. Cancel queued (not-yet-started) futures.
      2. SIGTERM every live worker process so the running A* call dies
         immediately.  Only do this on cancel; a normal completion path
         lets workers exit gracefully.
      3. Call shutdown(wait=False) so the executor's manager threads
         stop scheduling but don't block our thread on the join.
    """
    import signal as _sig
    cancelled = cancel_flag is not None and cancel_flag.is_set()
    try:
        pool.shutdown(wait=False, cancel_futures=True)
    except Exception as _exc:  # noqa: BLE001 — never let teardown raise
        _log.warning("autoroute.optimize.shutdown_error", error=str(_exc))
    if not cancelled:
        return
    # Reach into the executor to SIGTERM each worker.  `_processes` is a
    # dict[pid, Process] in CPython 3.11+; if its shape changes we silently
    # skip the kill and accept the lingering CPU.
    workers = getattr(pool, "_processes", None)
    if not workers:
        return
    for proc in list(workers.values()):
        try:
            if proc.is_alive():
                proc.terminate()
        except Exception:  # noqa: BLE001
            pass
    _log.info("autoroute.optimize.workers_terminated", count=len(workers))


def _run_opt_pass_in_process(args: tuple) -> tuple[list[Wire], list[str]]:  # type: ignore[type-arg]
    """Top-level wrapper for ProcessPoolExecutor — one optimization pass."""
    import os as _os2, threading as _th
    _log.info("autoroute.subprocess.start",
              pid=_os2.getpid(),
              thread_id=_th.get_ident(),
              is_main=_th.current_thread() is _th.main_thread())

    (sorted_nets, pin_positions, all_pin_positions, schematic, board, layout,
     user_wires, cost_model, max_rip_up_passes, side, insulated,
     wire_mode, max_detour_factor,
     plated_through_pads, under_component_penalty, failed_net_ids,
     components, instances, preferred_side, seed_offset) = args

    # Shuffle net order by seed_offset to explore different routing strategies.
    import random as _rnd
    nets = list(sorted_nets)
    if seed_offset > 0:
        _rnd.Random(seed_offset).shuffle(nets)

    return _run_one_pass(
        sorted_nets=nets,
        pin_positions=pin_positions,
        all_pin_positions=all_pin_positions,
        schematic=schematic,
        board=board,
        layout=layout,
        user_wires=user_wires,
        cost_model=cost_model,
        max_rip_up_passes=max_rip_up_passes,
        side=side,
        insulated=insulated,
        wire_mode=wire_mode,
        max_detour_factor=max_detour_factor,
        plated_through_pads=plated_through_pads,
        under_component_penalty=under_component_penalty,
        skip_net_ids=failed_net_ids,
        components=components,
        instances=instances,
        preferred_side=preferred_side,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _wire_fingerprint(
    wires: list[Wire],
) -> frozenset[tuple[tuple[tuple[float, float], ...], str, bool]]:
    """Canonical fingerprint of a wire layout for convergence detection.

    Uses sorted point coordinates so that direction-reversed identical paths
    hash equal.  The result is order-independent (frozenset over all wires).
    """
    return frozenset(
        (tuple(sorted((p.x, p.y) for p in w.path)), w.side, w.insulated)
        for w in wires
    )


def _run_one_pass(
    sorted_nets: list[Net],
    *,
    pin_positions: dict[PinRef, tuple[Point, str]],
    all_pin_positions: dict[tuple[Point, str], uuid.UUID],
    schematic: Schematic,
    board: Board,
    layout: Layout,
    user_wires: list[Wire],
    cost_model: RoutingCostModel,
    max_rip_up_passes: int,
    side: Literal["top", "bottom"],
    insulated: bool,
    wire_mode: Literal["bare", "insulated", "auto", "bare_diagonal"] = "insulated",
    max_detour_factor: float = 0.0,
    plated_through_pads: set[Point] = set(),
    under_component_penalty: dict[tuple[Point, str], float] = {},
    skip_net_ids: set[uuid.UUID] = set(),
    components: dict[str, ComponentDefinition] = {},
    instances: dict[uuid.UUID, ComponentInstance] = {},
    preferred_side: Literal["top", "bottom"] = "bottom",
    progress_callback: Callable[[int, str, int], None] | None = None,
    cancel_flag: threading.Event | None = None,
    pass_num: int = 0,
    n_nets: int = 0,
    push_and_shove: bool = True,
) -> tuple[list[Wire], list[str], PassStats]:
    """Run one full greedy routing pass over *sorted_nets*.

    Starts from *user_wires* (the original user-placed wires only) and appends
    newly routed wires as each net is processed.  Nets whose id is in
    *skip_net_ids* are skipped and recorded as unrouted without attempting A*.

    Returns ``(new_wires, unrouted_net_names, pass_stats)``.
    """
    working_wires: list[Wire] = list(user_wires)
    new_wires: list[Wire] = []
    unrouted: list[str] = []
    _pass_net_stats: list[NetStats] = []

    # Pre-compute total segment count for sub-net progress granularity.
    # A net with k placed pins needs k-1 segments (Steiner spanning tree).
    total_segments = max(1, sum(
        max(0, len([r for r in net.pins if r in pin_positions]) - 1)
        for net in sorted_nets
        if net.id not in skip_net_ids
    ))
    segs_done = 0

    for net_idx, net in enumerate(sorted_nets):
        if cancel_flag is not None and cancel_flag.is_set():
            raise AutorouteCancelled
        if progress_callback is not None:
            pct = int(segs_done * 100 / total_segments)
            progress_callback(pass_num, net.name, pct)
        if net.id in skip_net_ids:
            unrouted.append(net.name)
            continue

        net_placed = len([r for r in net.pins if r in pin_positions])
        net_segs = max(0, net_placed - 1)

        if progress_callback is not None:
            _cb = progress_callback
            _pass = pass_num
            _base = segs_done
            _total = total_segments

            def _seg_cb(net_name: str, seg_idx: int, seg_total: int) -> None:
                pct = int((_base + seg_idx) * 100 / _total)
                _cb(_pass, net_name, pct)
        else:
            _seg_cb = None  # type: ignore[assignment]

        routed = _route_net(
            net=net,
            pin_positions=pin_positions,
            all_pin_positions=all_pin_positions,
            schematic=schematic,
            board=board,
            layout=layout,
            working_wires=working_wires,
            components=components,
            instances=instances,
            side=side,
            cost_model=cost_model,
            max_rip_up_passes=max_rip_up_passes,
            insulated=insulated,
            wire_mode=wire_mode,
            max_detour_factor=max_detour_factor,
            plated_through_pads=plated_through_pads,
            under_component_penalty=under_component_penalty,
            preferred_side=preferred_side,
            segment_callback=_seg_cb,
            cancel_flag=cancel_flag,
            push_and_shove=push_and_shove,
            pass_new_wires=new_wires,
        )
        segs_done += net_segs
        if routed is None:
            unrouted.append(net.name)
            _pass_net_stats.append(NetStats(
                net_name=net.name, routed=False,
                nodes_expanded=0, over_budget_hits=0, rip_up_attempts=0,
                path_hops_min=0, path_hops_max=0, path_hops_final=0, segments_found=0,
            ))
        else:
            routed_wires, net_st = routed
            new_wires.extend(routed_wires)
            working_wires.extend(routed_wires)
            _pass_net_stats.append(net_st)

    ps = PassStats(
        pass_index=pass_num,
        seed=pass_num,
        routed_count=len(_pass_net_stats) - len(unrouted),
        unrouted_count=len(unrouted),
        total_wires=len(new_wires),
        total_nodes_expanded=sum(s.nodes_expanded for s in _pass_net_stats),
        total_over_budget_hits=sum(s.over_budget_hits for s in _pass_net_stats),
        total_rip_up_attempts=sum(s.rip_up_attempts for s in _pass_net_stats),
        nets=_pass_net_stats,
    )
    return new_wires, unrouted, ps


def _collect_plated_through_pads(board: Board) -> set[Point]:
    """Return positions of all plated-through pads on the board."""
    pts: set[Point] = set()
    for atom, pos in board.atoms():
        if isinstance(atom, PadAtom) and atom.layers.plated_through:
            pts.add(pos)
        elif isinstance(atom, StripAtom) and atom.layers.plated_through:
            # Each hole of the strip is a plated-through pad.
            n = int(atom.length) if isinstance(atom.length, int) else 1
            if atom.orientation == "horizontal":
                start_x = pos.x - n / 2.0 + 0.5
                for i in range(n):
                    pts.add(Point(start_x + i, pos.y))
            else:
                start_y = pos.y - n / 2.0 + 0.5
                for i in range(n):
                    pts.add(Point(pos.x, start_y + i))
    return pts


def _build_pin_positions(
    layout: Layout,
    components: dict[str, ComponentDefinition],
    instances: dict[uuid.UUID, ComponentInstance],
) -> dict[PinRef, tuple[Point, str]]:
    """Return PinRef → (world_position, side) for every placed pin."""
    pos: dict[PinRef, tuple[Point, str]] = {}
    for comp_id, placement in layout.placements.items():
        inst = instances.get(comp_id)
        if inst is None:
            continue
        defn = components.get(inst.definition_ref)
        if defn is None:
            continue
        for pin_num in range(1, defn.footprint.pin_count() + 1):
            p = pin_world_position(defn.footprint, placement, pin_num)
            pos[PinRef(component_id=comp_id, pin_number=pin_num)] = (p, placement.side)
    return pos


def _route_net(
    net: Net,
    pin_positions: dict[PinRef, tuple[Point, str]],
    all_pin_positions: dict[tuple[Point, str], uuid.UUID],
    schematic: Schematic,
    board: Board,
    layout: Layout,
    working_wires: list[Wire],
    components: dict[str, ComponentDefinition],
    instances: dict[uuid.UUID, ComponentInstance],
    side: Literal["top", "bottom"],
    cost_model: RoutingCostModel,
    max_rip_up_passes: int,
    insulated: bool,
    wire_mode: Literal["bare", "insulated", "auto", "bare_diagonal"] = "insulated",
    max_detour_factor: float = 0.0,
    plated_through_pads: set[Point] = set(),
    under_component_penalty: dict[tuple[Point, str], float] = {},
    preferred_side: Literal["top", "bottom"] = "bottom",
    segment_callback: Callable[[str, int, int], None] | None = None,
    cancel_flag: threading.Event | None = None,
    push_and_shove: bool = True,
    pass_new_wires: list[Wire] | None = None,
) -> tuple[list[Wire], NetStats] | None:
    """Route all connections for *net*.  Returns (wires, stats) or None on failure.

    *pass_new_wires* (optional) is the pass-level list of wires routed so far
    in this `_run_one_pass`.  When a rip-up removes a wire that was routed by
    an earlier net, we also need to remove it from this list — otherwise the
    donor net keeps a wire in its result that no longer exists in
    working_wires/connectivity_graph, producing a phantom duplicate route.
    """
    placed_refs = [ref for ref in net.pins if ref in pin_positions]
    if len(placed_refs) < 2:
        if _DEEP_DEBUG:
            _log.debug("autoroute.net.skip", net=net.name, reason="fewer than 2 placed pins")
        return ([], NetStats(
            net_name=net.name, routed=True,
            nodes_expanded=0, over_budget_hits=0, rip_up_attempts=0,
            path_hops_min=0, path_hops_max=0, path_hops_final=0, segments_found=0,
        ))

    if _DEEP_DEBUG:
        _log.debug(
            "autoroute.net.start",
            net=net.name,
            pins=[
                {"cid": str(r.component_id)[:8], "pin": r.pin_number,
                 "pos": f"({pin_positions[r][0].x},{pin_positions[r][0].y})",
                 "side": pin_positions[r][1]}
                for r in placed_refs
            ],
        )

    net_id = net.id
    # Own-net pad positions on BOTH layers (plated-through hole).
    net_ps: set[tuple[Point, str]] = set()
    for ref in net.pins:
        if ref in pin_positions:
            pt, _s = pin_positions[ref]
            net_ps.add((pt, "top"))
            net_ps.add((pt, "bottom"))
    occupied_pads: dict[tuple[Point, str], uuid.UUID] = {
        ps: nid
        for ps, nid in all_pin_positions.items()
        if ps not in net_ps
    }
    same_net_pads: dict[tuple[Point, str], uuid.UUID] = {
        ps: net_id for ps in net_ps
    }

    new_wires: list[Wire] = []
    _ns_nodes = 0
    _ns_ob = 0
    _ns_ripup = 0
    _ns_hops: list[int] = []

    # Build the connectivity graph once and update it incrementally.
    # We pass a layout without wires and then add working_wires via add_wire(),
    # which is O(path_len) per wire rather than O(full_graph) per rebuild.
    current_graph = ConnectivityGraph(
        board=board,
        layout=layout,
        components=components,
        instances=instances,
    )
    for _w in working_wires:
        current_graph.add_wire(_w)

    # Build wire_to_net_id once per `_route_net` call.  Used by the
    # same-net-reuse cost machinery to recognise existing wires of the
    # active net as electrically "free" (no crossing/at-pad/parallel
    # penalty; teleport edges between same-net pad-vertices).
    #
    # We resolve a wire to its net by checking whether any vertex of its
    # path coincides with a pin of some net; the first match wins (T-junction
    # branches still resolve correctly because *any* pin on the connected
    # component determines the net).  Wires that touch no pin remain
    # net_id=None and never participate in the reuse discount.
    pin_pos_to_net: dict[tuple[Point, str], uuid.UUID] = {}
    for _n in schematic.nets.values():
        for _ref in _n.pins:
            if _ref in pin_positions:
                _pt, _ = pin_positions[_ref]
                pin_pos_to_net[(_pt, "top")] = _n.id
                pin_pos_to_net[(_pt, "bottom")] = _n.id
    wire_to_net_id: dict[uuid.UUID, uuid.UUID | None] = {}
    for _w in working_wires:
        _resolved: uuid.UUID | None = None
        for _v in _w.path:
            _hit = pin_pos_to_net.get((_v, _w.side))
            if _hit is not None:
                _resolved = _hit
                break
        wire_to_net_id[_w.id] = _resolved

    # `same_net_pad_vertices` is computed lazily inside _route_segment_with_ripup
    # so it can refresh as working_wires grows during rip-up retries.

    # near_pin_pads: pads adjacent (on both layers) to pins that still need
    # routing.  The penalty discourages stealing corridors from other nets.
    #
    # A pin contributes to near_pin_pads only when:
    #   1. It belongs to a net (not floating) — floating pins have no pending
    #      route so no corridor will ever be needed near them.
    #   2. Its net is not yet fully connected in the current working graph —
    #      once a net is complete its pins no longer need reserved space.
    # Component IDs that already appear in this net — their other pins share
    # an enclosure with the route and must not block intra-component paths.
    net_component_ids: set[uuid.UUID] = {
        ref.component_id for ref in net.pins if ref in pin_positions
    }
    # Reverse map: (Point, side) → PinRef, for component_id lookup below.
    point_to_ref: dict[tuple[Point, str], PinRef] = {
        (pt, s): ref
        for ref, (pt, _pin_side) in pin_positions.items()
        for s in ("top", "bottom")
    }

    net_pts = {ps[0] for ps in net_ps}
    near_pin_pads: set[tuple[Point, str]] = set()
    pending_pin_pts: set[Point] = set()
    for (pin_pt, _pin_side), nid in occupied_pads.items():
        if nid == _FLOATING:
            continue
        # Skip pins from components already represented in this net — those
        # pins share an enclosure with the route and their 4-adjacent neighbours
        # inevitably lie on the only viable intra-component path (Panama Canal).
        candidate_ref = point_to_ref.get((pin_pt, _pin_side))
        if candidate_ref is not None and candidate_ref.component_id in net_component_ids:
            continue
        net_obj = schematic.nets.get(nid)
        if net_obj is None:
            continue
        refs_in_net = [r for r in net_obj.pins if r in pin_positions]
        if len(refs_in_net) < 2:
            continue
        first_node = current_graph.node_for_pin(refs_in_net[0])
        net_complete = first_node is not None and all(
            current_graph.node_for_pin(r) == first_node for r in refs_in_net
        )
        if not net_complete:
            pending_pin_pts.add(pin_pt)
    for pin_pt in pending_pin_pts:
        for dx, dy in ((1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0)):
            nb = Point(pin_pt.x + dx, pin_pt.y + dy)
            if nb not in pending_pin_pts and nb not in net_pts:
                near_pin_pads.add((nb, "top"))
                near_pin_pads.add((nb, "bottom"))

    cur_net_wire_points: set[tuple[Point, str]] = set()

    # Greedy nearest-neighbour Steiner tree.
    # Seed the tree with the pin that belongs to the closest pair — this avoids
    # starting from a skewed outlier and immediately forcing a long first wire.
    def _manhattan(ra: PinRef, rb: PinRef) -> float:
        pa, _ = pin_positions[ra]
        pb, _ = pin_positions[rb]
        return abs(pa.x - pb.x) + abs(pa.y - pb.y)

    best_seed = placed_refs[0]
    best_dist = float("inf")
    for i, ra in enumerate(placed_refs):
        for rb in placed_refs[i + 1:]:
            d = _manhattan(ra, rb)
            if d < best_dist:
                best_dist = d
                best_seed = ra

    seed_pt, seed_side = pin_positions[best_seed]
    connected_refs: list[PinRef] = [best_seed]
    connected_pts: list[tuple[Point, str]] = [(seed_pt, seed_side)]
    unconnected_refs: list[PinRef] = [r for r in placed_refs if r != best_seed]

    if _DEEP_DEBUG:
        _log.debug(
            "autoroute.net.seed",
            net=net.name,
            seed_pin=best_seed.pin_number,
            seed_cid=str(best_seed.component_id)[:8],
            seed_pos=f"({seed_pt.x},{seed_pt.y})",
            seed_side=seed_side,
            closest_pair_dist=best_dist,
        )

    seg_index = 0
    total_segments = len(placed_refs) - 1  # upper bound for progress
    while unconnected_refs:
        if cancel_flag is not None and cancel_flag.is_set():
            raise AutorouteCancelled
        if segment_callback is not None and total_segments > 0:
            segment_callback(net.name, seg_index, total_segments)
        # Absorb any refs already physically connected (e.g. adjacent component pins).
        still_unconnected: list[PinRef] = []
        for ref in unconnected_refs:
            target_node = current_graph.node_for_pin(ref)
            already = False
            if target_node is not None:
                for conn_ref in connected_refs:
                    if current_graph.node_for_pin(conn_ref) == target_node:
                        already = True
                        break
            if already:
                if _DEEP_DEBUG:
                    _log.debug(
                        "autoroute.net.already_connected",
                        net=net.name,
                        pin=ref.pin_number,
                        cid=str(ref.component_id)[:8],
                    )
                connected_refs.append(ref)
                rpt, rside = pin_positions[ref]
                connected_pts.append((rpt, rside))
            else:
                still_unconnected.append(ref)
        if not still_unconnected:
            break
        unconnected_refs = still_unconnected

        # Pick the closest unconnected pin to drive into the already-connected
        # set.  We route *from* the unconnected pin (source) *to* the connected
        # pin (target) so that A*'s target_set expands to the whole electric
        # set of the connected pin — any pad already on a previous wire of
        # this net is a valid endpoint.  Without that, A* would head for one
        # specific pad and might lay a parallel route through pads already
        # in the electric set, producing a redundant edge.
        best_src_pt: tuple[Point, str] = connected_pts[0]
        best_tgt_ref: PinRef = unconnected_refs[0]
        best_pair_dist = float("inf")
        for cpt, cside in connected_pts:
            for uref in unconnected_refs:
                upt, _ = pin_positions[uref]
                d = abs(upt.x - cpt.x) + abs(upt.y - cpt.y)
                if d < best_pair_dist:
                    best_pair_dist = d
                    best_src_pt = (cpt, cside)
                    best_tgt_ref = uref

        # Swap roles: A* runs from the *unconnected* pin (source) toward the
        # *connected* pin (target) so target_pads includes the whole electric
        # set of the connected pin.
        ref = best_tgt_ref
        unconnected_refs.remove(ref)
        target_pt = best_src_pt[0]
        unconnected_src_pos, unconnected_src_side = pin_positions[ref]
        best_src_pt = (unconnected_src_pos, unconnected_src_side)

        if _DEEP_DEBUG:
            _log.debug(
                "autoroute.net.segment",
                net=net.name,
                seg=seg_index,
                src_pos=f"({best_src_pt[0].x},{best_src_pt[0].y})",
                src_side=best_src_pt[1],
                tgt_pin=ref.pin_number,
                tgt_cid=str(ref.component_id)[:8],
                tgt_pos=f"({target_pt.x},{target_pt.y})",
                pair_dist=best_pair_dist,
            )

        seg_result = _route_segment_with_ripup(
            source_options=[best_src_pt],
            target=target_pt,
            manhattan_dist=best_pair_dist,
            board=board,
            layout=layout,
            working_wires=working_wires,
            additional_wires=new_wires,
            pass_new_wires=pass_new_wires,
            components=components,
            instances=instances,
            connectivity_graph=current_graph,
            side=side,
            cost_model=cost_model,
            max_rip_up_passes=max_rip_up_passes,
            wire_mode=wire_mode,
            max_detour_factor=max_detour_factor,
            occupied_pins=occupied_pads,
            same_net_pins=same_net_pads,
            near_pin_pads=near_pin_pads,
            insulated=insulated,
            plated_through_pads=plated_through_pads,
            cur_net_wire_points=cur_net_wire_points,
            under_component_penalty=under_component_penalty,
            preferred_side=preferred_side,
            push_and_shove=push_and_shove,
            wire_to_net_id=wire_to_net_id,
            active_net_id=net_id,
        )
        if seg_result is None:
            routed_wires = None
        else:
            routed_wires, _ss = seg_result
            _ns_nodes += _ss["nodes_expanded"]
            _ns_ob += _ss["over_budget_hits"]
            _ns_ripup += _ss["rip_up_attempts"]
            _ns_hops.append(_ss["path_hops"])
        if routed_wires is None:
            if _DEEP_DEBUG:
                _log.warning(
                    "autoroute.net.segment_failed",
                    net=net.name,
                    seg=seg_index,
                    src_pos=f"({best_src_pt[0].x},{best_src_pt[0].y})",
                    tgt_pos=f"({target_pt.x},{target_pt.y})",
                )
            return None
        if _DEEP_DEBUG:
            _log.debug(
                "autoroute.net.segment_ok",
                net=net.name,
                seg=seg_index,
                wires=[
                    {"side": w.side, "hops": len(w.path) - 1,
                     "from": f"({w.path[0].x},{w.path[0].y})",
                     "to": f"({w.path[-1].x},{w.path[-1].y})"}
                    for w in routed_wires
                ],
            )
        new_wires.extend(routed_wires)
        working_wires.extend(routed_wires)
        # Once a wire touches a plated-through pad it is electrically present
        # on the *other* layer too — so subsequent segments routing on that
        # other layer must treat the pad as already same-net and stop there
        # instead of laying a parallel path.  Without this `cur_net_wire_points`
        # would only see the wire's own side and `_truncate_at_same_net_wire`
        # could let a later route walk past it, producing a redundant edge in
        # the donor net's spanning tree.
        _OTHER = {"top": "bottom", "bottom": "top"}
        for w in routed_wires:
            for pt in w.path:
                cur_net_wire_points.add((pt, w.side))
                if pt in plated_through_pads:
                    cur_net_wire_points.add((pt, _OTHER[w.side]))
            # Incremental update — O(path_len) instead of O(full_graph_rebuild).
            current_graph.add_wire(w)
            # Live short-detection: after adding this wire, did any pin
            # in the schematic now share an electric set with a pin from
            # a different net?  Set PROTOBOARD_FAIL_ON_SHORT=1 to abort
            # autoroute the moment it happens; otherwise just warn loudly.
            if _SHORT_DETECTION_ENABLED:
                _check_no_net_fusion(
                    current_graph, schematic, net, w, fail=_FAIL_ON_SHORT,
                    seen_fusions=_SEEN_FUSIONS,
                )

        connected_refs.append(ref)
        rpt2, rside2 = pin_positions[ref]
        connected_pts.append((rpt2, rside2))
        seg_index += 1

    if _DEEP_DEBUG:
        _log.debug(
            "autoroute.net.done",
            net=net.name,
            new_wires=len(new_wires),
            total_hops=sum(len(w.path) - 1 for w in new_wires),
        )
    net_stats = NetStats(
        net_name=net.name,
        routed=True,
        nodes_expanded=_ns_nodes,
        over_budget_hits=_ns_ob,
        rip_up_attempts=_ns_ripup,
        path_hops_min=min(_ns_hops) if _ns_hops else 0,
        path_hops_max=max(_ns_hops) if _ns_hops else 0,
        path_hops_final=sum(_ns_hops),
        segments_found=len(new_wires),
    )
    return new_wires, net_stats


def _route_segment_with_ripup(
    source_options: list[tuple[Point, str]],
    target: Point,
    board: Board,
    layout: Layout,
    working_wires: list[Wire],
    additional_wires: list[Wire],
    components: dict[str, ComponentDefinition],
    instances: dict[uuid.UUID, ComponentInstance],
    connectivity_graph: ConnectivityGraph,
    side: Literal["top", "bottom"],
    cost_model: RoutingCostModel,
    max_rip_up_passes: int,
    occupied_pins: dict[tuple[Point, str], uuid.UUID],
    same_net_pins: dict[tuple[Point, str], uuid.UUID],
    near_pin_pads: set[tuple[Point, str]],
    insulated: bool,
    plated_through_pads: set[Point],
    cur_net_wire_points: set[tuple[Point, str]],
    under_component_penalty: dict[tuple[Point, str], float],
    preferred_side: Literal["top", "bottom"] = "bottom",
    wire_mode: Literal["bare", "insulated", "auto", "bare_diagonal"] = "insulated",
    max_detour_factor: float = 0.0,
    manhattan_dist: float = 0.0,
    push_and_shove: bool = True,
    pass_new_wires: list[Wire] | None = None,
    wire_to_net_id: dict[uuid.UUID, uuid.UUID | None] | None = None,
    active_net_id: uuid.UUID | None = None,
) -> tuple[list[Wire], dict[str, int]] | None:
    """Try to route *target* from any of *source_options*, with rip-up fallback.

    Returns (wires, seg_stats) or None.  seg_stats keys:
      nodes_expanded, over_budget_hits, rip_up_attempts, path_hops.
    """
    _seg_nodes = 0
    _seg_ob = 0
    # Build board pad set once for push-and-shove checks.
    _board_pad_set: set[Point] = {pos for _, pos in board.atoms()}
    # Track wires we rip up during this segment's attempts.  If we ultimately
    # fail to route, restore them — otherwise working_wires and
    # connectivity_graph diverge from the pass-level new_wires that still
    # holds these wires, and later nets will route through their pads
    # blind to the physical occupancy → silent net fusion.  The boolean
    # records whether the wire was donor-owned (in pass_new_wires) so the
    # restore can put it back in the right list.
    _ripped_wires: list[tuple[Wire, bool]] = []
    # Per-segment set of wire-ids that have already been mid-cell-rerouted
    # in this attempts loop.  Prevents two blockers from endlessly bouncing
    # each other when each reroute opens a corridor the other one wants.
    _already_rerouted: set[uuid.UUID] = set()
    # Pre-compute once whether *any* enhanced knob is active.  When all are
    # at defaults (zero), the corresponding indices are not consulted by
    # A*, so building them every attempt is wasted allocation pressure on
    # the worker thread — pressure that the project's known CPython 3.14
    # GC / Qt-paintEvent race turns into a silent segfault (see memory
    # [[project_silent_crash_post_upgrade]]).
    _enh_at_pad_active = (
        cost_model.at_pad_insulated_insulated_same_side != 0.0
        or cost_model.at_pad_insulated_bare_same_side != 0.0
    )
    _enh_parallel_active = cost_model.parallel_opposite_side != 0.0
    for attempt in range(max_rip_up_passes + 1):
        graph = connectivity_graph
        bare_segs = _build_bare_wire_segments(working_wires, wire_to_net_id)

        # Same-net pad vertices for the active net: every grid vertex of
        # every existing wire belonging to this net.  Drives find_paths'
        # extended target_set (the search may terminate at any of these
        # pads) and the teleport edges (zero-cost hops between same-net
        # vertices).  Rebuilt every attempt because working_wires can
        # grow during rip-up retries within this segment.
        same_net_pad_vertices: set[tuple[Point, str]] = set()
        if active_net_id is not None and wire_to_net_id is not None:
            for _w in working_wires:
                if wire_to_net_id.get(_w.id) != active_net_id:
                    continue
                for _v in _w.path:
                    same_net_pad_vertices.add((_v, _w.side))

        # Enhanced indices: built only when the corresponding cost knob
        # is non-zero.  ``find_paths`` accepts None and degrades to the
        # legacy hot-loop branch — same observable behaviour with zero
        # allocations on the default path.
        # Same-net reuse always needs at_pad_nets when any same-net wire
        # exists, but the heavy bitmask part is still elective.
        _needs_at_pad_nets = bool(same_net_pad_vertices)
        if _enh_at_pad_active or _needs_at_pad_nets:
            _bm_idx, _nets_idx = _build_at_pad_wire_index(working_wires, wire_to_net_id)
        else:
            _bm_idx, _nets_idx = None, None
        at_pad_idx = _bm_idx if _enh_at_pad_active else None
        at_pad_nets_idx = _nets_idx if _needs_at_pad_nets else None
        all_seg_idx = (
            _build_all_seg_index(working_wires, wire_to_net_id)
            if _enh_parallel_active else None
        )
        # Foreign-wire cell set: every (Point, side) where a wire belonging
        # to a *different* net (i.e. not active_net_id) has a vertex.  Used
        # by the ``wire_over_foreign_wire_pad`` penalty so the router pays
        # to thread through pads owned by other nets' wires.  Skipped when
        # the knob is zero — empty set then is harmless but unused.
        _wofp_cost = cost_model.get("wire_over_foreign_wire_pad", 0.0)
        _vfp_cost = cost_model.get("via_on_foreign_pad", 0.0)
        if (
            (_wofp_cost > 0.0 or _vfp_cost > 0.0)
            and active_net_id is not None
            and wire_to_net_id is not None
        ):
            foreign_wire_cells: set[tuple[Point, str]] = set()
            # ``foreign_via_pads``: cells where a foreign net has wires
            # ending on both top AND bottom — i.e. a plated-through via on
            # that pad belongs to the other net.  Detected by gathering
            # endpoints per (Point) per side and intersecting.
            from collections import defaultdict
            _foreign_endpoint_sides: dict[Point, set[str]] = defaultdict(set)
            for _w in working_wires:
                if wire_to_net_id.get(_w.id) == active_net_id:
                    continue
                if _wofp_cost > 0.0:
                    for _v in _w.path:
                        foreign_wire_cells.add((_v, _w.side))
                if _vfp_cost > 0.0 and _w.path:
                    _foreign_endpoint_sides[_w.path[0]].add(_w.side)
                    _foreign_endpoint_sides[_w.path[-1]].add(_w.side)
            foreign_via_pads: set[Point] = {
                pad
                for pad, sides in _foreign_endpoint_sides.items()
                if len(sides) >= 2
            }
        else:
            foreign_wire_cells = set()
            foreign_via_pads = set()
        # Active-net dominant side: among existing same-net wires, which
        # side (top/bottom) carries the majority?  Used by the
        # ``redundant_layer_change`` knob to gently suppress vias that
        # *leave* the dominant side — the "zebra suppression" half of
        # the cost knob.  Empty / equal → None, knob still applies as a
        # flat via surcharge.
        _rlc_cost = cost_model.get("redundant_layer_change", 0.0)
        active_dominant_side: str | None = None
        if _rlc_cost > 0.0 and active_net_id is not None and wire_to_net_id is not None:
            _top_n = 0
            _bot_n = 0
            for _w in working_wires:
                if wire_to_net_id.get(_w.id) != active_net_id:
                    continue
                if _w.side == "top":
                    _top_n += 1
                else:
                    _bot_n += 1
            if _top_n > _bot_n:
                active_dominant_side = "top"
            elif _bot_n > _top_n:
                active_dominant_side = "bottom"

        # Determine which insulation modes to attempt for this segment.
        # wire_mode="auto":          try bare first, fall back to insulated.
        # wire_mode="bare":          bare only, strict crossing ban.
        # wire_mode="bare_diagonal": bare only, parity-constrained diagonals
        #                            with perpendicular mesh-cell crossings
        #                            allowed at cost_model.crossing_bare_bare_diagonal.
        # wire_mode="insulated":     insulated only.
        import dataclasses as _dc
        if wire_mode == "bare" or wire_mode == "bare_diagonal":
            insulation_attempts = [False]
        elif wire_mode == "insulated":
            insulation_attempts = [True]
        else:  # "auto"
            insulation_attempts = [False, True]

        # For bare-wire attempt: forbid all crossings (bare×bare = short,
        # insulated×bare also not wanted on first try).  For bare_diagonal,
        # keep the cost_model as-is so its crossing_bare_bare_diagonal cost
        # and bare_diagonal_mode flag take effect.
        if wire_mode == "bare_diagonal":
            bare_only_cm = cost_model
        else:
            # Bare-first attempt: forbid every kind of insulated×bare and
            # bare×bare crossing by setting all 4 granular ib_* knobs to
            # inf alongside the bare×bare ban.
            bare_only_cm = cost_model.with_overrides(
                crossing_bare_bare=float("inf"),
                crossing_ib_at_pad_same_side=float("inf"),
                crossing_ib_at_pad_opposite_side=float("inf"),
                crossing_ib_off_pad_same_side=float("inf"),
                crossing_ib_off_pad_opposite_side=float("inf"),
            )

        best: tuple[float, list[PathStep]] | None = None
        best_is_preferred = False
        result_insulated: bool = insulation_attempts[-1]  # default to last attempt

        # Pre-compute the canonical keys of every pad belonging to the
        # net currently being routed (across both layers).  Any electric set
        # reachable from one of these pads is "own"; everything else is
        # alien — and an alien pad must block A*, otherwise a bare path that
        # merely touches the pad short-circuits to its alien net.
        own_canonicals: set[object] = set()
        for ps in same_net_pins:
            ck = connectivity_graph.node_for_pad(ps[0], ps[1])
            if ck is not None:
                own_canonicals.add(ck)

        # Discover one representative pad per alien electric set by querying
        # the graph for every other-net pin position.  Then expand each to
        # the full set of pads in that electric set.
        alien_pad_set: set[tuple[Point, str]] = set()
        seen_alien_canonicals: set[object] = set()
        for (pin_pt, pin_side) in occupied_pins:
            ck = connectivity_graph.node_for_pad(pin_pt, pin_side)
            if ck is None or ck in own_canonicals or ck in seen_alien_canonicals:
                continue
            seen_alien_canonicals.add(ck)
            for layer in ("top", "bottom"):
                for pad in connectivity_graph.pads_in_same_electric_set(pin_pt, layer):
                    alien_pad_set.add((pad, layer))

        for try_insulated in insulation_attempts:
            active_cm = bare_only_cm if not try_insulated else cost_model

            # Build pad-occupancy from existing wires.
            # - Insulated wire: blocks only its own side.
            # - Bare wire: blocks both sides.
            wire_occupied = _build_wire_occupied(
                working_wires,
                exclude_wire_id=None,
                both_sides=not try_insulated,
            )
            combined_occupied: dict[tuple[Point, str], uuid.UUID] = dict(occupied_pins)
            for ps, nid in wire_occupied.items():
                if ps not in combined_occupied:
                    combined_occupied[ps] = nid
            # Block every pad already part of an alien electric set, even when
            # the alien net has no component pin on this exact pad — the bare
            # wires that built that electric set still claim it electrically.
            _alien_sentinel = uuid.UUID("00000000-0000-0000-0000-000000000002")
            for ps in alien_pad_set:
                if ps not in combined_occupied and ps not in same_net_pins:
                    combined_occupied[ps] = _alien_sentinel
            for ps in same_net_pins:
                combined_occupied.pop(ps, None)
            effective_near = {
                ps for ps in near_pin_pads
                if ps not in combined_occupied and ps not in same_net_pins
            }

            sides_to_try = ["top", "bottom"] if plated_through_pads else [source_options[0][1]]

            def _astar_call(src_pt: Point, start_side: str, _ins: bool = try_insulated, _cm: RoutingCostModel = active_cm) -> tuple[str, list]:  # type: ignore[type-arg]
                target_pads = graph.pads_in_same_electric_set(target, start_side)
                return start_side, find_paths(
                    graph=graph,
                    source=src_pt,
                    targets=target_pads,
                    side=start_side,
                    cost_model=_cm,
                    k=1,
                    insulated=_ins,
                    occupied_pads=combined_occupied,
                    same_net_pads=same_net_pins,
                    near_pin_pads=effective_near,
                    near_pin_cost=10.0,
                    bare_wire_segments=bare_segs,
                    plated_through_pads=plated_through_pads,
                    penalised_pads=under_component_penalty,
                    at_pad_wires=at_pad_idx,
                    all_segments=all_seg_idx,
                    active_net_id=active_net_id,
                    at_pad_nets=at_pad_nets_idx,
                    same_net_pad_vertices=same_net_pad_vertices,
                    foreign_wire_cells=foreign_wire_cells,
                    foreign_via_pads=foreign_via_pads,
                    active_dominant_side=active_dominant_side,
                )

            attempt_best: tuple[float, list[PathStep]] | None = None
            attempt_best_is_preferred = False

            for src_pt, _src_side in source_options:
                if len(sides_to_try) > 1:
                    futures = {
                        _ASTAR_POOL.submit(_astar_call, src_pt, s): s
                        for s in sides_to_try
                    }
                    for fut in as_completed(futures):
                        start_side, results = fut.result()
                        if not results:
                            continue
                        _seg_nodes += results[0].nodes_expanded
                        _seg_ob += results[0].over_budget_hits
                        cost = results[0].cost
                        is_preferred = start_side == preferred_side
                        if (
                            attempt_best is None
                            or cost < attempt_best[0]
                            or (cost == attempt_best[0] and is_preferred and not attempt_best_is_preferred)
                        ):
                            attempt_best = (cost, results[0].path)
                            attempt_best_is_preferred = is_preferred
                else:
                    start_side = sides_to_try[0]
                    results = find_paths(
                        graph=graph,
                        source=src_pt,
                        targets=[target],
                        side=start_side,
                        cost_model=active_cm,
                        k=1,
                        insulated=try_insulated,
                        occupied_pads=combined_occupied,
                        same_net_pads=same_net_pins,
                        near_pin_pads=effective_near,
                        near_pin_cost=10.0,
                        bare_wire_segments=bare_segs,
                        plated_through_pads=plated_through_pads,
                        penalised_pads=under_component_penalty,
                        at_pad_wires=at_pad_idx,
                        all_segments=all_seg_idx,
                        active_net_id=active_net_id,
                        at_pad_nets=at_pad_nets_idx,
                        same_net_pad_vertices=same_net_pad_vertices,
                    )
                    if results:
                        _seg_nodes += results[0].nodes_expanded
                        _seg_ob += results[0].over_budget_hits
                        cost = results[0].cost
                        is_preferred = start_side == preferred_side
                        if (
                            attempt_best is None
                            or cost < attempt_best[0]
                            or (cost == attempt_best[0] and is_preferred and not attempt_best_is_preferred)
                        ):
                            attempt_best = (cost, results[0].path)
                            attempt_best_is_preferred = is_preferred

            # Apply max_detour_factor filter.
            if attempt_best is not None and max_detour_factor > 0.0 and manhattan_dist > 0.0:
                if len(attempt_best[1]) > manhattan_dist * max_detour_factor:
                    attempt_best = None  # path too long — try next insulation mode

            if attempt_best is not None:
                best = attempt_best
                best_is_preferred = attempt_best_is_preferred
                result_insulated = try_insulated
                break  # found a valid path — no need for fallback

        if best is not None:
            # Truncation only makes sense when the target is already
            # electrically connected to the wire-net being grown — i.e. the
            # net has at least one prior wire and the target pad sits in
            # that wire's electric set.  Otherwise an A* path that merely
            # *passes through* the existing wire on its way to a still-
            # disconnected target would be truncated to that crossing pad,
            # leaving the real target unrouted while the surrounding code
            # marks the net "done".
            target_set = connectivity_graph.pads_in_same_electric_set(target, side)
            target_already_connected = len(target_set) > 1
            if target_already_connected:
                path = _truncate_at_same_net_wire(best[1], cur_net_wire_points, target)
            else:
                path = best[1]
            wires = _path_to_wires(path, result_insulated)
            if _DEEP_DEBUG:
                _log.debug(
                    "autoroute.astar.ok",
                    attempt=attempt,
                    cost=round(best[0], 2),
                    path_pts=len(best[1]),
                    truncated=len(path) < len(best[1]),
                    result_wires=[
                        {"side": w.side, "hops": len(w.path) - 1,
                         "from": f"({w.path[0].x},{w.path[0].y})",
                         "to": f"({w.path[-1].x},{w.path[-1].y})"}
                        for w in wires
                    ],
                )
            seg_stats = {
                "nodes_expanded": _seg_nodes,
                "over_budget_hits": _seg_ob,
                "rip_up_attempts": attempt,
                "path_hops": sum(len(w.path) - 1 for w in wires),
            }
            return wires, seg_stats

        if _DEEP_DEBUG:
            _log.debug(
                "autoroute.astar.no_path",
                attempt=attempt,
                src=f"({source_options[0][0].x},{source_options[0][0].y})",
                tgt=f"({target.x},{target.y})",
                blocked_pads=len(combined_occupied),
                near_pads=len(effective_near),
            )

        if attempt >= max_rip_up_passes:
            break

        nearest_source = min(
            source_options,
            key=lambda ps: abs(ps[0].x - target.x) + abs(ps[0].y - target.y),
        )

        # Strategy 1 (mid-cell preferred reroute) — only fires when the
        # enhanced router has set ``max_reroute_factor > 0``.  Default
        # (0) makes this a no-op so the legacy ``astar`` router
        # behaviour is unchanged.
        pushed = None
        if push_and_shove and cost_model.max_reroute_factor > 0.0:
            mid_cell = _try_mid_cell_reroute(
                nearest_source[0], target,
                working_wires, additional_wires,
                connectivity_graph,
                cost_model,
                occupied_pins,
                same_net_pins,
                plated_through_pads,
                under_component_penalty,
                _already_rerouted,
                wire_to_net_id=wire_to_net_id,
            )
            if mid_cell is not None:
                _already_rerouted.add(mid_cell[0].id)
                pushed = mid_cell

        # Strategy 2 (classic axis-aligned nudge) — fires when Strategy
        # 1 did nothing.  ``push_and_shove`` here is the user's old
        # global toggle and gates the whole feature.
        if pushed is None and push_and_shove:
            pushed = _try_push_wire(
                nearest_source[0], target, working_wires, additional_wires,
                board, occupied_pins, _board_pads=_board_pad_set,
            )
        if pushed is not None:
            old_wire, new_wire = pushed
            working_wires.remove(old_wire)
            connectivity_graph.remove_wire(old_wire)
            working_wires.append(new_wire)
            connectivity_graph.add_wire(new_wire)
            # If push-and-shove nudged a wire that belonged to an earlier
            # net in this pass, update pass_new_wires so the donor net's
            # result reflects the new path rather than the stale old one.
            if pass_new_wires is not None and old_wire in pass_new_wires:
                pass_new_wires.remove(old_wire)
                pass_new_wires.append(new_wire)
            if _DEEP_DEBUG:
                _log.debug(
                    "autoroute.push_and_shove",
                    attempt=attempt,
                    pushed_wire_id=str(old_wire.id)[:8],
                    new_wire_id=str(new_wire.id)[:8],
                )
            continue  # retry A* with the nudged wire in place

        ripped = _find_wire_to_rip(
            working_wires, nearest_source[0], target, additional_wires
        )
        if ripped is None:
            if _DEEP_DEBUG:
                _log.debug("autoroute.ripup.nothing_to_rip",
                           src=f"({nearest_source[0].x},{nearest_source[0].y})",
                           tgt=f"({target.x},{target.y})")
            break
        if _DEEP_DEBUG:
            _log.warning(
                "autoroute.ripup",
                attempt=attempt,
                ripped_wire_id=str(ripped.id)[:8],
                ripped_side=ripped.side,
                ripped_from=f"({ripped.path[0].x},{ripped.path[0].y})",
                ripped_to=f"({ripped.path[-1].x},{ripped.path[-1].y})",
            )
        working_wires.remove(ripped)
        connectivity_graph.remove_wire(ripped)
        # If the ripped wire was routed by an earlier net in this pass it
        # still sits in pass_new_wires — remove it there too, otherwise the
        # donor net keeps a wire in its final result that no longer exists
        # in working_wires/connectivity_graph.  That manifested in real
        # projects as duplicate routes: two independent paths between the
        # same pin pair, one stale and one freshly laid through a different
        # corridor.  We track whether the wire was donor-owned so the
        # failure restore knows where to put it back.
        ripped_from_pass = (
            pass_new_wires is not None and ripped in pass_new_wires
        )
        if ripped_from_pass:
            pass_new_wires.remove(ripped)  # type: ignore[union-attr]
        _ripped_wires.append((ripped, ripped_from_pass))

    # Routing ultimately failed.  Restore any wires we ripped during the
    # attempts so callers (and later nets) see a consistent state.
    for w, from_pass in _ripped_wires:
        working_wires.append(w)
        connectivity_graph.add_wire(w)
        if from_pass and pass_new_wires is not None:
            pass_new_wires.append(w)
    return None


def _truncate_at_same_net_wire(
    path: list[PathStep],
    cur_net_wire_points: set[tuple[Point, str]],
    target: Point,
) -> list[PathStep]:
    """Truncate path at the first intermediate point that lies on a same-net wire.

    The start point (index 0) is never a truncation point — it is already
    connected.  We skip it and look from index 1 onwards.  If we find a
    same-net wire point before the explicit target, we stop there — no need
    to extend further since connectivity is already established.
    """
    if len(path) <= 2:
        return path
    for i in range(1, len(path) - 1):
        pt, s = path[i]
        if (pt, s) in cur_net_wire_points:
            return path[: i + 1]
    return path


def _build_under_component_penalty(
    board: Board,
    layout: Layout,
    components: dict[str, ComponentDefinition],
    instances: dict[uuid.UUID, ComponentInstance],
    penalty: float,
) -> dict[tuple[Point, str], float]:
    """Return a penalty map for top-layer pads that lie under a component body.

    For each placed component, compute its bounding box and mark every board
    pad that falls strictly inside that box (on the top layer) with *penalty*.
    Pin positions themselves are already handled by the occupied/same-net maps,
    so we skip them here — the penalty applies only to the interior pads between
    the pins.
    """
    if penalty <= 0.0:
        return {}

    result: dict[tuple[Point, str], float] = {}
    for comp_id, placement in layout.placements.items():
        inst = instances.get(comp_id)
        if inst is None:
            continue
        defn = components.get(inst.definition_ref)
        if defn is None:
            continue
        if placement.side != "top":
            # Bottom-mounted components penalise bottom pads instead.
            # (Rare case — skip for now, extend if needed.)
            continue
        bbox = component_bbox(defn.footprint, placement)
        # Iterate board atoms and mark those inside the bbox on the top layer.
        for atom, pos in board.atoms():
            if isinstance(atom, (PadAtom, StripAtom)) and bbox.contains(pos):
                key = (pos, "top")
                # Take the maximum penalty if multiple components overlap.
                existing = result.get(key, 0.0)
                if penalty > existing:
                    result[key] = penalty
    return result


def _path_to_wires(path: list[PathStep], insulated: bool) -> list[Wire]:
    """Convert a (possibly multi-layer) path into one Wire per layer segment."""
    segments = split_path_by_side(path)
    wires: list[Wire] = []
    for pts, seg_side in segments:
        if len(pts) < 2:
            continue
        wires.append(Wire(
            id=uuid.uuid4(),
            path=pts,
            insulated=insulated,
            side=seg_side,
        ))
    return wires


def _collect_wire_points(
    wires: list[Wire],
    net_id: uuid.UUID | None,
    net_wires: set[uuid.UUID] | None,
) -> set[tuple[Point, str]]:
    """Collect all path points from *wires* as (Point, side) pairs."""
    pts: set[tuple[Point, str]] = set()
    for w in wires:
        pts.update((p, w.side) for p in w.path)
    return pts


def _layout_with_wires(layout: Layout, wires: list[Wire]) -> Layout:
    """Return a Layout view that uses *wires* instead of layout.wires."""
    from dataclasses import replace
    return replace(layout, wires=wires)


def _build_wire_occupied(
    wires: list[Wire],
    exclude_wire_id: uuid.UUID | None,
    both_sides: bool = False,
) -> dict[tuple[Point, str], uuid.UUID]:
    """Map every grid point covered by existing wires → (Point, side) → sentinel UUID.

    Parameters
    ----------
    both_sides:
        Hint used for the *insulated* portion of the wire list: when True the
        caller intends a bare-on-bare conflict check and wants every wire to
        block both layers.  Bare wires always block both layers regardless
        (they share an electric set with the opposite layer through the
        plated-through hole), so this flag mostly matters for insulated wires.
    """
    occupied: dict[tuple[Point, str], uuid.UUID] = {}
    _sentinel = uuid.UUID("00000000-0000-0000-0000-000000000001")
    for wire in wires:
        if wire.id == exclude_wire_id:
            continue
        # A bare wire's metal sits inside a plated-through hole — its pad is
        # electrically continuous with the opposite layer.  Any new route on
        # the opposite layer that lands here would short to this wire's net,
        # so bare wires must block *both* sides unconditionally.
        if not wire.insulated:
            sides: tuple[str, ...] = ("top", "bottom")
        else:
            sides = ("top", "bottom") if both_sides else (wire.side,)
        for i in range(len(wire.path) - 1):
            p1 = wire.path[i]
            p2 = wire.path[i + 1]
            for pt in (p1, p2):
                for s in sides:
                    occupied[(pt, s)] = _sentinel
            if p1.x == p2.x:
                step = 1.0 if p2.y > p1.y else -1.0
                y = p1.y + step
                while (step > 0 and y < p2.y) or (step < 0 and y > p2.y):
                    for s in sides:
                        occupied[(Point(p1.x, y), s)] = _sentinel
                    y += step
            elif p1.y == p2.y:
                step = 1.0 if p2.x > p1.x else -1.0
                x = p1.x + step
                while (step > 0 and x < p2.x) or (step < 0 and x > p2.x):
                    for s in sides:
                        occupied[(Point(x, p1.y), s)] = _sentinel
                    x += step
    return occupied


def _build_occupied_map(
    wires: list[Wire],
    exclude_wire_id: uuid.UUID | None,
) -> dict[Point, uuid.UUID]:
    """Map every grid point covered by existing wires → sentinel UUID.

    Legacy interface (Point-keyed, side-unaware).  Used only internally by
    the rip-up logic that doesn't need per-side resolution.
    """
    occupied: dict[Point, uuid.UUID] = {}
    _sentinel = uuid.UUID("00000000-0000-0000-0000-000000000001")
    for wire in wires:
        if wire.id == exclude_wire_id:
            continue
        for i in range(len(wire.path) - 1):
            p1 = wire.path[i]
            p2 = wire.path[i + 1]
            occupied[p1] = _sentinel
            occupied[p2] = _sentinel
            if p1.x == p2.x:
                step = 1.0 if p2.y > p1.y else -1.0
                y = p1.y + step
                while (step > 0 and y < p2.y) or (step < 0 and y > p2.y):
                    occupied[Point(p1.x, y)] = _sentinel
                    y += step
            elif p1.y == p2.y:
                step = 1.0 if p2.x > p1.x else -1.0
                x = p1.x + step
                while (step > 0 and x < p2.x) or (step < 0 and x > p2.x):
                    occupied[Point(x, p1.y)] = _sentinel
                    x += step
    return occupied


def _build_bare_wire_segments(
    wires: list[Wire],
    wire_to_net_id: dict[uuid.UUID, uuid.UUID | None] | None = None,
) -> set[tuple[Point, Point, str, uuid.UUID | None]]:
    """Return the set of (p1, p2, side, net_id) tuples for all *bare* (non-insulated) wires.

    ``net_id`` is looked up via ``wire_to_net_id`` (None when the wire
    is not associated with any electrical net).  Same-net reuse logic
    in the A* core consults this field.
    """
    segs: set[tuple[Point, Point, str, uuid.UUID | None]] = set()
    for wire in wires:
        if wire.insulated:
            continue
        nid = wire_to_net_id.get(wire.id) if wire_to_net_id else None
        for i in range(len(wire.path) - 1):
            p1, p2 = wire.path[i], wire.path[i + 1]
            if (p1.x, p1.y) <= (p2.x, p2.y):
                segs.add((p1, p2, wire.side, nid))
            else:
                segs.add((p2, p1, wire.side, nid))
    return segs


def _build_all_seg_index(
    wires: list[Wire],
    wire_to_net_id: dict[uuid.UUID, uuid.UUID | None] | None = None,
) -> list[tuple[Point, Point, str, bool, uuid.UUID | None]]:
    """All wire segments as ``(p1, p2, side, is_insulated, net_id)`` tuples.

    Includes both bare and insulated wires.  Used by the enhanced router
    to detect parallel-on-opposite-side overlaps and by same-net reuse
    to skip penalties when the overlap belongs to the active net.  Segment
    orientation is normalised (smaller endpoint first) so duplicates are
    deduped trivially by callers.
    """
    out: list[tuple[Point, Point, str, bool, uuid.UUID | None]] = []
    seen: set[tuple[Point, Point, str, bool]] = set()
    for wire in wires:
        nid = wire_to_net_id.get(wire.id) if wire_to_net_id else None
        for i in range(len(wire.path) - 1):
            p1, p2 = wire.path[i], wire.path[i + 1]
            if (p1.x, p1.y) > (p2.x, p2.y):
                p1, p2 = p2, p1
            key = (p1, p2, wire.side, wire.insulated)
            if key in seen:
                continue
            seen.add(key)
            out.append((p1, p2, wire.side, wire.insulated, nid))
    return out


def _build_at_pad_wire_index(
    wires: list[Wire],
    wire_to_net_id: dict[uuid.UUID, uuid.UUID | None] | None = None,
) -> tuple[
    dict[tuple[Point, str], int],
    dict[tuple[Point, str], frozenset[uuid.UUID]],
]:
    """Per ``(pad, side)`` bitmask + per-pad set of net_ids whose wires touch.

    Returns ``(bitmask_index, net_ids_index)``.

    Bitmask bits: ``1`` = at least one insulated wire's vertex is on this
    pad on this side; ``2`` = at least one bare wire's vertex is on this
    pad on this side.  Both bits set means a mixed-type contact already
    exists.  Used by :class:`EnhancedAStarRouter` to penalise "two wires
    meeting at the same pad".

    The ``net_ids_index`` carries the set of net_ids whose wire-vertices
    land on each pad — used by same-net reuse to bypass the at_pad
    penalty (and the at_pad_bare_short reject) when the active net
    already has metal on this pad.
    """
    bm_index: dict[tuple[Point, str], int] = {}
    net_index: dict[tuple[Point, str], frozenset[uuid.UUID]] = {}
    nets_accum: dict[tuple[Point, str], set[uuid.UUID]] = {}
    for wire in wires:
        bit = 1 if wire.insulated else 2
        nid = wire_to_net_id.get(wire.id) if wire_to_net_id else None
        for vertex in wire.path:
            key = (vertex, wire.side)
            bm_index[key] = bm_index.get(key, 0) | bit
            if nid is not None:
                bucket = nets_accum.setdefault(key, set())
                bucket.add(nid)
    for key, nids in nets_accum.items():
        net_index[key] = frozenset(nids)
    return bm_index, net_index


def _nudge_wire(
    wire: Wire,
    dx: float,
    dy: float,
    board_pads: set[Point],
    working_wires: list[Wire],
    occupied_pins: dict[tuple[Point, str], uuid.UUID],
) -> Wire | None:
    """Try to shift *wire* by (dx, dy).

    Returns the shifted Wire if all new path points are free board pads
    and unoccupied by component pins or other wires, else None.
    """
    new_path = [Point(p.x + dx, p.y + dy) for p in wire.path]

    # All new points must be valid board pads.
    for pt in new_path:
        if pt not in board_pads:
            return None

    # No collision with component pins.
    other_side = "top" if wire.side == "bottom" else "bottom"
    for pt in new_path:
        if (pt, wire.side) in occupied_pins or (pt, other_side) in occupied_pins:
            return None

    # No collision with other existing wires.
    other_wire_occupied = _build_wire_occupied(
        [w for w in working_wires if w.id != wire.id],
        exclude_wire_id=None,
        both_sides=not wire.insulated,
    )
    for pt in new_path:
        if (pt, wire.side) in other_wire_occupied:
            return None

    from typing import cast, Literal as _Lit
    # Preserve wire id — nudge is a translation of the same logical wire.
    return Wire(
        id=wire.id,
        path=new_path,
        insulated=wire.insulated,
        side=cast(_Lit["top", "bottom"], wire.side),
        color=wire.color,
    )


def _try_mid_cell_reroute(
    source: Point,
    target: Point,
    working_wires: list[Wire],
    additional_wires: list[Wire],
    connectivity_graph: "ConnectivityGraph",
    cost_model: RoutingCostModel,
    occupied_pins: dict[tuple[Point, str], uuid.UUID],
    same_net_pins: dict[tuple[Point, str], uuid.UUID],
    plated_through_pads: set[Point] | None,
    under_component_penalty: dict[tuple[Point, str], float],
    already_rerouted: set[uuid.UUID],
    wire_to_net_id: dict[uuid.UUID, uuid.UUID | None] | None = None,
) -> tuple[Wire, Wire] | None:
    """Mid-cell-preferred push-and-shove (Strategy 1).

    Picks a blocking wire that runs through the source↔target corridor
    and reruns A* for it under ``bare_diagonal_mode=True`` plus the
    *opposite* parity family from the blocker's own.  This biases any
    unavoidable crossings to fall at mesh-cell centres (integer (x,y)
    points between pads) rather than landing on a pad — the same
    electrical-safety mechanism used by the bare-diagonal wire mode.

    Caller responsibility: enable this strategy by setting
    ``cost_model.max_reroute_factor > 0``.  Returns ``None`` when the
    feature is disabled, no suitable blocker exists, or the rerouted
    cost exceeds ``original_cost * max_reroute_factor``.

    ``already_rerouted`` accumulates wire IDs that have been touched by
    this strategy in the current pass — preventing infinite re-bounce
    when two wires keep stealing each other's corridor.
    """
    factor = cost_model.max_reroute_factor
    if factor <= 0.0:
        return None
    import dataclasses as _dc

    protected_ids = {w.id for w in additional_wires}
    min_x = min(source.x, target.x) - 0.5
    max_x = max(source.x, target.x) + 0.5
    min_y = min(source.y, target.y) - 0.5
    max_y = max(source.y, target.y) + 0.5

    # Adjusted cost model: force bare-diagonal exploration and flip the
    # parity family by making the bare×bare-diagonal cost cheap.
    reroute_cm = cost_model.with_overrides(
        bare_diagonal_mode=True,
        crossing_bare_bare=float("inf"),
        crossing_bare_bare_diagonal=max(
            cost_model.crossing_bare_bare_diagonal, 0.5
        ) if cost_model.crossing_bare_bare_diagonal != float("inf") else 0.5,
    )

    for wire in working_wires:
        if wire.id in protected_ids or wire.id in already_rerouted:
            continue
        if len(wire.path) < 2:
            continue
        # Wire must cross the requested source↔target bbox.
        if not any(
            min_x <= pt.x <= max_x and min_y <= pt.y <= max_y
            for pt in wire.path
        ):
            continue

        w_src, w_tgt = wire.path[0], wire.path[-1]
        # Estimate the original wire's "cost" as Chebyshev path length
        # — exact A* cost is not tracked on Wire, but the
        # Chebyshev count is a tight lower bound that matches the cost
        # of an optimal straight A* run with free_pad=1.0.
        original_cost = sum(
            max(
                abs(wire.path[i + 1].x - wire.path[i].x),
                abs(wire.path[i + 1].y - wire.path[i].y),
            )
            for i in range(len(wire.path) - 1)
        )
        budget = original_cost * factor

        # Remove the wire from the graph temporarily so the rerouter
        # doesn't trip over it.
        connectivity_graph.remove_wire(wire)
        try:
            # Derive active net for the rerouted wire from wire_to_net_id;
            # may be None (floating, or caller didn't provide the map) —
            # find_paths tolerates a missing active_net_id (no discount).
            _reroute_net_id = (
                wire_to_net_id.get(wire.id) if wire_to_net_id else None
            )
            # Same-net pad vertices = vertices of *other* wires of this net.
            _reroute_pv: set[tuple[Point, str]] = set()
            if _reroute_net_id is not None and wire_to_net_id is not None:
                for _ow in working_wires:
                    if _ow.id == wire.id:
                        continue
                    if wire_to_net_id.get(_ow.id) != _reroute_net_id:
                        continue
                    for _v in _ow.path:
                        _reroute_pv.add((_v, _ow.side))
            results = find_paths(
                graph=connectivity_graph,
                source=w_src,
                targets=[w_tgt],
                side=wire.side,
                cost_model=reroute_cm,
                k=1,
                insulated=wire.insulated,
                occupied_pads=dict(occupied_pins),
                same_net_pads=dict(same_net_pins),
                near_pin_pads=set(),
                near_pin_cost=10.0,
                bare_wire_segments=_build_bare_wire_segments(
                    [w for w in working_wires if w.id != wire.id],
                    wire_to_net_id,
                ),
                plated_through_pads=plated_through_pads,
                penalised_pads=under_component_penalty,
                active_net_id=_reroute_net_id,
                same_net_pad_vertices=_reroute_pv,
            )
        finally:
            connectivity_graph.add_wire(wire)

        if not results:
            continue
        best = results[0]
        if best.cost > budget:
            continue
        # Same path (or essentially same) — nothing gained.
        new_path = [p for p, _s in best.path]
        if new_path == list(wire.path):
            continue
        # Defensive: A* may return a 1-point trivial path when source is
        # already in target_set (e.g. via extended same-net targets).
        # A Wire requires ≥ 2 points; skip these.
        if len(new_path) < 2:
            continue
        # Preserve wire id — mid-cell reroute is an alternative path for
        # the same logical wire.
        new_wire = Wire(
            id=wire.id,
            path=new_path,
            insulated=wire.insulated,
            side=wire.side,
            color=wire.color,
        )
        return wire, new_wire

    return None


def _try_push_wire(
    source: Point,
    target: Point,
    working_wires: list[Wire],
    additional_wires: list[Wire],
    board: Board,
    occupied_pins: dict[tuple[Point, str], uuid.UUID],
    _board_pads: set[Point] | None = None,
) -> tuple[Wire, Wire] | None:
    """Try to nudge a blocking wire sideways to open a corridor for routing.

    Finds the first wire (not in *additional_wires*) whose path passes through
    the bounding box between *source* and *target*, and that is axis-aligned
    (horizontal or vertical).  Tries shifting it by ±1 in the perpendicular
    direction.

    Returns (old_wire, new_wire) if a push is possible, else None.
    """
    board_pads: set[Point] = _board_pads if _board_pads is not None else {
        pos for _, pos in board.atoms()
    }
    protected_ids = {w.id for w in additional_wires}
    min_x = min(source.x, target.x) - 0.5
    max_x = max(source.x, target.x) + 0.5
    min_y = min(source.y, target.y) - 0.5
    max_y = max(source.y, target.y) + 0.5

    for wire in working_wires:
        if wire.id in protected_ids:
            continue
        if len(wire.path) < 2:
            continue

        # Only push axis-aligned wires (diagonal push is complex and rare).
        first, last = wire.path[0], wire.path[-1]
        is_horizontal = first.y == last.y and all(p.y == first.y for p in wire.path)
        is_vertical = first.x == last.x and all(p.x == first.x for p in wire.path)
        if not (is_horizontal or is_vertical):
            continue

        # Wire must pass through the corridor region.
        if not any(
            min_x <= pt.x <= max_x and min_y <= pt.y <= max_y
            for pt in wire.path
        ):
            continue

        # Try perpendicular nudges of ±1 raster unit.
        nudges: list[tuple[float, float]]
        if is_horizontal:
            nudges = [(0.0, 1.0), (0.0, -1.0)]
        else:
            nudges = [(1.0, 0.0), (-1.0, 0.0)]

        for dx, dy in nudges:
            pushed = _nudge_wire(wire, dx, dy, board_pads, working_wires, occupied_pins)
            if pushed is not None:
                return wire, pushed

    return None


def _find_wire_to_rip(
    wires: list[Wire],
    source: Point,
    target: Point,
    protected: list[Wire],
) -> Wire | None:
    """Find a wire in *wires* (not in *protected*) that lies roughly between source and target."""
    protected_ids = {w.id for w in protected}
    min_x = min(source.x, target.x) - 1
    max_x = max(source.x, target.x) + 1
    min_y = min(source.y, target.y) - 1
    max_y = max(source.y, target.y) + 1

    for wire in wires:
        if wire.id in protected_ids:
            continue
        for pt in wire.path:
            if min_x <= pt.x <= max_x and min_y <= pt.y <= max_y:
                return wire
    return None
