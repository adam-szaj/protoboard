# SPDX-License-Identifier: MIT
"""Built-in routers, registered at import time.

This module exists so the rest of the codebase can ``import protoboard.services.routing.builtin``
to guarantee the default routers are present in the registry.  Plugins
discovered via entry-points are registered separately by the loader.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from protoboard.services.routing.protocol import (
    ProgressSink,
    Router,
    RouterCapabilities,
    registry,
)

if TYPE_CHECKING:
    from protoboard.services.routing.autorouter import AutorouteResult
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
        request: "RouteRequest",
        sink: ProgressSink,
    ) -> "AutorouteResult":
        from protoboard.services.routing.autorouter import autoroute

        rs = request.routing_settings
        worker_layout = request.layout.to_layout()
        worker_schematic = request.schematic.to_schematic()

        return autoroute(
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


__all__ = ["AStarRouter", "register_builtins"]
