# SPDX-License-Identifier: MIT
"""Tests for WireOptimizer plugin discovery via entry points."""

from __future__ import annotations

from importlib.metadata import EntryPoint

import pytest

from protoboard.services.routing.autorouter import AutorouteResult
from protoboard.services.routing.discovery import (
    OPTIMIZER_GROUP,
    discover_all_plugins,
    discover_optimizer_plugins,
)
from protoboard.services.routing.optimizer import (
    OptimizerCapabilities,
    _OptimizerRegistry,
    registry as optimizer_registry,
)
from protoboard.services.routing.protocol import ProgressSink
from protoboard.services.routing.snapshot import RouteRequest


class _NoopOptimizer:
    name = "noop-plugin"
    display_name = "No-op Plugin"

    def optimize(
        self,
        request: RouteRequest,
        result: AutorouteResult,
        sink: ProgressSink,
    ) -> AutorouteResult:
        return result

    def capabilities(self) -> OptimizerCapabilities:
        return OptimizerCapabilities(notes="noop")


class _BrokenInitOptimizer:
    name = "broken-opt"
    display_name = "Broken"

    def __init__(self) -> None:
        raise RuntimeError("intentional failure")

    def optimize(self, *_a, **_k) -> AutorouteResult: ...
    def capabilities(self) -> OptimizerCapabilities: ...  # type: ignore[empty-body]


class _NotAnOptimizer:
    """Missing the `optimize` method — Protocol runtime check fails."""
    name = "not-opt"
    display_name = "Not Optimizer"


def _fake_ep(name: str, target: object) -> EntryPoint:
    """Build an EntryPoint whose .load() returns *target*.

    Same trick as in test_router_discovery: wrap the EntryPoint in a
    lightweight proxy so we don't have to touch real package metadata.
    """
    ep = EntryPoint(name=name, value="fake:Target", group=OPTIMIZER_GROUP)
    class _Wrap:
        def __init__(self, real: EntryPoint, payload: object) -> None:
            self._real = real
            self._payload = payload
        def __getattr__(self, attr: str) -> object:
            return getattr(self._real, attr)
        def load(self) -> object:
            return self._payload
    return _Wrap(ep, target)  # type: ignore[return-value]


class TestDiscoverOptimizerPlugins:
    def test_registers_into_optimizer_registry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        eps = [_fake_ep("good", _NoopOptimizer)]
        monkeypatch.setattr(
            "protoboard.services.routing.discovery.entry_points",
            lambda group: eps if group == OPTIMIZER_GROUP else [],
        )
        tmp = _OptimizerRegistry()
        monkeypatch.setattr(
            "protoboard.services.routing.discovery.optimizer_registry", tmp,
        )

        discovered = discover_optimizer_plugins()
        assert discovered == ["noop-plugin"]
        assert "noop-plugin" in tmp.list_available()

    def test_broken_plugin_does_not_abort_discovery(self, monkeypatch: pytest.MonkeyPatch) -> None:
        eps = [
            _fake_ep("good", _NoopOptimizer),
            _fake_ep("bad", _BrokenInitOptimizer),
        ]
        monkeypatch.setattr(
            "protoboard.services.routing.discovery.entry_points",
            lambda group: eps if group == OPTIMIZER_GROUP else [],
        )
        tmp = _OptimizerRegistry()
        monkeypatch.setattr(
            "protoboard.services.routing.discovery.optimizer_registry", tmp,
        )

        discovered = discover_optimizer_plugins()
        assert discovered == ["noop-plugin"]
        assert "broken-opt" not in tmp.list_available()

    def test_protocol_mismatch_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        eps = [_fake_ep("invalid", _NotAnOptimizer)]
        monkeypatch.setattr(
            "protoboard.services.routing.discovery.entry_points",
            lambda group: eps if group == OPTIMIZER_GROUP else [],
        )
        tmp = _OptimizerRegistry()
        monkeypatch.setattr(
            "protoboard.services.routing.discovery.optimizer_registry", tmp,
        )
        assert discover_optimizer_plugins() == []

    def test_entry_points_failure_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(group: str) -> list[EntryPoint]:
            raise RuntimeError("entry_points crashed")
        monkeypatch.setattr(
            "protoboard.services.routing.discovery.entry_points", _boom,
        )
        assert discover_optimizer_plugins() == []


class TestDiscoverAllPlugins:
    def test_discovers_both_groups(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from protoboard.services.routing.discovery import ROUTER_GROUP

        class _FakeRouter:
            name = "fake-router"; display_name = "Fake"
            def route(self, *_a, **_k) -> AutorouteResult:
                return AutorouteResult()
            def capabilities(self):
                from protoboard.services.routing.protocol import RouterCapabilities
                return RouterCapabilities()

        def _eps(group: str) -> list[EntryPoint]:
            if group == ROUTER_GROUP:
                return [_fake_ep("r", _FakeRouter)]
            if group == OPTIMIZER_GROUP:
                return [_fake_ep("o", _NoopOptimizer)]
            return []

        monkeypatch.setattr(
            "protoboard.services.routing.discovery.entry_points", _eps,
        )
        from protoboard.services.routing.discovery import (
            registry as router_reg_alias,
        )
        # Substitute fresh registries on both sides.
        tmp_router = type(router_reg_alias)()
        tmp_opt = _OptimizerRegistry()
        monkeypatch.setattr(
            "protoboard.services.routing.discovery.registry", tmp_router,
        )
        monkeypatch.setattr(
            "protoboard.services.routing.discovery.optimizer_registry", tmp_opt,
        )

        out = discover_all_plugins()
        assert out == {"routers": ["fake-router"], "wire_optimizers": ["noop-plugin"]}
        assert "fake-router" in tmp_router.list_available()
        assert "noop-plugin" in tmp_opt.list_available()


class TestGlobalRegistryIntegration:
    def test_simplify_present_after_builtin_import(self) -> None:
        """Importing builtin_optimizers must register the SimplifyOptimizer."""
        import protoboard.services.routing.builtin_optimizers  # noqa: F401
        assert "simplify" in optimizer_registry.list_available()
