# SPDX-License-Identifier: MIT
"""Default technical-service adapters used by editing composition roots."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from protoboard.controller.ports.drc import DRCIssue
from protoboard.controller.ports.edit_services import EditServicePorts, RouteResult
from protoboard.services.drc.engine import DRCEngine

if TYPE_CHECKING:
    from protoboard.domain.axis_labels_settings import AxisLabelSettings
    from protoboard.domain.board.board import Board
    from protoboard.domain.board.pad_state import PadIndex
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.components.package import PackageFamily
    from protoboard.domain.connectivity.incremental import ConnectivityGraph
    from protoboard.domain.layout.layout import Layout
    from protoboard.domain.routing_settings import RoutingSettings
    from protoboard.domain.schematic.schematic import Schematic


class DefaultAutorouteAdapter:
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
    ) -> RouteResult:
        import protoboard.services.routing.builtin  # noqa: F401
        from protoboard.services.routing.protocol import ProgressSink, registry
        from protoboard.services.routing.snapshot import RouteRequest

        request = RouteRequest.from_session(
            board=board,
            layout=layout,
            schematic=schematic,
            components=components,
            instances=instances,
            routing_settings=settings,
            net_ids=net_ids,
            report_path=report_path,
        )
        return registry.default().route(request, ProgressSink())


class DefaultDrcAdapter:
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
    ) -> list[DRCIssue]:
        return DRCEngine().check_with_graph(
            schematic=schematic,
            layout=layout,
            components=components,
            instances=instances,
            graph=graph,
            axis_labels=axis_labels,
            board=board,
            pad_index=pad_index,
        )


class YamlProjectStore:
    def save(
        self,
        directory: Path,
        *,
        board: Board,
        layout: Layout,
        schematic: Schematic,
        instances: dict[uuid.UUID, ComponentInstance],
    ) -> None:
        from protoboard.services.persistence.project_yaml import save_project

        save_project(
            directory,
            board=board,
            layout=layout,
            schematic=schematic,
            instances=instances,
        )


class BoardExportAdapter:
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
    ) -> None:
        if kind == "pdf":
            from protoboard.services.export.pdf import export_pdf

            export_pdf(
                path,
                board,
                layout,
                components,
                instances,
                schematic,
                package_families=package_families,
            )
        elif kind == "png":
            from protoboard.services.export.png import export_png

            export_png(
                path,
                board,
                layout,
                components,
                instances,
                schematic,
                package_families=package_families,
            )
        elif kind == "svg":
            from protoboard.services.export.svg import export_svg

            path.write_text(
                export_svg(
                    board,
                    layout,
                    components,
                    instances,
                    schematic,
                    package_families=package_families,
                )
            )
        else:
            raise ValueError(f"unknown export kind: {kind!r}")


def default_edit_service_ports() -> EditServicePorts:
    from protoboard.services.editing.routing_adapters import (
        default_routing_service_ports,
    )

    return EditServicePorts(
        autoroute=DefaultAutorouteAdapter(),
        drc=DefaultDrcAdapter(),
        projects=YamlProjectStore(),
        export=BoardExportAdapter(),
        routing=default_routing_service_ports(),
    )
