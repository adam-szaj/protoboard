# SPDX-License-Identifier: MIT
"""Discover and register external routing plugins via entry points.

Two plugin groups are supported, sharing the same load/register
machinery:

- ``protoboard.routers`` — full Router implementations
  (turn a RouteRequest into wires).
- ``protoboard.wire_optimizers`` — WireOptimizer post-processors
  (take an AutorouteResult and return a transformed one).

A third-party plugin opts in by declaring an entry point::

    [project.entry-points."protoboard.routers"]
    my-router = "my_package.module:MyRouter"

    [project.entry-points."protoboard.wire_optimizers"]
    my-optimizer = "my_package.module:MyOptimizer"

At app startup :func:`discover_all_plugins` walks both groups, imports
each entry point, instantiates the class (or invokes the factory) and
registers the result in the matching registry.  Failures are logged but
never crash the host application.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import EntryPoint, entry_points
from typing import Any

import structlog

from protoboard.services.routing.optimizer import WireOptimizer
from protoboard.services.routing.optimizer import registry as optimizer_registry
from protoboard.services.routing.protocol import Router
from protoboard.services.routing.protocol import registry as router_registry

_log = structlog.get_logger(__name__)

ROUTER_GROUP = "protoboard.routers"
OPTIMIZER_GROUP = "protoboard.wire_optimizers"


def _load_and_construct(
    ep: EntryPoint,
    protocol_check: Callable[[object], bool],
    role: str,
) -> Any | None:
    """Load *ep*, instantiate if needed, validate against *protocol_check*.

    *role* is a short string ("router" / "optimizer") used in log lines.

    The entry point may resolve to:
    - a class (called with no args),
    - a zero-arg factory (callable without a `route`/`optimize` attribute),
    - an already-constructed instance satisfying the protocol.

    Returns the validated instance or ``None``.  Never raises.
    """
    try:
        obj = ep.load()
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "routing.discovery.load_failed",
            role=role,
            entry_point=ep.name,
            module=ep.value,
            error=str(exc),
        )
        return None

    # Decide whether to call the loaded object: a class or a bare callable
    # without the protocol's "is this already an instance?" sentinel
    # attribute (route/optimize) is treated as a factory.  This keeps the
    # "already-built instance" case working without extra ceremony.
    is_class = isinstance(obj, type)
    is_factory = not is_class and callable(obj) and not protocol_check(obj)

    if is_class or is_factory:
        try:
            instance = obj()
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "routing.discovery.construct_failed",
                role=role,
                entry_point=ep.name,
                error=str(exc),
            )
            return None
    else:
        instance = obj

    if not protocol_check(instance):
        _log.warning(
            "routing.discovery.protocol_mismatch",
            role=role,
            entry_point=ep.name,
            type=type(instance).__name__,
        )
        return None

    return instance


def _discover_into(
    group: str,
    protocol_check: Callable[[object], bool],
    register: Callable[[Any], Any],
    role: str,
) -> list[str]:
    """Walk *group*, load each entry point, register successes.

    Returns the list of registered plugin names (in entry-point order).
    """
    registered: list[str] = []
    try:
        eps = entry_points(group=group)
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "routing.discovery.entry_points_failed",
            group=group,
            error=str(exc),
        )
        return registered

    for ep in eps:
        instance = _load_and_construct(ep, protocol_check, role=role)
        if instance is None:
            continue
        register(instance)
        registered.append(instance.name)
        _log.info(
            "routing.discovery.registered",
            role=role,
            entry_point=ep.name,
            plugin_name=instance.name,
        )

    return registered


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def discover_router_plugins() -> list[str]:
    """Walk ``protoboard.routers`` and register all valid routers."""
    return _discover_into(
        group=ROUTER_GROUP,
        protocol_check=lambda x: isinstance(x, Router),
        register=router_registry.register,
        role="router",
    )


def discover_optimizer_plugins() -> list[str]:
    """Walk ``protoboard.wire_optimizers`` and register all valid optimizers."""
    return _discover_into(
        group=OPTIMIZER_GROUP,
        protocol_check=lambda x: isinstance(x, WireOptimizer),
        register=optimizer_registry.register,
        role="optimizer",
    )


def discover_all_plugins() -> dict[str, list[str]]:
    """Run discovery for every supported plugin group.

    Returns a dict keyed by role ("routers", "wire_optimizers") with the
    list of newly registered plugin names per group.
    """
    return {
        "routers": discover_router_plugins(),
        "wire_optimizers": discover_optimizer_plugins(),
    }


__all__ = [
    "discover_all_plugins",
    "discover_router_plugins",
    "discover_optimizer_plugins",
    "ROUTER_GROUP",
    "OPTIMIZER_GROUP",
]
