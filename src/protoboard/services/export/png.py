# SPDX-License-Identifier: MIT
"""PNG board exporter — rasterises the SVG output to a PNG image."""

from __future__ import annotations

import uuid
from pathlib import Path

from PySide6.QtCore import QByteArray, QSize
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

from protoboard.domain.board.board import Board
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.services.export.svg import export_svg

# Default rasterisation: 4 pixels per SVG user unit (1 raster ≈ 40 px at scale=10).
_DEFAULT_PIXEL_SCALE = 4


def export_png(
    path: Path,
    board: Board,
    layout: Layout,
    components: dict[str, ComponentDefinition],
    instances: dict[uuid.UUID, ComponentInstance],
    svg_scale: float = 10.0,
    pixel_scale: int = _DEFAULT_PIXEL_SCALE,
) -> None:
    """Write a rasterised PNG of the board top layer to *path*.

    *pixel_scale* controls how many physical pixels correspond to one SVG user
    unit (default 4).  At the default svg_scale=10 this gives 40 px per raster
    unit, which is roughly 100 dpi at 2.54 mm pitch.
    """
    svg_str = export_svg(board, layout, components, instances, scale=svg_scale)

    renderer = QSvgRenderer(QByteArray(svg_str.encode()))
    default_size = renderer.defaultSize()
    img_size = QSize(
        default_size.width() * pixel_scale,
        default_size.height() * pixel_scale,
    )

    image = QImage(img_size, QImage.Format.Format_ARGB32)
    image.fill(0)  # transparent

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter)
    painter.end()

    image.save(str(path), b"PNG")
