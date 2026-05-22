# SPDX-License-Identifier: MIT
"""Tests for the Router protocol, registry, and built-in AStarRouter."""

from __future__ import annotations

import pytest

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.layout.layout import Layout
from protoboard.domain.routing_settings import RoutingSettings
from protoboard.domain.schematic.schematic import Schematic
from protoboard.services.routing.autorouter import AutorouteResult
from protoboard.services.routing.builtin import AStarRouter
from protoboard.services.routing.protocol import (
    ProgressSink,
    Router,
    RouterCapabilities,
    _Registry,
    registry,
)
from protoboard.services.routing.snapshot import RouteRequest


def _perfboard() -> Board:
    grid = BoardGrid(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        ),
        rows=4,
        cols=4,
        row_gap=0.0,
        col_gap=0.0,
    )
    return Board(root=grid)


def _empty_request() -> RouteRequest:
    return RouteRequest.from_session(
        board=_perfboard(),
        layout=Layout(),
        schematic=Schematic(nets={}),
        components={},
        instances={},
        routing_settings=RoutingSettings(),
    )


class _FakeRouter:
    """Minimal Router stub for registry tests."""

    name = "fake"
    display_name = "Fake (test only)"

    def __init__(self, called: list[RouteRequest] | None = None) -> None:
        self.called: list[RouteRequest] = called if called is not None else []

    def route(self, request: RouteRequest, sink: ProgressSink) -> AutorouteResult:
        self.called.append(request)
        return AutorouteResult()

    def capabilities(self) -> RouterCapabilities:
        return RouterCapabilities(notes="fake")

    def supported_cost_keys(self) -> frozenset[str]:
        return frozenset()


class TestRouterProtocol:
    def test_runtime_check_accepts_compliant_class(self) -> None:
        assert isinstance(_FakeRouter(), Router)

    def test_astar_router_satisfies_protocol(self) -> None:
        assert isinstance(AStarRouter(), Router)

    def test_runtime_check_rejects_non_router(self) -> None:
        class _NotARouter:
            pass
        assert not isinstance(_NotARouter(), Router)


class TestRegistry:
    def test_register_and_lookup(self) -> None:
        reg = _Registry()
        r = _FakeRouter()
        reg.register(r)
        assert reg.get("fake") is r

    def test_lookup_missing_raises(self) -> None:
        reg = _Registry()
        with pytest.raises(KeyError):
            reg.get("nonexistent")

    def test_reregister_replaces(self) -> None:
        reg = _Registry()
        r1 = _FakeRouter()
        r2 = _FakeRouter()
        reg.register(r1)
        reg.register(r2)
        assert reg.get("fake") is r2

    def test_register_rejects_empty_name(self) -> None:
        class _Anon:
            name = ""
            display_name = "x"
        reg = _Registry()
        with pytest.raises(ValueError):
            reg.register(_Anon())  # type: ignore[arg-type]

    def test_list_available_sorted(self) -> None:
        reg = _Registry()

        class _A:
            name = "zebra"; display_name = "Z"
            def route(self, *_a, **_k): ...
            def capabilities(self): return RouterCapabilities()
            def supported_cost_keys(self): return frozenset()
        class _B:
            name = "alpha"; display_name = "A"
            def route(self, *_a, **_k): ...
            def capabilities(self): return RouterCapabilities()
            def supported_cost_keys(self): return frozenset()

        reg.register(_A())  # type: ignore[arg-type]
        reg.register(_B())  # type: ignore[arg-type]
        assert reg.list_available() == ["alpha", "zebra"]

    def test_default_prefers_astar(self) -> None:
        reg = _Registry()
        reg.register(_FakeRouter())
        reg.register(AStarRouter())
        assert reg.default().name == "astar"

    def test_default_falls_back_to_alpha_when_no_astar(self) -> None:
        reg = _Registry()

        class _Beta:
            name = "beta"; display_name = "B"
            def route(self, *_a, **_k): ...
            def capabilities(self): return RouterCapabilities()
            def supported_cost_keys(self): return frozenset()
        class _Alpha:
            name = "alpha"; display_name = "A"
            def route(self, *_a, **_k): ...
            def capabilities(self): return RouterCapabilities()
            def supported_cost_keys(self): return frozenset()

        reg.register(_Beta())  # type: ignore[arg-type]
        reg.register(_Alpha())  # type: ignore[arg-type]
        assert reg.default().name == "alpha"

    def test_default_empty_raises(self) -> None:
        reg = _Registry()
        with pytest.raises(LookupError):
            reg.default()


class TestGlobalRegistry:
    def test_builtin_astar_is_registered(self) -> None:
        # builtin module is imported here to ensure side-effect ran.
        import protoboard.services.routing.builtin  # noqa: F401
        assert "astar" in registry.list_available()

    def test_global_default_is_astar(self) -> None:
        import protoboard.services.routing.builtin  # noqa: F401
        assert registry.default().name == "astar"


class TestAStarRouter:
    def test_route_returns_autoroute_result(self) -> None:
        router = AStarRouter()
        sink = ProgressSink()
        result = router.route(_empty_request(), sink)
        assert isinstance(result, AutorouteResult)
        # Empty schematic → no wires, no unrouted nets.
        assert result.wires == []
        assert result.unrouted_nets == []

    def test_capabilities_reports_bare_diagonal(self) -> None:
        caps = AStarRouter().capabilities()
        assert caps.supports_bare_diagonal
        assert caps.supports_layer_change


class TestProgressSink:
    def test_cancelled_returns_false_with_no_flag(self) -> None:
        sink = ProgressSink()
        assert not sink.cancelled()

    def test_cancelled_reflects_flag(self) -> None:
        import threading
        ev = threading.Event()
        sink = ProgressSink(cancel_flag=ev)
        assert not sink.cancelled()
        ev.set()
        assert sink.cancelled()
