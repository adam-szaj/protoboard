# SPDX-License-Identifier: MIT
"""Multi-panel A4 PDF — board template for hand assembly.

Single A4 portrait page with four panels showing the board in:

    ┌─────────────────┬─────────────────┐
    │  Top            │  Top — Mirror H │
    ├─────────────────┼─────────────────┤
    │  Bottom         │  Bottom — Mirror H │
    └─────────────────┴─────────────────┘

Use case: print at 100% scale, cut out the panels.  The two unmirrored
panels go under the board for placing components (top side up); the
two mirrored panels go on top for soldering (bottom side up, as seen
from underneath after flipping the board).

Each panel is drawn at 1:1 mm when the board fits within the panel
bounds; otherwise it auto-scales down to fit (preserving aspect ratio).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QMarginsF, QRectF, QSizeF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QPageLayout,
    QPageSize,
    QPainter,
    QPen,
)
from PySide6.QtPrintSupport import QPrinter

from protoboard.services.export.paint import paint_snapshot
from protoboard.services.export.snapshot import BoardSnapshot, ViewTransform

if TYPE_CHECKING:
    pass

# A4 dimensions in millimetres (portrait).
_A4_W_MM = 210.0
_A4_H_MM = 297.0
# Outer page margin and gutter between panels.
_PAGE_MARGIN_MM = 10.0
_GUTTER_MM = 8.0
# Reserved space for each panel's label above it.
_LABEL_HEIGHT_MM = 6.0


def export_template_pdf(
    path: Path,
    snapshot: BoardSnapshot,
    *,
    raster_pitch_mm: float | None = None,
) -> None:
    """Write a 4-panel A4 template PDF to *path*.

    *snapshot* is built once with ``BoardSnapshot.from_session(session)``
    and used four times via :meth:`BoardSnapshot.with_view` for the
    different orientations.  Camera side determines which side of the
    board is in front; the mirror flag flips horizontally.
    """
    if raster_pitch_mm is None:
        raster_pitch_mm = snapshot.board.raster_pitch_mm

    panels: list[tuple[str, str, ViewTransform]] = [
        ("Top",                 "top",    "normal"),
        ("Top — Mirror H",      "top",    "mirror_h"),
        ("Bottom",              "bottom", "normal"),
        ("Bottom — Mirror H",   "bottom", "mirror_h"),
    ]

    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(str(path))
    page_size = QPageSize(
        QSizeF(_A4_W_MM, _A4_H_MM), QPageSize.Unit.Millimeter,
    )
    printer.setPageLayout(QPageLayout(
        page_size,
        QPageLayout.Orientation.Portrait,
        QMarginsF(0, 0, 0, 0),
        QPageLayout.Unit.Millimeter,
    ))

    painter = QPainter()
    painter.begin(printer)
    try:
        _draw_template_page(
            painter, printer, snapshot, panels,
            raster_pitch_mm=raster_pitch_mm,
        )
    finally:
        painter.end()


def _draw_template_page(
    painter: QPainter,
    printer: QPrinter,
    snapshot: BoardSnapshot,
    panels: list[tuple[str, str, ViewTransform]],
    *,
    raster_pitch_mm: float,
) -> None:
    """Lay out four panels on one A4 page."""
    page_rect = printer.pageRect(QPrinter.Unit.DevicePixel)
    page_w_px = page_rect.width()
    page_h_px = page_rect.height()
    px_per_mm = page_w_px / _A4_W_MM

    margin_px = _PAGE_MARGIN_MM * px_per_mm
    gutter_px = _GUTTER_MM * px_per_mm
    label_px = _LABEL_HEIGHT_MM * px_per_mm

    inner_w = page_w_px - 2 * margin_px - gutter_px
    inner_h = page_h_px - 2 * margin_px - gutter_px
    panel_w = inner_w / 2
    panel_h = inner_h / 2

    # Title font: small enough to fit comfortably in label_px.
    font = QFont("DejaVu Sans", 9)
    painter.setFont(font)

    # Subtle border around each panel for cutting alignment.
    border_pen = QPen(QColor("#888"))
    border_pen.setWidthF(0.3 * px_per_mm)

    # Board native size in mm (used to decide 1:1 vs auto-scale).
    b = snapshot.board.bounds
    board_w_mm = (b.width + 2) * raster_pitch_mm
    board_h_mm = (b.height + 2) * raster_pitch_mm

    for idx, (label, camera, transform) in enumerate(panels):
        col = idx % 2
        row = idx // 2
        x = margin_px + col * (panel_w + gutter_px)
        y = margin_px + row * (panel_h + gutter_px)

        # Label above the panel.
        painter.setPen(QColor("#222"))
        painter.drawText(
            QRectF(x, y, panel_w, label_px),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            label,
        )

        # Panel canvas (below label).
        canvas_rect = QRectF(
            x, y + label_px,
            panel_w, panel_h - label_px,
        )
        painter.setPen(border_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(canvas_rect)

        # Board target rect — 1:1 mm if it fits, else fit-to-canvas.
        board_w_px = board_w_mm * px_per_mm
        board_h_px = board_h_mm * px_per_mm
        if board_w_px <= canvas_rect.width() and board_h_px <= canvas_rect.height():
            # Centre the board in the canvas at 1:1 scale.
            ox = canvas_rect.x() + (canvas_rect.width() - board_w_px) / 2
            oy = canvas_rect.y() + (canvas_rect.height() - board_h_px) / 2
            target = QRectF(ox, oy, board_w_px, board_h_px)
        else:
            # Auto-scale to fit, preserving aspect.  ``paint_snapshot``
            # uses KeepAspectRatio so we just hand it the canvas.
            target = canvas_rect

        view_snap = snapshot.with_view(
            camera_side=camera,  # type: ignore[arg-type]
            view_transform=transform,
        )
        paint_snapshot(painter, view_snap, target)


__all__ = ["export_template_pdf"]
