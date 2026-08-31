# SPDX-License-Identifier: MIT
"""Ports for technical services invoked by editing use cases."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import Path

    from protoboard.controller.ports.drc import DRCIssue
    from protoboard.controller.ports.routing import RoutingServicePorts
    from protoboard.domain.axis_labels_settings import AxisLabelSettings
    from protoboard.domain.board.board import Board
    from protoboard.domain.board.pad_state import PadIndex
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.components.package import PackageFamily
    from protoboard.domain.connectivity.incremental import ConnectivityGraph
    from protoboard.domain.layout.layout import Layout
    from protoboard.domain.layout.wire import Wire
    from protoboard.domain.routing_settings import RoutingSettings
    from protoboard.domain.schematic.schematic import Schematic


class RouteResult(Protocol):
    @property
    def wires(self) -> Sequence[Wire]: ...


class AutoroutePort(Protocol):
    def route(
        self,
        *,
        board: Board,
        layout: Layout,
        schematic: Schematic,
        components: dict[str, ComponentDefinition],
        instances: dict[uuid.UUID, ComponentInstance],
        settings: RoutingSettings,
        net_ids: set[uuid.UUID] | None,
        report_path: Path | None,
    ) -> RouteResult: ...


class DrcPort(Protocol):
    def check(
        self,
        *,
        board: Board,
        layout: Layout,
        schematic: Schematic,
        components: dict[str, ComponentDefinition],
        instances: dict[uuid.UUID, ComponentInstance],
        graph: ConnectivityGraph,
        axis_labels: AxisLabelSettings,
        pad_index: PadIndex,
    ) -> list[DRCIssue]: ...


class ProjectStorePort(Protocol):
    def save(
        self,
        directory: Path,
        *,
        board: Board,
        layout: Layout,
        schematic: Schematic,
        instances: dict[uuid.UUID, ComponentInstance],
    ) -> None: ...


class BoardExportPort(Protocol):
    def export(
        self,
        kind: str,
        path: Path,
        *,
        board: Board,
        layout: Layout,
        components: dict[str, ComponentDefinition],
        instances: dict[uuid.UUID, ComponentInstance],
        schematic: Schematic,
        package_families: dict[str, PackageFamily],
    ) -> None: ...


@dataclass(frozen=True)
class EditServicePorts:
    autoroute: AutoroutePort
    drc: DrcPort
    projects: ProjectStorePort
    export: BoardExportPort
    routing: RoutingServicePorts
