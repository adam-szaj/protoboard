# SPDX-License-Identifier: MIT
"""Enhanced A* router — opt-in penalties and push-and-shove strategies.

This router is a **sibling** of :class:`AStarRouter`; it does not replace
it.  When every enhanced knob is at its default (zero / disabled),
``EnhancedAStarRouter.route()`` produces output that is byte-identical
to ``AStarRouter.route()``.  Tests in
``tests/unit/test_enhanced_router_smoke.py`` lock that in.

Why a separate router and not a flag on the existing one?  The user
explicitly asked for minimal regression risk: shipping a sibling lets
people opt in per project and lets the default ``astar`` stay frozen
while new behaviour matures.

Architecture
------------

Rather than fork the 3,000-line A* + autorouter code path, the enhanced
router runs the same ``autoroute()`` function.  All new behaviour is
parameterised through additional fields on :class:`RoutingCostModel` and
:class:`RoutingSettings` — the underlying autorouter consults them when
the corresponding worker logic is in place (added in PRs 3-5).  Until
those PRs land, the enhanced router is functionally identical to
``astar``; the smoke test guards that contract.

Tracking Rust port: TODO.  The Rust extension currently mirrors the
default cost model.  Once enhanced penalties stabilise (matching Python
A* behaviour with defaults ≠ 0), the Rust kernel needs the same fields
or a Python-only fallback must be selected when any enhanced knob is
active.  See ``RoutingCostModel`` comments next to each new field.
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


class EnhancedAStarRouter:
    """Sibling of :class:`AStarRouter` with opt-in penalty knobs.

    All new behaviour is off by default (penalty fields = 0, push-and-shove
    extensions disabled).  In that state the router's output equals
    ``AStarRouter``'s — see ``test_enhanced_router_smoke.py``.

    Knobs:

    * ``at_pad_insulated_insulated_same_side`` — extra cost when a step
      lands on a pad that already hosts another insulated wire vertex on
      the same side.  Tuning starting point: 1.0.
    * ``at_pad_insulated_bare_same_side`` — same as above but the
      existing vertex is a bare wire.  Tuning starting point: 3.0.
    * ``parallel_opposite_side`` — extra cost when the step is collinear
      with (and overlaps) a wire segment on the opposite side.  Tuning
      starting point: 0.5.
    * ``max_reroute_factor`` (in RoutingSettings) — push-and-shove
      mid-cell rerouter cost ceiling; 0 disables.  Tuning starting
      point: 1.5.
    """

    name = "astar_enhanced"
    display_name = "A* enhanced (mid-cell + at-pad penalties)"

    def route(
        self,
        request: "RouteRequest",
        sink: ProgressSink,
    ) -> "AutorouteResult":
        import gc
        from protoboard.services.routing.autorouter import autoroute

        rs = request.routing_settings
        worker_layout = request.layout.to_layout()
        worker_schematic = request.schematic.to_schematic()

        # Worker-thread GC quiet zone.
        # The enhanced router builds additional indices per attempt and
        # mid-cell reroute kicks new A* searches inside the same loop —
        # both alloc lots of small Python objects on the worker thread.
        # CPython 3.14.2 has a known race where cyclic-GC ``mark_stacks``
        # walking the heap concurrently with Qt's main-thread paintEvent
        # silently SIGSEGVs (see memory ``project_silent_crash_post_upgrade``
        # and the corresponding ``__init__.py`` xcb workaround).  Disabling
        # the *cyclic* collector for the duration of the call eliminates
        # the race; refcount-based deallocation keeps memory bounded and
        # a single ``gc.collect()`` afterwards reclaims any cycles created
        # during routing.  This is a per-call suppression, not a global
        # opt-out, so other parts of the app keep their normal GC cadence.
        _gc_was_enabled = gc.isenabled()
        if _gc_was_enabled:
            gc.disable()
        try:
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
        finally:
            if _gc_was_enabled:
                gc.enable()
                # Reclaim any cycles created during the routing pass so
                # they don't sit around until the next natural collection.
                gc.collect()

    def capabilities(self) -> RouterCapabilities:
        return RouterCapabilities(
            supports_layer_change=True,
            supports_bare_wires=True,
            supports_bare_diagonal=True,
            supports_push_and_shove=True,
            supports_rip_up_and_retry=True,
            supports_optimize_passes=True,
            notes=(
                "Experimental A* with opt-in pad-crossing and parallel-track "
                "penalties plus mid-cell preferred push-and-shove.  Python-only "
                "code path (no Rust kernel); slower than 'astar' on dense "
                "boards but supports finer cost shaping.  All extra knobs "
                "default to 0 — identical output to 'astar' until tuned."
            ),
        )


# Statically assert that EnhancedAStarRouter satisfies the Router protocol.
_check: Router = EnhancedAStarRouter()
del _check


def register_enhanced() -> None:
    """Add :class:`EnhancedAStarRouter` to the global registry.

    Idempotent — re-registration replaces the previous entry.  Called at
    import; expose a public name so app startup can call it defensively.
    """
    registry.register(EnhancedAStarRouter())


register_enhanced()


__all__ = ["EnhancedAStarRouter", "register_enhanced"]
