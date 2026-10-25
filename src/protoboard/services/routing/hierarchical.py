# SPDX-License-Identifier: MIT
"""Hierarchical autoroute — bottom-up walk over the group tree.

Strategy:

1. Post-order traverse ``layout.groups`` rooted at ``ROOT_GROUP_ID``.
   Visit each non-root group **after** all its descendants.
2. For each visited group, route only the nets that are *fully internal*
   to the group's transitive component set, resolved through
   :class:`protoboard.domain.layout.group_tree.GroupTree`. Wires from earlier
   groups are kept in ``layout.wires`` so the next pass sees them as obstacles
   and as same-net candidates for reuse.
3. After the tree is exhausted, route every remaining net at the
   top level.  This is where global rails (GND/VCC) and cross-group
   connections (typically going through exported pins) get drawn.

The result is functionally equivalent to a single flat autoroute call,
but lets the user's group organisation drive the routing strategy:
strongly-coupled cliques inside a subsystem are routed tight before any
long-haul cross-group wire competes for the same channels.

The function leans on the existing :func:`autoroute` for the heavy
lifting; this module only sequences the calls and merges their
``AutorouteResult`` objects.
"""

from __future__ import annotations

import copy
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import structlog

from protoboard.domain.board.board import Board
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.group_tree import GroupTree
from protoboard.domain.layout.layout import ROOT_GROUP_ID, Layout
from protoboard.domain.layout.wire import Wire
from protoboard.domain.routing.cost_model import RoutingCostModel
from protoboard.domain.schematic.schematic import Schematic
from protoboard.services.routing.autorouter import autoroute, write_report
from protoboard.services.routing.results import AutorouteResult, PassStats
from protoboard.types import Side

if TYPE_CHECKING:
    from protoboard.domain.board.pad_state import PadIndex
    from protoboard.domain.routing.simplify_mode import SimplifyMode


_log = structlog.get_logger(__name__)


def _postorder_groups(
    groups: Mapping[uuid.UUID, object],  # ComponentGroup; Mapping for covariance
    root_id: uuid.UUID,
) -> list[uuid.UUID]:
    """Return non-root group ids in post-order (children before parent).

    Cycles are guarded against by a visited set; in production the group
    tree is acyclic (enforced by :func:`would_create_cycle` at command
    time), so the guard is purely defensive.
    """
    order: list[uuid.UUID] = []
    visited: set[uuid.UUID] = set()

    def _dfs(node_id: uuid.UUID) -> None:
        if node_id in visited:
            return
        visited.add(node_id)
        node = groups.get(node_id)
        if node is None:
            return
        for child_id in getattr(node, "child_group_ids", frozenset()):
            _dfs(child_id)
        if node_id != root_id:
            order.append(node_id)

    _dfs(root_id)
    return order


def hierarchical_autoroute(
    board: Board,
    layout: Layout,
    schematic: Schematic,
    components: dict[str, ComponentDefinition],
    instances: dict[uuid.UUID, ComponentInstance],
    *,
    side: Side = "top",
    cost_model: RoutingCostModel,
    max_rip_up_passes: int = 3,
    insulated: bool = True,
    allow_layer_change: bool = True,
    max_optimize_iterations: int = 10,
    preferred_side: Side = "bottom",
    wire_mode: Literal["bare", "insulated", "auto", "bare_diagonal"] = "insulated",
    max_detour_factor: float = 0.0,
    progress_callback: Callable[[int, str, int], None] | None = None,
    optimize_progress_callback: Callable[[int, int], None] | None = None,
    cancel_flag: threading.Event | None = None,
    net_ids: set[uuid.UUID] | None = None,
    report_path: Path | None = None,
    push_and_shove: bool = True,
    enable_simplify_pass: bool = True,
    simplify_after_each_wire: bool = False,
    simplify_all_after_autoroute: bool = True,
    simplify_all_max_iterations: int = 50,
    simplify_mode: SimplifyMode | None = None,
    pad_index: PadIndex | None = None,
) -> AutorouteResult:
    """Route ``schematic`` via a bottom-up walk over ``layout.groups``.

    Falls back to a single flat ``autoroute`` call when the project has
    no real group structure (root with no child groups).

    Returns the merged :class:`AutorouteResult` across all passes.  Stats
    are concatenated; unrouted nets are the union across passes (any net
    that failed in its target pass).
    """
    # Determine the universe of nets we are allowed to route.
    target_net_ids = (
        net_ids if net_ids is not None else {n.id for n in schematic.nets.values()}
    )

    # Working layout — keep a single mutable copy that accumulates wires
    # across passes so each subsequent pass sees prior results as
    # obstacles + same-net reuse candidates.  Caller's layout is left
    # untouched (matches flat ``autoroute`` semantics).
    working_layout = copy.deepcopy(layout)

    group_order = _postorder_groups(working_layout.groups, ROOT_GROUP_ID)
    _log.info(
        "hierarchical.start",
        n_groups=len(group_order),
        n_target_nets=len(target_net_ids),
    )

    merged_wires: list[Wire] = []
    merged_unrouted: set[str] = set()
    merged_stats: list[PassStats] = []
    routed_net_ids: set[uuid.UUID] = set()

    def _route_subset(
        subset_ids: set[uuid.UUID],
        stage_label: str,
    ) -> None:
        """Run one autoroute pass over *subset_ids* and merge results."""
        if not subset_ids:
            return
        if cancel_flag is not None and cancel_flag.is_set():
            return
        _log.info(
            "hierarchical.stage",
            stage=stage_label,
            n_nets=len(subset_ids),
        )
        # Wrap progress_callback so the GUI sees which group is being
        # routed.  Format: "[group_label] net_name".  The original
        # net_name still appears so caller-side highlighting still works.
        if progress_callback is not None:
            _outer_cb = progress_callback
            _stage = stage_label

            def _wrapped_progress(
                pass_num: int,
                net_name: str,
                pct: int,
            ) -> None:
                _outer_cb(pass_num, f"[{_stage}] {net_name}", pct)

            stage_progress_cb = _wrapped_progress
        else:
            stage_progress_cb = None

        sub_result = autoroute(
            board=board,
            layout=working_layout,
            schematic=schematic,
            components=components,
            instances=instances,
            side=side,
            cost_model=cost_model,
            max_rip_up_passes=max_rip_up_passes,
            insulated=insulated,
            allow_layer_change=allow_layer_change,
            max_optimize_iterations=max_optimize_iterations,
            preferred_side=preferred_side,
            wire_mode=wire_mode,
            max_detour_factor=max_detour_factor,
            progress_callback=stage_progress_cb,
            optimize_progress_callback=optimize_progress_callback,
            cancel_flag=cancel_flag,
            net_ids=subset_ids,
            report_path=None,
            push_and_shove=push_and_shove,
            enable_simplify_pass=enable_simplify_pass,
            simplify_after_each_wire=simplify_after_each_wire,
            # Iterative simplify-all runs INSIDE each group's autoroute
            # call (N+1 total: once per group + once at top-level).  This
            # is intentional — subsequent groups see prior groups' wires
            # already in their simplified shape, which produces better
            # routing decisions than deferring all cleanup to the end.
            # The cost is bounded: simplify is conservative (only
            # shortens) so per-group loops converge in 2-5 iterations.
            simplify_all_after_autoroute=simplify_all_after_autoroute,
            simplify_all_max_iterations=simplify_all_max_iterations,
            simplify_mode=simplify_mode,
            pad_index=pad_index,
        )
        # Export separate wires without working-layout knot refs. AddWireCommand
        # resolves their endpoints against the destination layout when applied.
        merged_wires.extend(replace(w, a=None, b=None) for w in sub_result.wires)
        # Append routed wires to working_layout so subsequent passes see them.
        for w in sub_result.wires:
            try:
                working_layout.ensure_wire_knots(w)
                working_layout.add_wire(w)
            except (ValueError, KeyError):
                # If the wire failed to land (knot conflict with prior
                # geometry) we keep it in the result but skip it as an
                # obstacle — better than crashing the whole hierarchical
                # run.  Flat autoroute reports identical issues today.
                pass
        merged_unrouted.update(sub_result.unrouted_nets)
        merged_stats.extend(sub_result.pass_stats)
        routed_net_ids.update(subset_ids)

    # ---- Bottom-up pass over groups ---------------------------------
    group_tree = GroupTree(working_layout.groups)
    for gid in group_order:
        if cancel_flag is not None and cancel_flag.is_set():
            break
        group_comps = group_tree.all_component_ids(gid)
        internal = group_tree.internal_net_ids_for_components(
            frozenset(group_comps),
            schematic,
        )
        subset = set(internal) - routed_net_ids
        subset &= target_net_ids
        # Prefer the user-facing group name; fall back to the truncated
        # id when the group has no name set yet.
        grp = working_layout.groups.get(gid)
        grp_label = (
            grp.name if grp is not None and grp.name else f"group:{str(gid)[:8]}"
        )
        _route_subset(subset, stage_label=grp_label)

    # ---- Top-level pass: anything left -------------------------------
    remaining = target_net_ids - routed_net_ids
    _route_subset(remaining, stage_label="top")

    # Optional report on the merged result.
    if report_path is not None:
        merged_result = AutorouteResult(
            wires=merged_wires,
            unrouted_nets=sorted(merged_unrouted),
            pass_stats=merged_stats,
        )
        write_report(merged_result, report_path)
        return merged_result

    return AutorouteResult(
        wires=merged_wires,
        unrouted_nets=sorted(merged_unrouted),
        pass_stats=merged_stats,
    )


__all__ = ["hierarchical_autoroute"]
