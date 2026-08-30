# SPDX-License-Identifier: MIT
"""Tests for entry-point based router plugin discovery.

The discovery loader must:
- discover Router subclasses exposed via the ``protoboard.routers``
  entry-point group,
- accept either a class, a callable factory, or a pre-built instance,
- never crash the app on malformed plugins (log + skip).
"""

from __future__ import annotations

from importlib.metadata import EntryPoint

import pytest

from protoboard.services.routing.autorouter import AutorouteResult
from protoboard.services.routing.discovery import _load_one, discover_plugins
from protoboard.services.routing.protocol import (
    ProgressSink,
    RouterCapabilities,
    _Registry,
    registry,
)
from protoboard.services.routing.snapshot import RouteRequest


class _GoodRouter:
    name = "good-plugin"
    display_name = "Good Plugin"

    def route(self, request: RouteRequest, sink: ProgressSink) -> AutorouteResult:
        return AutorouteResult()

    def capabilities(self) -> RouterCapabilities:
        return RouterCapabilities()

    def supported_cost_keys(self) -> frozenset[str]:
        return frozenset()


def _good_factory() -> _GoodRouter:
    return _GoodRouter()


class _BrokenInit:
    name = "broken-init"
    display_name = "Broken Init"

    def __init__(self) -> None:
        raise RuntimeError("intentional init failure")

    def route(self, *_a, **_k) -> AutorouteResult:
        return AutorouteResult()

    def capabilities(self) -> RouterCapabilities:
        return RouterCapabilities()


class _NotARouter:
    """Has no `route` method — fails the Protocol runtime check."""

    name = "not-a-router"
    display_name = "Not A Router"


# ---------------------------------------------------------------------------
# _load_one — single-entry-point branches
# ---------------------------------------------------------------------------


def _fake_ep(name: str, target: object) -> EntryPoint:
    """Build an EntryPoint whose .load() returns *target*.

    importlib.metadata.EntryPoint is a NamedTuple; we monkeypatch the
    instance's `load` to short-circuit module resolution.
    """
    ep = EntryPoint(name=name, value="fake:Target", group="protoboard.routers")

    # Replace .load() on this instance only — we can't override on a
    # NamedTuple subclass, so use a closure trick via attribute assignment
    # on a wrapper object.
    class _Wrap:
        def __init__(self, real: EntryPoint, payload: object) -> None:
            self._real = real
            self._payload = payload

        def __getattr__(self, attr: str) -> object:
            return getattr(self._real, attr)

        def load(self) -> object:
            return self._payload

    return _Wrap(ep, target)  # type: ignore[return-value]


class TestLoadOne:
    def test_loads_class(self) -> None:
        r = _load_one(_fake_ep("good", _GoodRouter))
        assert r is not None
        assert r.name == "good-plugin"

    def test_loads_factory(self) -> None:
        r = _load_one(_fake_ep("good", _good_factory))
        assert r is not None
        assert r.name == "good-plugin"

    def test_loads_prebuilt_instance(self) -> None:
        r = _load_one(_fake_ep("good", _GoodRouter()))
        assert r is not None
        assert r.name == "good-plugin"

    def test_broken_init_returns_none(self) -> None:
        # Must NOT raise — just return None and log a warning.
        r = _load_one(_fake_ep("bad", _BrokenInit))
        assert r is None

    def test_protocol_mismatch_returns_none(self) -> None:
        r = _load_one(_fake_ep("not-a-router", _NotARouter))
        assert r is None

    def test_load_failure_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ep = _fake_ep("broken", _GoodRouter)
        # Replace .load on the wrapper so it raises.
        monkeypatch.setattr(
            ep, "load", lambda: (_ for _ in ()).throw(ImportError("boom"))
        )
        r = _load_one(ep)
        assert r is None


# ---------------------------------------------------------------------------
# discover_plugins — registry interaction
# ---------------------------------------------------------------------------


class TestDiscoverPlugins:
    def test_registers_into_global_registry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        eps = [_fake_ep("good", _GoodRouter)]
        monkeypatch.setattr(
            "protoboard.services.routing.discovery.entry_points",
            lambda group: eps,
        )
        # Use a temporary registry so the test doesn't pollute global state.
        tmp = _Registry()
        monkeypatch.setattr(
            "protoboard.services.routing.discovery.registry",
            tmp,
        )

        discovered = discover_plugins()
        assert discovered == ["good-plugin"]
        assert "good-plugin" in tmp.list_available()

    def test_broken_plugin_does_not_abort_discovery(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        eps = [
            _fake_ep("good", _GoodRouter),
            _fake_ep("bad", _BrokenInit),
        ]
        monkeypatch.setattr(
            "protoboard.services.routing.discovery.entry_points",
            lambda group: eps,
        )
        tmp = _Registry()
        monkeypatch.setattr(
            "protoboard.services.routing.discovery.registry",
            tmp,
        )

        discovered = discover_plugins()
        # The good one survived; the broken one was silently skipped.
        assert discovered == ["good-plugin"]
        assert "good-plugin" in tmp.list_available()
        assert "broken-init" not in tmp.list_available()

    def test_no_entry_points_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "protoboard.services.routing.discovery.entry_points",
            lambda group: [],
        )
        tmp = _Registry()
        monkeypatch.setattr(
            "protoboard.services.routing.discovery.registry",
            tmp,
        )
        assert discover_plugins() == []

    def test_entry_points_failure_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(group: str) -> list[EntryPoint]:
            raise RuntimeError("entry_points crashed")

        monkeypatch.setattr(
            "protoboard.services.routing.discovery.entry_points",
            _boom,
        )
        # Must NOT propagate.
        assert discover_plugins() == []

    def test_idempotent_against_real_global_registry(self) -> None:
        """Calling discover_plugins() with the live entry-point set is a no-op.

        No external routers are declared in the test environment, so the
        call returns an empty list and doesn't disturb the built-in
        AStarRouter.
        """
        # Ensure the side-effect registration ran in this test process.
        import protoboard.services.routing.builtin  # noqa: F401

        before = set(registry.list_available())
        discover_plugins()
        after = set(registry.list_available())
        assert "astar" in after
        assert before.issubset(after)
