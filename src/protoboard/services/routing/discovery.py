# SPDX-License-Identifier: MIT
"""Discover and register external routers via :mod:`importlib.metadata` entry points.

A third-party plugin opts in by declaring an entry point::

    [project.entry-points."protoboard.routers"]
    my-router = "my_package.module:MyRouter"

At app startup, :func:`discover_plugins` walks all installed entry points
in the ``protoboard.routers`` group, imports each one, instantiates the
class (or callable factory) and registers the result.  Failures are
logged but do not crash the app — a broken plugin must never take down
the host application.
"""

from __future__ import annotations

from importlib.metadata import EntryPoint, entry_points

import structlog

from protoboard.services.routing.protocol import Router, registry

_log = structlog.get_logger(__name__)

ENTRY_POINT_GROUP = "protoboard.routers"


def _load_one(ep: EntryPoint) -> Router | None:
    """Load and instantiate one entry-point router.

    The entry point may resolve to either:
    - a Router subclass / type (called with no args), or
    - an already-constructed Router instance, or
    - a zero-arg factory returning a Router.

    Returns the constructed Router or ``None`` if the entry point is
    malformed (with a warning logged).
    """
    try:
        obj = ep.load()
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "routing.discovery.load_failed",
            entry_point=ep.name,
            module=ep.value,
            error=str(exc),
        )
        return None

    if isinstance(obj, type):
        try:
            instance = obj()
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "routing.discovery.construct_failed",
                entry_point=ep.name,
                error=str(exc),
            )
            return None
    elif callable(obj) and not hasattr(obj, "route"):
        try:
            instance = obj()
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "routing.discovery.factory_failed",
                entry_point=ep.name,
                error=str(exc),
            )
            return None
    else:
        instance = obj

    if not isinstance(instance, Router):
        _log.warning(
            "routing.discovery.protocol_mismatch",
            entry_point=ep.name,
            type=type(instance).__name__,
        )
        return None

    return instance


def discover_plugins() -> list[str]:
    """Discover and register every router exposed via the entry-point group.

    Idempotent: re-registering a previously known router replaces it.
    Returns the list of newly registered router names (for logging).
    """
    registered: list[str] = []
    try:
        eps = entry_points(group=ENTRY_POINT_GROUP)
    except Exception as exc:  # noqa: BLE001
        _log.warning("routing.discovery.entry_points_failed", error=str(exc))
        return registered

    for ep in eps:
        instance = _load_one(ep)
        if instance is None:
            continue
        registry.register(instance)
        registered.append(instance.name)
        _log.info(
            "routing.discovery.registered",
            entry_point=ep.name,
            router_name=instance.name,
        )

    return registered


__all__ = ["discover_plugins", "ENTRY_POINT_GROUP"]
