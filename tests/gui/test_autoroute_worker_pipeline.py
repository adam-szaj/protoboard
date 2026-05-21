# SPDX-License-Identifier: MIT
"""Tests for AutorouteWorker chaining optimizers after the router.

Worker pipeline contract:
1. Build a snapshot at construction (covered in test_autoroute_worker_snapshot).
2. In run(): pick the router from the registry, call route(request, sink).
3. If optimizer_names is given:
   - suppress autoroute()'s embedded simplify pass via dataclasses.replace,
   - feed result through each named optimizer in order,
   - swallow missing optimizer names (warn + skip), don't crash.
4. Respect cancellation between optimizers.

The worker is exercised via direct ``.run()`` invocation on the test
thread so we can assert on the registered routers/optimizers without
spinning up a QThread.
"""

from __future__ import annotations

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.layout.layout import Layout
from protoboard.domain.routing_settings import RoutingSettings
from protoboard.domain.schematic.schematic import Schematic
from protoboard.gui.workers.autoroute_worker import AutorouteWorker
from protoboard.services.routing.autorouter import AutorouteResult
from protoboard.services.routing.optimizer import (
    OptimizerCapabilities,
    registry as optimizer_registry,
)
from protoboard.services.routing.protocol import (
    ProgressSink,
    RouterCapabilities,
    registry as router_registry,
)
from protoboard.services.routing.snapshot import RouteRequest


def _perfboard() -> Board:
    return Board(
        root=BoardGrid(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            cell=PadAtom(
                width_policy=SizePolicy.HUG,
                height_policy=SizePolicy.HUG,
                layers=PadLayers(top=True, bottom=True, plated_through=True),
            ),
            rows=4, cols=4, row_gap=0.0, col_gap=0.0,
        )
    )


def _empty_request(*, enable_simplify_pass: bool = True) -> RouteRequest:
    return RouteRequest.from_session(
        board=_perfboard(),
        layout=Layout(),
        schematic=Schematic(nets={}),
        components={},
        instances={},
        routing_settings=RoutingSettings(enable_simplify_pass=enable_simplify_pass),
    )


class _RecordingRouter:
    """Captures the request it was called with and returns a fixed result."""

    name = "recording-router"
    display_name = "Recording (test)"

    def __init__(self, wires_to_return: list | None = None) -> None:
        self._wires = wires_to_return or []
        self.last_request: RouteRequest | None = None
        self.calls = 0

    def route(self, request: RouteRequest, sink: ProgressSink) -> AutorouteResult:
        self.last_request = request
        self.calls += 1
        return AutorouteResult(wires=list(self._wires))

    def capabilities(self) -> RouterCapabilities:
        return RouterCapabilities()


class _StubOptimizer:
    """Tag-style optimizer: records that it ran and appends a marker."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.display_name = name
        self.calls = 0

    def optimize(self, request, result, sink):
        self.calls += 1
        # Return a *new* result so we can assert chained values flow through.
        return AutorouteResult(
            wires=list(result.wires),
            unrouted_nets=list(result.unrouted_nets) + [self.name],
            pass_stats=list(result.pass_stats),
        )

    def capabilities(self) -> OptimizerCapabilities:
        return OptimizerCapabilities()


def _make_worker(
    qtbot,
    *, request: RouteRequest, router_name: str,
    optimizer_names: list[str] | None,
) -> tuple[AutorouteWorker, list]:
    """Build a worker that captures emitted finished/error payloads.

    ``qtbot`` is taken just to ensure pytest-qt has spun up a
    QApplication — QThread + Signal need it even though we drive
    ``.run()`` inline on the test thread.
    """
    emitted: list = []
    worker = AutorouteWorker(
        request=request,
        router_name=router_name,
        optimizer_names=optimizer_names,
    )
    # qtbot reference keeps the QApplication alive for the duration of the test.
    assert qtbot is not None
    worker.finished.connect(lambda r: emitted.append(("finished", r)))
    worker.error.connect(lambda m: emitted.append(("error", m)))
    return worker, emitted


class TestLegacyPath:
    """optimizer_names=None ⇒ run() must NOT touch enable_simplify_pass."""

    def test_request_passed_to_router_unchanged(self, qtbot) -> None:
        rec = _RecordingRouter()
        router_registry.register(rec)
        try:
            req = _empty_request(enable_simplify_pass=True)
            worker, emitted = _make_worker(
                qtbot,
                request=req, router_name="recording-router", optimizer_names=None,
            )
            worker.run()
            assert rec.calls == 1
            # Untouched — request flows verbatim.
            assert rec.last_request is req
            assert rec.last_request.routing_settings.enable_simplify_pass is True
            assert emitted[-1][0] == "finished"
        finally:
            router_registry.routers.pop("recording-router", None)


class TestExplicitOptimizerChain:
    def test_suppresses_embedded_simplify_pass(self, qtbot) -> None:
        rec = _RecordingRouter()
        router_registry.register(rec)
        try:
            req = _empty_request(enable_simplify_pass=True)
            worker, emitted = _make_worker(
                qtbot,
                request=req, router_name="recording-router", optimizer_names=[],
            )
            worker.run()
            # The router sees a request with enable_simplify_pass forced to False.
            assert rec.last_request is not req
            assert rec.last_request.routing_settings.enable_simplify_pass is False
            assert emitted[-1][0] == "finished"
        finally:
            router_registry.routers.pop("recording-router", None)

    def test_optimizers_run_in_order(self, qtbot) -> None:
        rec = _RecordingRouter()
        a = _StubOptimizer("opt-a")
        b = _StubOptimizer("opt-b")
        router_registry.register(rec)
        optimizer_registry.register(a)
        optimizer_registry.register(b)
        try:
            req = _empty_request(enable_simplify_pass=False)
            worker, emitted = _make_worker(
                qtbot,
                request=req, router_name="recording-router",
                optimizer_names=["opt-a", "opt-b"],
            )
            worker.run()
            assert a.calls == 1
            assert b.calls == 1
            kind, result = emitted[-1]
            assert kind == "finished"
            # Each optimizer appended its name → chain ordering captured.
            assert result.unrouted_nets == ["opt-a", "opt-b"]
        finally:
            router_registry.routers.pop("recording-router", None)
            optimizer_registry.optimizers.pop("opt-a", None)
            optimizer_registry.optimizers.pop("opt-b", None)

    def test_unknown_optimizer_is_skipped_with_warning(self, qtbot) -> None:
        """Missing optimizer name doesn't crash — others still run."""
        rec = _RecordingRouter()
        a = _StubOptimizer("opt-a")
        router_registry.register(rec)
        optimizer_registry.register(a)
        try:
            req = _empty_request(enable_simplify_pass=False)
            worker, emitted = _make_worker(
                qtbot,
                request=req, router_name="recording-router",
                optimizer_names=["nonexistent", "opt-a"],
            )
            worker.run()
            assert a.calls == 1
            kind, result = emitted[-1]
            assert kind == "finished"
            assert result.unrouted_nets == ["opt-a"]
        finally:
            router_registry.routers.pop("recording-router", None)
            optimizer_registry.optimizers.pop("opt-a", None)

    def test_empty_optimizer_list_short_circuits(self, qtbot) -> None:
        """No optimizers ran, but the simplify-pass suppression still applied."""
        rec = _RecordingRouter()
        router_registry.register(rec)
        try:
            req = _empty_request(enable_simplify_pass=True)
            worker, emitted = _make_worker(
                qtbot,
                request=req, router_name="recording-router", optimizer_names=[],
            )
            worker.run()
            assert rec.calls == 1
            assert rec.last_request.routing_settings.enable_simplify_pass is False
            assert emitted[-1][0] == "finished"
        finally:
            router_registry.routers.pop("recording-router", None)


class TestCancellationBetweenOptimizers:
    def test_cancel_after_router_skips_optimizers(self, qtbot) -> None:
        rec = _RecordingRouter()
        a = _StubOptimizer("opt-a")
        b = _StubOptimizer("opt-b")
        router_registry.register(rec)
        optimizer_registry.register(a)
        optimizer_registry.register(b)
        try:
            req = _empty_request(enable_simplify_pass=False)
            worker, emitted = _make_worker(
                qtbot,
                request=req, router_name="recording-router",
                optimizer_names=["opt-a", "opt-b"],
            )
            # Pre-cancel — sink.cancelled() is True before the loop starts.
            worker._cancel_flag.set()
            worker.run()
            assert a.calls == 0
            assert b.calls == 0
            # AutorouteCancelled → empty AutorouteResult.
            kind, result = emitted[-1]
            assert kind == "finished"
            assert result.wires == []
        finally:
            router_registry.routers.pop("recording-router", None)
            optimizer_registry.optimizers.pop("opt-a", None)
            optimizer_registry.optimizers.pop("opt-b", None)
