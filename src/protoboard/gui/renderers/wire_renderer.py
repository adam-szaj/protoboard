# SPDX-License-Identifier: MIT
"""Renderer for wire segments."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import QGraphicsLineItem, QGraphicsScene

from protoboard.domain.layout.wire import WireSegment

WIRE_BARE_COLOR = QColor("#888888")
WIRE_INSULATED_COLORS = [
    QColor("#E03030"),  # red
    QColor("#3060E0"),  # blue
    QColor("#30A030"),  # green
    QColor("#E0A020"),  # yellow
    QColor("#A030A0"),  # purple
]
WIRE_WIDTH = 0.15


def render_wires(scene: QGraphicsScene, wires: list[WireSegment]) -> None:
    for wire in wires:
        color = _wire_color(wire)
        pen = QPen(color, WIRE_WIDTH)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pts = wire.path
        for i in range(len(pts) - 1):
            line = QGraphicsLineItem(pts[i].x, pts[i].y, pts[i + 1].x, pts[i + 1].y)
            line.setPen(pen)
            scene.addItem(line)


def _wire_color(wire: WireSegment) -> QColor:
    if not wire.insulated:
        return WIRE_BARE_COLOR
    if wire.color:
        return QColor(wire.color)
    # Cycle through palette based on object id for visual variety.
    idx = abs(hash(wire.id)) % len(WIRE_INSULATED_COLORS)
    return WIRE_INSULATED_COLORS[idx]
