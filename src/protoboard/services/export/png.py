# SPDX-License-Identifier: MIT
"""PNG board exporter — rasterises the board via paint_snapshot."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QRectF, QSize
from PySide6.QtGui import QImage, QPainter

from protoboard.domain.board.pad_state import PadIndex
from protoboard.domain.schematic.schematic import Schematic
from protoboard.controller.theme import DEFAULT_THEME, Theme
from protoboard.services.export.paint import paint_snapshot
from protoboard.services.export.snapshot import BoardSnapshot

if TYPE_CHECKING:
    from protoboard.domain.board.board import Board
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.layout.layout import Layout

# Default rasterisation: 40 px per raster unit ≈ 100 dpi at 2.54 mm pitch.
_DEFAULT_PIXELS_PER_RASTER = 40


def export_png(
    path: Path,
    board: "Board",
    layout: "Layout",
    components: dict[str, "ComponentDefinition"],
    instances: dict[uuid.UUID, "ComponentInstance"],
    schematic: "Schematic | None" = None,
    svg_scale: float = 10.0,         # kept for signature compatibility
    pixel_scale: int = 4,             # legacy: SVG units × pixel_scale
    theme: Theme = DEFAULT_THEME,
) -> None:
    """Rasterise the board into a PNG at *path*.

    Pixel density: the legacy contract ``svg_scale=10, pixel_scale=4``
    yielded 40 px per raster unit (≈100 dpi).  We honour that contract
    by computing ``pixels_per_raster = svg_scale * pixel_scale`` so
    existing callers see no behaviour change in the output size.
    """
    pixels_per_raster = max(1, int(round(svg_scale * pixel_scale)))
    sch = schematic if schematic is not None else Schematic(nets={})
    pad_index = PadIndex(
        board=board, layout=layout, schematic=sch,
        components=components, instances=instances,
    )
    families = getattr(theme, "package_families", None) or {}
    snapshot = BoardSnapshot(
        board=board, layout=layout,
        components=components, instances=instances,
        schematic=sch, pad_index=pad_index,
        package_families=families, theme=theme,
    )
    export_png_from_snapshot(
        path, snapshot, pixels_per_raster=pixels_per_raster,
    )


def export_png_from_snapshot(
    path: Path,
    snapshot: BoardSnapshot,
    *,
    pixels_per_raster: int = _DEFAULT_PIXELS_PER_RASTER,
) -> None:
    """Snapshot-driven PNG export."""
    b = snapshot.board.bounds
    # Board + 1-raster margin on each side.
    img_w = int(round((b.lx + 2) * pixels_per_raster))
    img_h = int(round((b.ly + 2) * pixels_per_raster))
    if img_w <= 0 or img_h <= 0:
        raise ValueError(f"degenerate board bounds: {b.lx}×{b.ly}")
    img = QImage(QSize(img_w, img_h), QImage.Format.Format_ARGB32)
    img.fill(0)  # transparent canvas
    painter = QPainter(img)
    try:
        paint_snapshot(painter, snapshot, QRectF(0, 0, img_w, img_h))
    finally:
        painter.end()
    # Let Qt infer the format from the ``.png`` extension.  Passing an explicit
    # format is fragile here: this PySide6's runtime rejects a ``bytes`` format
    # ("QImage.save called with wrong argument values") even though the type
    # stub asks for ``bytes`` — a stub-vs-runtime mismatch — so the safe call is
    # no format arg at all.  ``save`` returns False (does not raise) on failure.
    if not img.save(str(path)):
        raise OSError(f"failed to write PNG to {path}")


__all__ = ["export_png", "export_png_from_snapshot"]
