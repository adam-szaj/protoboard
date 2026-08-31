# SPDX-License-Identifier: MIT
"""PDF board exporter — print-ready 1:1 scale via paint_snapshot pipeline."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QMarginsF, QRectF, QSizeF
from PySide6.QtGui import QPageLayout, QPageSize, QPainter
from PySide6.QtPrintSupport import QPrinter

from protoboard.controller.view_options.theme import DEFAULT_THEME, Theme
from protoboard.domain.board.pad_state import PadIndex
from protoboard.domain.schematic.schematic import Schematic
from protoboard.services.export.paint import paint_snapshot
from protoboard.services.export.snapshot import BoardSnapshot

if TYPE_CHECKING:
    from protoboard.domain.board.board import Board
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.components.package.family import PackageFamily
    from protoboard.domain.layout.layout import Layout

# 1 raster unit = 2.54 mm (standard 0.1" pitch) — default.
_MM_PER_RASTER = 2.54


def export_pdf(
    path: Path,
    board: Board,
    layout: Layout,
    components: dict[str, ComponentDefinition],
    instances: dict[uuid.UUID, ComponentInstance],
    schematic: Schematic | None = None,
    raster_pitch_mm: float = _MM_PER_RASTER,
    theme: Theme = DEFAULT_THEME,
    package_families: dict[str, PackageFamily] | None = None,
) -> None:
    """Write a print-ready 1:1 PDF of the board top layer to *path*.

    Physical size: each raster unit = *raster_pitch_mm* mm (default 2.54).
    Page sized exactly to board bounds + 1-raster margin per side, so a
    direct print at 100% scale produces a 1:1 template.
    """
    sch = schematic if schematic is not None else Schematic(nets={})
    pad_index = PadIndex(
        board=board,
        layout=layout,
        schematic=sch,
        components=components,
        instances=instances,
    )
    families = (
        package_families
        if package_families is not None
        else getattr(theme, "package_families", None)
    ) or {}
    snapshot = BoardSnapshot(
        board=board,
        layout=layout,
        components=components,
        instances=instances,
        schematic=sch,
        pad_index=pad_index,
        package_families=families,
        theme=theme,
    )
    export_pdf_from_snapshot(path, snapshot, raster_pitch_mm=raster_pitch_mm)


def export_pdf_from_snapshot(
    path: Path,
    snapshot: BoardSnapshot,
    *,
    raster_pitch_mm: float = _MM_PER_RASTER,
) -> None:
    """Snapshot-driven PDF export (single page, 1:1 mm scale)."""
    b = snapshot.board.bounds
    # Board + 1-raster margin on each side.
    page_w_mm = (b.lx + 2) * raster_pitch_mm
    page_h_mm = (b.ly + 2) * raster_pitch_mm

    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(str(path))

    page_size = QPageSize(
        QSizeF(page_w_mm, page_h_mm),
        QPageSize.Unit.Millimeter,
    )
    page_layout = QPageLayout(
        page_size,
        QPageLayout.Orientation.Portrait,
        QMarginsF(0, 0, 0, 0),
        QPageLayout.Unit.Millimeter,
    )
    printer.setPageLayout(page_layout)

    painter = QPainter()
    painter.begin(printer)
    try:
        page_rect = printer.pageRect(QPrinter.Unit.DevicePixel)
        paint_snapshot(painter, snapshot, QRectF(page_rect))
    finally:
        painter.end()


__all__ = ["export_pdf", "export_pdf_from_snapshot"]
