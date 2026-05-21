# SPDX-License-Identifier: MIT
"""Tests for the WireOptimizer protocol and its registry."""

from __future__ import annotations

import pytest

from protoboard.services.routing.autorouter import AutorouteResult
from protoboard.services.routing.optimizer import (
    OptimizerCapabilities,
    WireOptimizer,
    _OptimizerRegistry,
    registry,
)
from protoboard.services.routing.protocol import ProgressSink
from protoboard.services.routing.snapshot import RouteRequest


class _GoodOptimizer:
    name = "noop"
    display_name = "No-op (test)"

    def optimize(
        self,
        request: RouteRequest,
        result: AutorouteResult,
        sink: ProgressSink,
    ) -> AutorouteResult:
        return result

    def capabilities(self) -> OptimizerCapabilities:
        return OptimizerCapabilities(notes="test")


class _NotAnOptimizer:
    """Missing the `optimize` method — fails the Protocol runtime check."""
    name = "nope"
    display_name = "Nope"


class TestProtocol:
    def test_runtime_check_accepts_compliant_class(self) -> None:
        assert isinstance(_GoodOptimizer(), WireOptimizer)

    def test_runtime_check_rejects_incomplete_class(self) -> None:
        assert not isinstance(_NotAnOptimizer(), WireOptimizer)


class TestRegistry:
    def test_register_and_lookup(self) -> None:
        reg = _OptimizerRegistry()
        opt = _GoodOptimizer()
        reg.register(opt)
        assert reg.get("noop") is opt

    def test_lookup_missing_raises(self) -> None:
        reg = _OptimizerRegistry()
        with pytest.raises(KeyError):
            reg.get("nonexistent")

    def test_reregister_replaces(self) -> None:
        reg = _OptimizerRegistry()
        opt1 = _GoodOptimizer()
        opt2 = _GoodOptimizer()
        reg.register(opt1)
        reg.register(opt2)
        assert reg.get("noop") is opt2

    def test_register_rejects_empty_name(self) -> None:
        class _Anon:
            name = ""
            display_name = "x"
        reg = _OptimizerRegistry()
        with pytest.raises(ValueError):
            reg.register(_Anon())  # type: ignore[arg-type]

    def test_list_available_sorted(self) -> None:
        reg = _OptimizerRegistry()

        class _Zeta:
            name = "zeta"; display_name = "Z"
            def optimize(self, *_a, **_k): ...
            def capabilities(self): return OptimizerCapabilities()
        class _Alpha:
            name = "alpha"; display_name = "A"
            def optimize(self, *_a, **_k): ...
            def capabilities(self): return OptimizerCapabilities()

        reg.register(_Zeta())  # type: ignore[arg-type]
        reg.register(_Alpha())  # type: ignore[arg-type]
        assert reg.list_available() == ["alpha", "zeta"]


class TestGlobalRegistry:
    def test_global_registry_is_singleton(self) -> None:
        # Sanity check that the module-level registry is the same object
        # everywhere — tests rely on this for plugin discovery.
        from protoboard.services.routing.optimizer import registry as reg2
        assert reg2 is registry


class TestCapabilities:
    def test_default_flags_are_false(self) -> None:
        caps = OptimizerCapabilities()
        assert not caps.rebalances_sides
        assert not caps.inserts_jumpers
        assert not caps.removes_jumpers
        assert not caps.insulated_to_bare
        assert not caps.bare_to_insulated
        assert not caps.shortens_paths
        assert caps.notes == ""

    def test_capabilities_are_frozen(self) -> None:
        caps = OptimizerCapabilities()
        with pytest.raises(Exception):  # FrozenInstanceError
            caps.shortens_paths = True  # type: ignore[misc]
