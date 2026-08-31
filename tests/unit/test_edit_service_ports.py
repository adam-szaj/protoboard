# SPDX-License-Identifier: MIT
"""EditController delegates technical work through injected service ports."""

from __future__ import annotations

import ast
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest

from protoboard.controller.editing import EditController, EditingContext
from protoboard.controller.ports.drc import DRCIssue
from protoboard.controller.ports.edit_services import (
    AutoroutePort,
    BoardExportPort,
    DrcPort,
    ProjectStorePort,
)
from protoboard.controller.ports.routing import (
    OptimizerOption,
    PathfinderPort,
    RoutePath,
    RoutingServicePorts,
    UnweaveResult,
    WireOptimizerPort,
    WireSimplifierPort,
    WireUnweaverPort,
)
from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, PadLayers, SizePolicy
from protoboard.domain.layout.wire import Wire
from protoboard.domain.routing_settings import RoutingSettings
from protoboard.geometry import Point
from protoboard.headless_session import HeadlessSession
from protoboard.services.library.loader import ComponentLibrary


class _RecordingCommand:
    allowed_during_autoroute = True
    description = "recording command"

    def __init__(self) -> None:
        self.ran = False

    def apply(self, _state: object) -> None:
        self.ran = True

    def revert(self, _state: object) -> None:
        pass


class _RejectingCommand(_RecordingCommand):
    def apply(self, _state: object) -> None:
        from protoboard.domain.commands.base import CommandRejectedError

        raise CommandRejectedError("rejected")


def _controller_for(session: HeadlessSession) -> EditController:
    from protoboard.controller.ports.edit_services import EditServicePorts

    return EditController(
        cast(EditingContext, session),
        EditServicePorts(
            autoroute=cast(AutoroutePort, object()),
            drc=cast(DrcPort, object()),
            projects=cast(ProjectStorePort, object()),
            export=cast(BoardExportPort, object()),
            routing=_routing_ports(),
        ),
    )


def test_controller_execution_port_notifies_only_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    controller = _controller_for(session)
    notifications: list[None] = []
    monkeypatch.setattr(session, "notify_changed", lambda: notifications.append(None))

    command = _RecordingCommand()
    assert controller.execute_command(command)
    assert command.ran
    assert len(notifications) == 1

    silent = _RecordingCommand()
    assert controller.execute_command_silent(silent)
    assert silent.ran
    assert len(notifications) == 1

    assert not controller.execute_command(_RejectingCommand())
    assert len(notifications) == 1


def test_migrated_tools_do_not_execute_command_stack_directly() -> None:
    for filename in ("select_tool.py", "wire_edit_tool.py"):
        path = (
            Path(__file__).parents[2]
            / "src"
            / "protoboard"
            / "controller"
            / "tools"
            / filename
        )
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert not any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "stack"
            for node in ast.walk(tree)
        )


def _session() -> HeadlessSession:
    board = Board(
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
    return HeadlessSession(board=board, library=ComponentLibrary())


@dataclass
class _RouteResult:
    wires: list[Wire] = field(default_factory=list)


class _RecordingAutorouter:
    def __init__(self, result: _RouteResult) -> None:
        self.result = result
        self.kwargs: dict[str, object] | None = None

    def route(self, **kwargs: object) -> _RouteResult:
        self.kwargs = kwargs
        return self.result


class _RecordingPathfinder:
    def __init__(self, result: list[RoutePath]) -> None:
        self.result = result
        self.kwargs: dict[str, object] | None = None

    def find_paths(self, **kwargs: object) -> list[RoutePath]:
        self.kwargs = kwargs
        return self.result


class _RecordingDrc:
    def __init__(self, result: list[DRCIssue]) -> None:
        self.result = result
        self.kwargs: dict[str, object] | None = None

    def check(self, **kwargs: object) -> list[DRCIssue]:
        self.kwargs = kwargs
        return self.result


class _RecordingProjects:
    def __init__(self) -> None:
        self.directory: Path | None = None
        self.kwargs: dict[str, object] | None = None

    def save(self, directory: Path, **kwargs: object) -> None:
        self.directory = directory
        self.kwargs = kwargs


class _RecordingExport:
    def __init__(self) -> None:
        self.kind: str | None = None
        self.path: Path | None = None
        self.kwargs: dict[str, object] | None = None

    def export(self, kind: str, path: Path, **kwargs: object) -> None:
        self.kind = kind
        self.path = path
        self.kwargs = kwargs


def _routing_ports(
    pathfinder: PathfinderPort | None = None,
    optimizer: WireOptimizerPort | None = None,
    unweaver: WireUnweaverPort | None = None,
) -> RoutingServicePorts:
    return RoutingServicePorts(
        pathfinder=pathfinder or cast(PathfinderPort, object()),
        simplifier=cast(WireSimplifierPort, object()),
        optimizer=optimizer or cast(WireOptimizerPort, object()),
        unweaver=unweaver or cast(WireUnweaverPort, object()),
    )


class _RecordingOptimizer:
    def __init__(self, output: list[Wire]) -> None:
        self.output = output
        self.kwargs: dict[str, object] | None = None

    def options(self) -> list[OptimizerOption]:
        return [OptimizerOption("test", "Test optimizer", "notes")]

    def optimize(self, name: str, **kwargs: object) -> list[Wire]:
        self.kwargs = {"name": name, **kwargs}
        return self.output


class _RecordingUnweaver:
    def __init__(self, output: UnweaveResult) -> None:
        self.output = output
        self.kwargs: dict[str, object] | None = None

    def unweave(self, **kwargs: object) -> UnweaveResult:
        self.kwargs = kwargs
        return self.output


def test_optimizer_and_unweaver_receive_current_project_snapshot() -> None:
    from protoboard.controller.ports.edit_services import EditServicePorts

    session = _session()
    first = Wire(id=uuid.uuid4(), path=[Point(0.5, 0.5), Point(1.5, 0.5)])
    second = Wire(id=uuid.uuid4(), path=[Point(2.5, 0.5), Point(3.5, 0.5)])
    optimizer = _RecordingOptimizer([second, first])
    unweave_result = UnweaveResult((first, second), 2, 0)
    unweaver = _RecordingUnweaver(unweave_result)
    controller = EditController(
        cast(EditingContext, session),
        EditServicePorts(
            autoroute=cast(AutoroutePort, object()),
            drc=cast(DrcPort, object()),
            projects=cast(ProjectStorePort, object()),
            export=cast(BoardExportPort, object()),
            routing=_routing_ports(optimizer=optimizer, unweaver=unweaver),
        ),
    )

    assert controller.optimizer_options() == [
        OptimizerOption("test", "Test optimizer", "notes")
    ]
    assert controller.optimize_wires("test", [first, second]) == [second, first]
    assert optimizer.kwargs == {
        "name": "test",
        "board": session.board,
        "layout": session.layout,
        "schematic": session.schematic,
        "components": session.components,
        "instances": session.instances,
        "settings": session.routing_settings,
        "wires": [first, second],
    }

    assert controller.unweave_wires([first, second]) is unweave_result
    assert unweaver.kwargs == {
        "wires": [first, second],
        "schematic": session.schematic,
        "graph": session.connectivity_graph(),
        "allow_layer_change": session.routing_settings.allow_layer_change,
        "allow_side_flip": session.routing_settings.visual_unweave_side_flip,
    }


def test_find_paths_forwards_to_injected_routing_port() -> None:
    from types import SimpleNamespace

    from protoboard.controller.ports.edit_services import EditServicePorts
    from protoboard.domain.routing.cost_model import RoutingCostModel
    from protoboard.geometry import Point

    session = _session()
    source = Point(0.5, 0.5)
    target = Point(2.5, 0.5)
    net_id = uuid.uuid4()
    expected = cast(
        "list[RoutePath]",
        [SimpleNamespace(path=[(source, "top"), (target, "top")])],
    )
    pathfinder = _RecordingPathfinder(expected)
    controller = EditController(
        cast(EditingContext, session),
        EditServicePorts(
            autoroute=cast(AutoroutePort, object()),
            drc=cast(DrcPort, object()),
            projects=cast(ProjectStorePort, object()),
            export=cast(BoardExportPort, object()),
            routing=_routing_ports(pathfinder),
        ),
    )
    graph = session.connectivity_graph()
    costs = RoutingCostModel()

    actual = controller.find_paths(
        graph=graph,
        source=source,
        targets=[target],
        side="top",
        cost_model=costs,
        k=3,
        insulated=False,
        active_net_id=net_id,
    )

    assert actual is expected
    assert pathfinder.kwargs == {
        "graph": graph,
        "source": source,
        "targets": [target],
        "side": "top",
        "cost_model": costs,
        "k": 3,
        "insulated": False,
        "occupied_pads": None,
        "same_net_pads": None,
        "near_pin_pads": None,
        "near_pin_cost": 10.0,
        "bare_wire_segments": None,
        "plated_through_pads": None,
        "penalised_pads": None,
        "extra_budget_ratio": 0.5,
        "at_pad_wires": None,
        "all_segments": None,
        "active_net_id": net_id,
        "at_pad_nets": None,
        "same_net_pad_vertices": None,
        "foreign_wire_cells": None,
        "foreign_via_pads": None,
        "active_dominant_side": None,
    }


def test_autoroute_forwards_current_project_to_injected_port(tmp_path: Path) -> None:
    from protoboard.controller.ports.edit_services import EditServicePorts

    session = _session()
    result = _RouteResult()
    autorouter = _RecordingAutorouter(result)
    ports = EditServicePorts(
        autoroute=autorouter,
        drc=cast(DrcPort, object()),
        projects=cast(ProjectStorePort, object()),
        export=cast(BoardExportPort, object()),
        routing=_routing_ports(),
    )
    controller = EditController(cast(EditingContext, session), ports)
    settings = RoutingSettings()
    net_ids = {uuid.uuid4()}
    report_path = tmp_path / "route-report.json"

    actual = controller.autoroute(
        net_ids,
        settings=settings,
        report_path=report_path,
    )

    assert actual is result
    assert autorouter.kwargs == {
        "board": session.board,
        "layout": session.layout,
        "schematic": session.schematic,
        "components": session.components,
        "instances": session.instances,
        "settings": settings,
        "net_ids": net_ids,
        "report_path": report_path,
    }


def test_run_drc_forwards_current_connectivity_to_injected_port() -> None:
    from protoboard.controller.ports.edit_services import EditServicePorts

    session = _session()
    expected = [
        DRCIssue(
            severity="warning",
            code="test",
            message="test issue",
            locations=[],
            affected_nets=[],
        )
    ]
    drc = _RecordingDrc(expected)
    controller = EditController(
        cast(EditingContext, session),
        EditServicePorts(
            autoroute=cast(AutoroutePort, object()),
            drc=drc,
            projects=cast(ProjectStorePort, object()),
            export=cast(BoardExportPort, object()),
            routing=_routing_ports(),
        ),
    )

    actual = controller.run_drc()

    assert actual is expected
    assert drc.kwargs == {
        "board": session.board,
        "layout": session.layout,
        "schematic": session.schematic,
        "components": session.components,
        "instances": session.instances,
        "graph": session.connectivity_graph(),
        "axis_labels": session.axis_label_settings,
        "pad_index": session.pad_index(),
    }


def test_save_forwards_current_project_to_injected_port(tmp_path: Path) -> None:
    from protoboard.controller.ports.edit_services import EditServicePorts

    session = _session()
    projects = _RecordingProjects()
    controller = EditController(
        cast(EditingContext, session),
        EditServicePorts(
            autoroute=cast(AutoroutePort, object()),
            drc=cast(DrcPort, object()),
            projects=projects,
            export=cast(BoardExportPort, object()),
            routing=_routing_ports(),
        ),
    )
    directory = tmp_path / "project"

    controller.save(directory)

    assert projects.directory == directory
    assert projects.kwargs == {
        "board": session.board,
        "layout": session.layout,
        "schematic": session.schematic,
        "instances": session.instances,
    }


def test_export_forwards_current_render_state_to_injected_port(tmp_path: Path) -> None:
    from protoboard.controller.ports.edit_services import EditServicePorts

    session = _session()
    exporter = _RecordingExport()
    controller = EditController(
        cast(EditingContext, session),
        EditServicePorts(
            autoroute=cast(AutoroutePort, object()),
            drc=cast(DrcPort, object()),
            projects=cast(ProjectStorePort, object()),
            export=exporter,
            routing=_routing_ports(),
        ),
    )
    path = tmp_path / "board.svg"

    controller.export("svg", path)

    assert exporter.kind == "svg"
    assert exporter.path == path
    assert exporter.kwargs == {
        "board": session.board,
        "layout": session.layout,
        "components": session.components,
        "instances": session.instances,
        "schematic": session.schematic,
        "package_families": session.package_families,
    }
