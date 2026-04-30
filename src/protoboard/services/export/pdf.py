# SPDX-License-Identifier: MIT
"""PDF board exporter — print-ready 1:1 scale via Qt SVG→PDF pipeline."""

from __future__ import annotations

import uuid
from pathlib import Path

from PySide6.QtCore import QByteArray, QMarginsF, QSizeF
from PySide6.QtGui import QPageLayout, QPageSize, QPainter
from PySide6.QtPrintSupport import QPrinter
from PySide6.QtSvg import QSvgRenderer

from protoboard.domain.board.board import Board
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.services.export.svg import export_svg

# 1 raster unit = 2.54 mm (standard 0.1" pitch).
# SVG export uses scale=10 (10 SVG-px per raster).
# To get print-ready PDF we need SVG viewBox in mm, then Qt maps mm→pt.
_MM_PER_RASTER = 2.54


def export_pdf(
    path: Path,
    board: Board,
    layout: Layout,
    components: dict[str, ComponentDefinition],
    instances: dict[uuid.UUID, ComponentInstance],
    raster_pitch_mm: float = _MM_PER_RASTER,
) -> None:
    """Write a print-ready 1:1 PDF of the board top layer to *path*.

    Physical size: each raster unit = *raster_pitch_mm* mm (default 2.54 mm).
    The PDF page is sized exactly to the board bounds + 1-raster margin all around.
    """
    svg_str = export_svg(board, layout, components, instances)

    bounds = board.bounds
    # Board + 1-raster margin on each side (matches export_svg offset).
    board_w_raster = bounds.width + 2
    board_h_raster = bounds.height + 2

    # Physical size in mm.
    page_w_mm = board_w_raster * raster_pitch_mm
    page_h_mm = board_h_raster * raster_pitch_mm

    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(str(path))

    page_size = QPageSize(QSizeF(page_w_mm, page_h_mm), QPageSize.Unit.Millimeter)
    page_layout = QPageLayout(
        page_size,
        QPageLayout.Orientation.Portrait,
        QMarginsF(0, 0, 0, 0),
        QPageLayout.Unit.Millimeter,
    )
    printer.setPageLayout(page_layout)

    renderer = QSvgRenderer(QByteArray(svg_str.encode()))

    painter = QPainter()
    painter.begin(printer)
    # Map the full SVG onto the full printer page rect (device pixels).
    page_rect = printer.pageRect(QPrinter.Unit.DevicePixel)
    renderer.render(painter, page_rect)
    painter.end()
