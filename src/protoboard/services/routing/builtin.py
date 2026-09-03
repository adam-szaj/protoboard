# SPDX-License-Identifier: MIT
"""Built-in routers, registered at import time.

This module exists so the rest of the codebase can ``import protoboard.services.routing.builtin``
to guarantee the default routers are present in the registry.  Plugins
discovered via entry-points are registered separately by the loader.
"""  # noqa: E501

from __future__ import annotations

from typing import TYPE_CHECKING

from protoboard.services.routing.protocol import (
    ProgressSink,
    Router,
    RouterCapabilities,
    registry,
)

if TYPE_CHECKING:
    from protoboard.services.routing.results import AutorouteResult
    from protoboard.services.routing.snapshot import RouteRequest


class AStarRouter:
    """Default A* router — wraps the existing :func:`autoroute` function.

    Adapts the immutable RouteRequest back into the legacy mutable
    Layout/Schematic shapes the function expects.  The adaptation is a
    fresh copy each call, so the legacy code can mutate freely without
    affecting the request.
    """

    name = "astar"
    display_name = "A* (default)"

    def route(
        self,
        request: RouteRequest,
        sink: ProgressSink,
    ) -> AutorouteResult:
        from protoboard.services.routing.autorouter import autoroute
        from protoboard.services.routing.hierarchical import hierarchical_autoroute

        rs = request.routing_settings
        worker_layout = request.layout.to_layout()
        worker_schematic = request.schematic.to_schematic()
        entry = hierarchical_autoroute if rs.hierarchical else autoroute

        return entry(
            board=request.board,
            layout=worker_layout,
            schematic=worker_schematic,
            components=dict(request.components),
            instances=dict(request.instances),
            side=request.side,
            cost_model=rs.cost_model(),
            preferred_side=rs.preferred_side,
            allow_layer_change=rs.allow_layer_change,
            max_rip_up_passes=rs.max_rip_up_passes,
            max_optimize_iterations=rs.max_optimize_iterations,
            wire_mode=rs.wire_mode,
            max_detour_factor=rs.max_detour_factor,
            progress_callback=sink.progress,
            optimize_progress_callback=sink.optimize_progress,
            cancel_flag=sink.cancel_flag,
            net_ids=set(request.net_ids) if request.net_ids is not None else None,
            report_path=request.report_path,  # type: ignore[arg-type]
            push_and_shove=rs.push_and_shove,
            enable_simplify_pass=rs.enable_simplify_pass,
            simplify_after_each_wire=rs.simplify_after_each_wire,
            simplify_all_after_autoroute=rs.simplify_all_after_autoroute,
            simplify_all_max_iterations=rs.simplify_all_max_iterations,
            simplify_mode=rs.simplify_mode,
        )

    def capabilities(self) -> RouterCapabilities:
        return RouterCapabilities(
            supports_layer_change=True,
            supports_bare_wires=True,
            supports_bare_diagonal=True,
            supports_push_and_shove=True,
            supports_rip_up_and_retry=True,
            supports_optimize_passes=True,
            notes=(
                "Greedy A* with rip-up/retry, optional parallel optimisation "
                "passes and a bare-diagonal mode.  Default for all projects."
            ),
        )

    def supported_cost_keys(self) -> frozenset[str]:
        return frozenset(ASTAR_COST_KEYS)


# Cost keys read by the vanilla A* hot loop and rip-up retry machinery.
# Kept module-level so tests can assert on it without instantiating a router.
ASTAR_COST_KEYS: frozenset[str] = frozenset(
    {
        # Core path cost
        "free_pad",
        "same_net_pad",
        "occupied_by_other_net",
        "direction_change",
        "diagonal_multiplier",
        # Crossings (bare×bare; granular ib_*/ii_* below)
        "crossing_bare_bare",
        "crossing_bare_bare_diagonal",
        # Vias / under-component
        "layer_change",
        "under_component_top",
        # Pre-route congestion estimator (consumed by autorouter pre-pass, merged
        # into the under-component penalty map before A* sees it).
        "congestion_weight",
        # Modes
        "bare_diagonal_mode",
        # Rip-up / push-and-shove
        "max_reroute_factor",
        # Same-net reuse
        "same_net_wire_reuse_cost",
        # "Clean routing" penalties — angle-aware bends, diagonal parity, and
        # foreign-pad / via avoidance.  All default to 0.0, so the historical
        # behaviour is preserved unless explicitly enabled via preset/UI.
        "bend_45",
        "bend_90",
        "bend_135",
        "diagonal_preferred_family",
        "diagonal_dispreferred_family",
        "wire_over_foreign_wire_pad",
        "via_on_foreign_pad",
        "redundant_layer_change",
        "deferred_via_lookahead",
        "deferred_via_priority_mode",
        # Granular crossing knobs (B2 will activate them inside _crossing_cost).
        "crossing_ib_at_pad_same_side",
        "crossing_ib_at_pad_opposite_side",
        "crossing_ib_off_pad_same_side",
        "crossing_ib_off_pad_opposite_side",
        "crossing_ii_at_pad_same_side",
        "crossing_ii_at_pad_opposite_side",
        "crossing_ii_off_pad_same_side",
        "crossing_ii_off_pad_opposite_side",
    }
)


# Compatibility alias for callers that used the old private constant.
_ASTAR_COST_KEYS = ASTAR_COST_KEYS


# Statically assert that AStarRouter satisfies the Router protocol.
_check: Router = AStarRouter()
del _check


def register_builtins() -> None:
    """Add all built-in routers to the global registry.

    Idempotent — safe to call multiple times.  Called automatically at
    import; expose a public name so app startup can also call it
    defensively before discovering entry-point plugins.
    """
    registry.register(AStarRouter())


register_builtins()


__all__ = ["ASTAR_COST_KEYS", "AStarRouter", "register_builtins"]
