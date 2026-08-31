# SPDX-License-Identifier: MIT
"""EditController delegates technical work through injected service ports."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from protoboard.controller.editing import EditController, EditingContext
from protoboard.controller.ports.drc import DRCIssue
from protoboard.controller.ports.edit_services import (
    AutoroutePort,
    BoardExportPort,
    DrcPort,
    ProjectStorePort,
)
from protoboard.controller.ports.routing import (
    PathfinderPort,
    RoutingServicePorts,
    WireOptimizerPort,
    WireSimplifierPort,
    WireUnweaverPort,
)
from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, PadLayers, SizePolicy
from protoboard.domain.layout.wire import Wire
from protoboard.domain.routing_settings import RoutingSettings
from protoboard.headless_session import HeadlessSession
from protoboard.services.library.loader import ComponentLibrary


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


def _routing_ports() -> RoutingServicePorts:
    return RoutingServicePorts(
        pathfinder=cast(PathfinderPort, object()),
        simplifier=cast(WireSimplifierPort, object()),
        optimizer=cast(WireOptimizerPort, object()),
        unweaver=cast(WireUnweaverPort, object()),
    )


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
