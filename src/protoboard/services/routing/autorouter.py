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
3. Return a list of new WireSegment objects (caller adds them to layout).
4. Return a list of nets that could not be routed (unrouted report).

Multi-layer routing:
  On perfboard (plated-through holes), the router may switch layers at any
  pad.  Each layer change costs `cost_model.layer_change`.  The resulting
  path is split into per-layer WireSegment objects.
"""

from __future__ import annotations

import threading
import csv
import datetime
import io
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
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
from protoboard.domain.layout.wire import WireSegment
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from protoboard.services.routing.astar import PathStep, find_paths, split_path_by_side
from protoboard.services.routing.cost_model import RoutingCostModel

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

# Module-level thread pool for parallel A* side evaluation.
_ASTAR_POOL = ThreadPoolExecutor(max_workers=_CPU_COUNT, thread_name_prefix="astar")
_atexit.register(lambda: _ASTAR_POOL.shutdown(wait=False, cancel_futures=True))


class AutorouteCancelled(Exception):
    """Raised when the cancel_flag is set during routing."""


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

    wires: list[WireSegment] = field(default_factory=list)
    unrouted_nets: list[str] = field(default_factory=list)  # net names
    pass_stats: list[PassStats] = field(default_factory=list)


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

    # Build pin-position lookup: PinRef → (world_position, side).
    pin_positions = _build_pin_positions(layout, components, instances)

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

    # Snapshot of user-placed wires — never mutated; each pass starts from here.
    user_wires: list[WireSegment] = list(layout.wires)

    _log.info(
        "autoroute.start",
        nets=len(sorted_nets),
        existing_wires=len(user_wires),
        allow_layer_change=allow_layer_change,
        max_optimize_iterations=max_optimize_iterations,
        net_order=[n.name for n in sorted_nets],
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
            with ProcessPoolExecutor(max_workers=n_parallel) as pool:
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
                        pool.shutdown(wait=False, cancel_futures=True)
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


def _run_opt_pass_in_process(args: tuple) -> tuple[list[WireSegment], list[str]]:  # type: ignore[type-arg]
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
    wires: list[WireSegment],
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
    user_wires: list[WireSegment],
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
) -> tuple[list[WireSegment], list[str], PassStats]:
    """Run one full greedy routing pass over *sorted_nets*.

    Starts from *user_wires* (the original user-placed wires only) and appends
    newly routed wires as each net is processed.  Nets whose id is in
    *skip_net_ids* are skipped and recorded as unrouted without attempting A*.

    Returns ``(new_wires, unrouted_net_names, pass_stats)``.
    """
    working_wires: list[WireSegment] = list(user_wires)
    new_wires: list[WireSegment] = []
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
    working_wires: list[WireSegment],
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
) -> tuple[list[WireSegment], NetStats] | None:
    """Route all connections for *net*.  Returns (wires, stats) or None on failure."""
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

    new_wires: list[WireSegment] = []
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

        # Pick the (source, target) pair with minimum manhattan distance across
        # all connected×unconnected combinations.  Pass only that specific source
        # to A* — not the full connected_pts list — so the router is forced to
        # route the geometrically closest segment rather than sneaking through an
        # already-routed wire to reach a distant target from a different origin.
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

        ref = best_tgt_ref
        unconnected_refs.remove(ref)
        target_pt, _ = pin_positions[ref]

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
        for w in routed_wires:
            for pt in w.path:
                cur_net_wire_points.add((pt, w.side))
            # Incremental update — O(path_len) instead of O(full_graph_rebuild).
            current_graph.add_wire(w)

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
    working_wires: list[WireSegment],
    additional_wires: list[WireSegment],
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
) -> tuple[list[WireSegment], dict[str, int]] | None:
    """Try to route *target* from any of *source_options*, with rip-up fallback.

    Returns (wires, seg_stats) or None.  seg_stats keys:
      nodes_expanded, over_budget_hits, rip_up_attempts, path_hops.
    """
    _seg_nodes = 0
    _seg_ob = 0
    # Build board pad set once for push-and-shove checks.
    _board_pad_set: set[Point] = {pos for _, pos in board.atoms()}
    for attempt in range(max_rip_up_passes + 1):
        graph = connectivity_graph
        bare_segs = _build_bare_wire_segments(working_wires)

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
            bare_only_cm = _dc.replace(
                cost_model,
                crossing_bare_bare=float("inf"),
                crossing_insulated_bare=float("inf"),
            )

        best: tuple[float, list[PathStep]] | None = None
        best_is_preferred = False
        result_insulated: bool = insulation_attempts[-1]  # default to last attempt

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
            path = _truncate_at_same_net_wire(best[1], cur_net_wire_points, target)
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

        # Push-and-shove: try nudging a blocking wire sideways before rip-up.
        pushed = _try_push_wire(
            nearest_source[0], target, working_wires, additional_wires,
            board, occupied_pins, _board_pads=_board_pad_set,
        ) if push_and_shove else None
        if pushed is not None:
            old_wire, new_wire = pushed
            working_wires.remove(old_wire)
            connectivity_graph.remove_wire(old_wire)
            working_wires.append(new_wire)
            connectivity_graph.add_wire(new_wire)
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


def _path_to_wires(path: list[PathStep], insulated: bool) -> list[WireSegment]:
    """Convert a (possibly multi-layer) path into one WireSegment per layer segment."""
    segments = split_path_by_side(path)
    wires: list[WireSegment] = []
    for pts, seg_side in segments:
        if len(pts) < 2:
            continue
        wires.append(WireSegment(
            id=uuid.uuid4(),
            path=pts,
            insulated=insulated,
            side=seg_side,
        ))
    return wires


def _collect_wire_points(
    wires: list[WireSegment],
    net_id: uuid.UUID | None,
    net_wires: set[uuid.UUID] | None,
) -> set[tuple[Point, str]]:
    """Collect all path points from *wires* as (Point, side) pairs."""
    pts: set[tuple[Point, str]] = set()
    for w in wires:
        pts.update((p, w.side) for p in w.path)
    return pts


def _layout_with_wires(layout: Layout, wires: list[WireSegment]) -> Layout:
    """Return a Layout view that uses *wires* instead of layout.wires."""
    from dataclasses import replace
    return replace(layout, wires=wires)


def _build_wire_occupied(
    wires: list[WireSegment],
    exclude_wire_id: uuid.UUID | None,
    both_sides: bool = False,
) -> dict[tuple[Point, str], uuid.UUID]:
    """Map every grid point covered by existing wires → (Point, side) → sentinel UUID.

    Parameters
    ----------
    both_sides:
        When True, each wire point blocks *both* ``"top"`` and ``"bottom"``
        (used for bare wires, which are bare metal in a plated-through hole).
        When False (default), each wire point blocks only ``wire.side``
        (used for insulated wires, which do not electrically contact the
        opposite layer).
    """
    occupied: dict[tuple[Point, str], uuid.UUID] = {}
    _sentinel = uuid.UUID("00000000-0000-0000-0000-000000000001")
    for wire in wires:
        if wire.id == exclude_wire_id:
            continue
        sides: tuple[str, ...] = ("top", "bottom") if both_sides else (wire.side,)
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
    wires: list[WireSegment],
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
    wires: list[WireSegment],
) -> set[tuple[Point, Point, str]]:
    """Return the set of (p1, p2, side) triples for all *bare* (non-insulated) wires."""
    segs: set[tuple[Point, Point, str]] = set()
    for wire in wires:
        if wire.insulated:
            continue
        for i in range(len(wire.path) - 1):
            p1, p2 = wire.path[i], wire.path[i + 1]
            if (p1.x, p1.y) <= (p2.x, p2.y):
                segs.add((p1, p2, wire.side))
            else:
                segs.add((p2, p1, wire.side))
    return segs


def _nudge_wire(
    wire: WireSegment,
    dx: float,
    dy: float,
    board_pads: set[Point],
    working_wires: list[WireSegment],
    occupied_pins: dict[tuple[Point, str], uuid.UUID],
) -> WireSegment | None:
    """Try to shift *wire* by (dx, dy).

    Returns the shifted WireSegment if all new path points are free board pads
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
    return WireSegment(
        id=uuid.uuid4(),
        path=new_path,
        insulated=wire.insulated,
        side=cast(_Lit["top", "bottom"], wire.side),
        color=wire.color,
    )


def _try_push_wire(
    source: Point,
    target: Point,
    working_wires: list[WireSegment],
    additional_wires: list[WireSegment],
    board: Board,
    occupied_pins: dict[tuple[Point, str], uuid.UUID],
    _board_pads: set[Point] | None = None,
) -> tuple[WireSegment, WireSegment] | None:
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
    wires: list[WireSegment],
    source: Point,
    target: Point,
    protected: list[WireSegment],
) -> WireSegment | None:
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
