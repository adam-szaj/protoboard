# SPDX-License-Identifier: MIT
"""Pluggable router protocol.

A *router* is anything that can turn a :class:`RouteRequest` (immutable
input) into an :class:`AutorouteResult` (list of new wires + unrouted
nets + per-pass stats).  Implementations must operate in a sandbox: they
read from the request but never reach back into live session state.

Built-in implementations are registered in :mod:`protoboard.services.routing.builtin`.
External plugins register themselves via the ``protoboard.routers``
entry-point group::

    [project.entry-points."protoboard.routers"]
    my-router = "my_package.module:MyRouter"

The application discovers them at startup; failures are logged but do
not crash the app.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from protoboard.services.routing.autorouter import AutorouteResult
    from protoboard.services.routing.snapshot import RouteRequest


@dataclass(frozen=True)
class RouterCapabilities:
    """What a router supports — read by the UI to enable/disable options.

    A router that does not advertise a capability is assumed not to use
    the corresponding RoutingSettings field.  The UI may still show the
    setting but mark it as ignored.
    """

    supports_layer_change: bool = True
    supports_bare_wires: bool = True
    supports_bare_diagonal: bool = False
    supports_push_and_shove: bool = False
    supports_rip_up_and_retry: bool = True
    supports_optimize_passes: bool = True
    # Free-form description shown in the router-picker tooltip.
    notes: str = ""


# Progress callback signatures match the legacy AutorouteWorker contract so
# the worker can wire signals directly without adapter glue.
ProgressFn = Callable[[int, str, int], None]  # pass_num, net_name, pct
OptimizeProgressFn = Callable[[int, int], None]  # done, total
# Per-iteration progress for the iterative simplify-all post-pass.
# Signature: (iter_num, max_iter, n_wires_simplified_this_iter).
# When n_wires_simplified_this_iter == 0 the loop has converged and will
# break on the next check.
SimplifyProgressFn = Callable[[int, int, int], None]


@dataclass
class ProgressSink:
    """Bundle of optional progress callbacks + a cancellation flag.

    Passed by the worker into the router so a router implementation has a
    single object to thread through its internal loops.  Any field may be
    ``None`` for callers that don't care.
    """

    progress: ProgressFn | None = None
    optimize_progress: OptimizeProgressFn | None = None
    simplify_progress: SimplifyProgressFn | None = None
    cancel_flag: threading.Event | None = None

    def cancelled(self) -> bool:
        return self.cancel_flag is not None and self.cancel_flag.is_set()


@runtime_checkable
class Router(Protocol):
    """Pluggable autorouter interface.

    Contract:
    - ``route(request, sink)`` returns an :class:`AutorouteResult` or
      raises ``AutorouteCancelled`` if the sink's flag was set.
    - The router must NOT mutate ``request`` or any object reachable
      through it.  The request is frozen on purpose; treat it as
      read-only.
    - Long-running loops must call ``sink.cancelled()`` periodically and
      raise ``AutorouteCancelled`` when it returns True.
    - Progress reporting is best-effort.  Routers should call
      ``sink.progress(...)`` between net-level work items so the UI's
      progress bar moves.
    """

    name: str  # registry key — must be unique
    display_name: str  # human-readable, shown in UI

    def route(
        self,
        request: RouteRequest,
        sink: ProgressSink,
    ) -> AutorouteResult: ...

    def capabilities(self) -> RouterCapabilities: ...

    def supported_cost_keys(self) -> frozenset[str]:
        """Cost-model keys this router reads.

        Used by the preset/preferences UI to render only the knobs that
        actually influence this router's behaviour.  Unknown keys are
        silently ignored at routing time, so returning a subset is safe
        but renders fewer controls than the router can actually accept.

        Default implementation returns the empty set (router consumes no
        cost knobs); built-in routers override to enumerate their keys.
        """
        ...

    # ------------------------------------------------------------------
    # Optional plugin hooks (not part of the Protocol contract).
    #
    # Plugin routers MAY define these to register custom cost keys with
    # the UI so spinboxes get tooltips and proper tunability flags:
    #
    #     def cost_key_docs(self) -> dict[str, str]:
    #         '''Plugin-specific cost keys with tooltip strings.'''
    #         return {"my_knob": "explanation shown in the UI"}
    #
    #     def user_tunable_cost_keys(self) -> frozenset[str]:
    #         '''Subset of supported_cost_keys that the UI should expose.
    #         Plugin routers can hide implementation-detail knobs by
    #         omitting them here.  Default = everything supported.'''
    #         return self.supported_cost_keys()
    #
    # ``cost_metadata.merged_cost_key_docs()`` and
    # ``merged_user_tunable_cost_keys()`` discover these via getattr
    # with sensible empty defaults, so legacy routers without the hooks
    # keep working unchanged.
    # ------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass
class _Registry:
    """Process-global router registry.

    Module-level instance is exposed as :data:`registry`.  Keep the
    indirection so tests can construct a fresh registry instead of
    polluting global state.
    """

    routers: dict[str, Router] = field(default_factory=dict)

    def register(self, router: Router) -> Router:
        """Add or replace a router by ``router.name``.

        Re-registration is allowed (overrides a previous entry).  Tests
        and plugins relying on lookup-order should not depend on it.
        """
        if not getattr(router, "name", None):
            raise ValueError("Router must define a non-empty `name`")
        self.routers[router.name] = router
        return router

    def get(self, name: str) -> Router:
        try:
            return self.routers[name]
        except KeyError:
            available = ", ".join(sorted(self.routers)) or "<none>"
            raise KeyError(
                f"No router registered as {name!r}; available: {available}"
            ) from None

    def list_available(self) -> list[str]:
        return sorted(self.routers)

    def default(self) -> Router:
        """Return the default router.

        Picks 'astar' if registered, otherwise the alphabetically first.
        Raises ``LookupError`` if the registry is empty.
        """
        if "astar" in self.routers:
            return self.routers["astar"]
        if not self.routers:
            raise LookupError("No routers registered")
        return self.routers[sorted(self.routers)[0]]


registry = _Registry()


__all__ = [
    "Router",
    "RouterCapabilities",
    "ProgressSink",
    "ProgressFn",
    "OptimizeProgressFn",
    "SimplifyProgressFn",
    "registry",
]
