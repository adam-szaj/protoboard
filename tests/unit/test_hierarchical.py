# SPDX-License-Identifier: MIT
"""Contract tests for the group-aware routing orchestration."""

from __future__ import annotations

import threading
import uuid

import pytest

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.layout.group import ComponentGroup
from protoboard.domain.layout.layout import ROOT_GROUP_ID, Layout
from protoboard.domain.routing.cost_model import RoutingCostModel
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.services.routing import hierarchical as hierarchical_module
from protoboard.services.routing.results import AutorouteResult


def _board() -> Board:
    return Board(
        root=BoardGrid(
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
    )


def test_hierarchical_routing_routes_internal_nets_before_top_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_component = uuid.uuid4()
    outside_component = uuid.uuid4()
    child_net = Net(
        id=uuid.uuid4(),
        name="child-net",
        pins={PinRef(child_component, 1), PinRef(child_component, 2)},
    )
    cross_net = Net(
        id=uuid.uuid4(),
        name="cross-net",
        pins={PinRef(child_component, 1), PinRef(outside_component, 1)},
    )
    child_group_id = uuid.uuid4()
    layout = Layout(
        groups={
            ROOT_GROUP_ID: ComponentGroup(
                id=ROOT_GROUP_ID,
                name="Root",
                component_ids=frozenset({outside_component}),
                child_group_ids=frozenset({child_group_id}),
            ),
            child_group_id: ComponentGroup(
                id=child_group_id,
                name="Child",
                component_ids=frozenset({child_component}),
            ),
        }
    )
    calls: list[set[uuid.UUID]] = []
    progress: list[str] = []

    def recording_autoroute(**kwargs: object) -> AutorouteResult:
        net_ids = set(kwargs["net_ids"])  # type: ignore[arg-type]
        calls.append(net_ids)
        callback = kwargs["progress_callback"]
        if callback is not None:
            callback(1, "route", 100)  # type: ignore[operator]
        return AutorouteResult()

    monkeypatch.setattr(hierarchical_module, "autoroute", recording_autoroute)

    result = hierarchical_module.hierarchical_autoroute(
        board=_board(),
        layout=layout,
        schematic=Schematic(nets={child_net.id: child_net, cross_net.id: cross_net}),
        components={},
        instances={},
        cost_model=RoutingCostModel(),
        progress_callback=lambda _pass, label, _pct: progress.append(label),
    )

    assert calls == [{child_net.id}, {cross_net.id}]
    assert progress[0].startswith("[Child]")
    assert progress[1].startswith("[top]")
    assert result.wires == []
    assert layout.wires == []


def test_hierarchical_routing_honours_cancellation_before_first_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def unexpected_autoroute(**_kwargs: object) -> AutorouteResult:
        nonlocal calls
        calls += 1
        return AutorouteResult()

    monkeypatch.setattr(hierarchical_module, "autoroute", unexpected_autoroute)
    cancel_flag = threading.Event()
    cancel_flag.set()

    result = hierarchical_module.hierarchical_autoroute(
        board=_board(),
        layout=Layout(),
        schematic=Schematic(nets={}),
        components={},
        instances={},
        cost_model=RoutingCostModel(),
        cancel_flag=cancel_flag,
    )

    assert calls == 0
    assert result.wires == []
    assert result.unrouted_nets == []
