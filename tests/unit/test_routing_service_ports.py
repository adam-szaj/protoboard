# SPDX-License-Identifier: MIT
"""Controller-owned routing DTO and service-bundle contracts."""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from protoboard.controller.ports.routing import (
    OptimizerOption,
    PathfinderPort,
    RoutingServicePorts,
    SimplifyInputs,
    SimplifyRequest,
    UnweaveResult,
    WireOptimizerPort,
    WireSimplifierPort,
    WireUnweaverPort,
)
from protoboard.domain.board.board import Board
from protoboard.domain.board.pad_state import PadIndex
from protoboard.domain.connectivity.incremental import ConnectivityGraph
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.wire import Wire
from protoboard.domain.routing.cost_model import RoutingCostModel
from protoboard.domain.routing.simplify_mode import SimplifyMode
from protoboard.domain.routing_settings import RoutingSettings
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from protoboard.services.editing.routing_adapters import (
    DefaultPathfinder,
    DefaultWireOptimizer,
    DefaultWireSimplifier,
    DefaultWireUnweaver,
)


def _wire(x: float) -> Wire:
    return Wire(
        id=uuid.uuid4(),
        path=[Point(x, 0.0), Point(x + 1.0, 0.0)],
    )


def test_simplify_request_preserves_wire_order_and_shared_inputs() -> None:
    first = _wire(0.0)
    second = _wire(2.0)
    third = _wire(4.0)
    inputs = SimplifyInputs(
        pin_positions=frozenset(),
        pad_to_net={},
        board_pads=frozenset(),
        wire_to_net={},
        pad_index=cast(PadIndex, object()),
    )

    request = inputs.request(
        first,
        [second, third],
        SimplifyMode.CHEBYSHEV,
    )

    assert isinstance(request, SimplifyRequest)
    assert request.wire is first
    assert request.other_wires == (second, third)
    assert request.inputs is inputs
    assert request.mode is SimplifyMode.CHEBYSHEV


def test_routing_result_values_preserve_declared_order() -> None:
    first = _wire(0.0)
    second = _wire(2.0)

    option = OptimizerOption(name="test", label="Test", notes="Notes")
    result = UnweaveResult(
        wires=(second, first),
        before_crossings=3,
        after_crossings=1,
    )

    assert option.name == "test"
    assert result.wires == (second, first)
    assert result.before_crossings == 3
    assert result.after_crossings == 1


def test_routing_service_bundle_keeps_each_injected_port() -> None:
    pathfinder = cast(PathfinderPort, object())
    simplifier = cast(WireSimplifierPort, object())
    optimizer = cast(WireOptimizerPort, object())
    unweaver = cast(WireUnweaverPort, object())

    ports = RoutingServicePorts(
        pathfinder=pathfinder,
        simplifier=simplifier,
        optimizer=optimizer,
        unweaver=unweaver,
    )

    assert ports.pathfinder is pathfinder
    assert ports.simplifier is simplifier
    assert ports.optimizer is optimizer
    assert ports.unweaver is unweaver


def test_controller_routing_ports_do_not_import_service_implementations() -> None:
    path = Path(__file__).parents[2] / "src/protoboard/controller/ports/routing.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    service_imports = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith("protoboard.services")
    ]

    assert service_imports == []


def test_default_pathfinder_delegates_and_preserves_result_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from protoboard.services.routing import astar

    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    expected: list[object] = [
        SimpleNamespace(path=[(Point(0.0, 0.0), "top")]),
        SimpleNamespace(path=[(Point(1.0, 0.0), "bottom")]),
    ]

    def _find_paths(*args: object, **kwargs: object) -> list[object]:
        calls.append((args, kwargs))
        return expected

    monkeypatch.setattr(astar, "find_paths", _find_paths)
    graph = object()
    target = Point(2.0, 0.0)
    net_id = uuid.uuid4()

    actual = DefaultPathfinder().find_paths(
        graph=cast(ConnectivityGraph, graph),
        source=Point(0.0, 0.0),
        targets=[target],
        side="top",
        cost_model=RoutingCostModel(),
        k=2,
        active_net_id=net_id,
    )

    assert actual is expected
    assert calls[0][0][0] is graph
    assert calls[0][0][2] == [target]
    assert calls[0][0][5] == 2
    assert calls[0][1]["active_net_id"] == net_id


def test_default_simplifier_translates_controller_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from protoboard.services.routing import postprocess

    wire = _wire(0.0)
    other = _wire(2.0)
    replacement = _wire(4.0)
    net_id = uuid.uuid4()
    pad = Point(0.0, 0.0)
    inputs = SimplifyInputs(
        pin_positions=frozenset({pad}),
        pad_to_net={pad: net_id},
        board_pads=frozenset({pad}),
        wire_to_net={wire.id: net_id, other.id: None},
        pad_index=cast(PadIndex, object()),
    )
    captured: dict[str, object] = {}

    def _simplify(*args: object, **kwargs: object) -> Wire:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return replacement

    monkeypatch.setattr(postprocess, "generic_simplify_wire", _simplify)

    actual = DefaultWireSimplifier().simplify(
        inputs.request(wire, [other], SimplifyMode.BARE_PARITY)
    )

    assert actual is replacement
    assert captured["args"] == (wire, [other], {pad}, {pad: net_id})
    assert cast(dict[str, object], captured["kwargs"])["wire_net_id"] == net_id


def test_default_optimizer_hides_registry_objects_and_preserves_wire_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import protoboard.services.routing.builtin_optimizers  # noqa: F401
    from protoboard.services.routing import optimizer as optimizer_module
    from protoboard.services.routing.autorouter import AutorouteResult
    from protoboard.services.routing.snapshot import RouteRequest

    first = _wire(0.0)
    second = _wire(2.0)
    request = object()
    received: dict[str, object] = {}

    class _Optimizer:
        display_name = "Test optimizer"

        def capabilities(self) -> object:
            return SimpleNamespace(notes="Test notes")

        def optimize(
            self, route_request: object, result: AutorouteResult, sink: object
        ) -> AutorouteResult:
            received["request"] = route_request
            received["wires"] = result.wires
            return AutorouteResult(wires=[second, first])

    class _Registry:
        def list_available(self) -> list[str]:
            return ["simplify", "test"]

        def get(self, name: str) -> _Optimizer:
            assert name == "test"
            return _Optimizer()

    monkeypatch.setattr(optimizer_module, "registry", _Registry())
    monkeypatch.setattr(
        RouteRequest,
        "from_session",
        classmethod(lambda cls, **kwargs: request),
    )
    adapter = DefaultWireOptimizer()

    options = adapter.options()
    output = adapter.optimize(
        "test",
        board=cast(Board, object()),
        layout=cast(Layout, object()),
        schematic=cast(Schematic, object()),
        components={},
        instances={},
        settings=cast(RoutingSettings, object()),
        wires=[first, second],
    )

    assert options == [OptimizerOption("test", "Test optimizer", "Test notes")]
    assert received == {"request": request, "wires": [first, second]}
    assert output == (second, first)


def test_default_unweaver_reports_crossings_around_service_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from protoboard.services.routing import unweave

    first = _wire(0.0)
    second = _wire(2.0)
    output = [second, first]
    calls: list[list[Wire]] = []

    def _count(wires: list[Wire]) -> int:
        calls.append(wires)
        return 4 if len(calls) == 1 else 1

    monkeypatch.setattr(unweave, "count_mutual_crossings", _count)
    monkeypatch.setattr(unweave, "unweave_wires", lambda wires, **kwargs: output)

    result = DefaultWireUnweaver().unweave(
        wires=[first, second],
        schematic=cast(Schematic, object()),
        graph=cast(ConnectivityGraph, object()),
        allow_layer_change=True,
        allow_side_flip=False,
    )

    assert result == UnweaveResult(tuple(output), 4, 1)
    assert calls == [[first, second], output]
