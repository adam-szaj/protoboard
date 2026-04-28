# SPDX-License-Identifier: MIT
"""Routing hint overlay — renders top-K candidate paths in the scene."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import QGraphicsLineItem, QGraphicsScene

from protoboard.services.routing.astar import RoutingResult

# Three colours from best (green) to worst (orange).
_HINT_COLORS = [
    QColor(0, 200, 80, 200),  # green — cheapest
    QColor(220, 180, 0, 180),  # yellow
    QColor(220, 100, 0, 160),  # orange — most expensive of the three
]
_HINT_WIDTH = 0.10


def render_routing_hints(
    scene: QGraphicsScene,
    results: list[RoutingResult],
) -> list[QGraphicsLineItem]:
    """Add path-hint items to the scene. Returns the added items for cleanup."""
    items: list[QGraphicsLineItem] = []
    for i, result in enumerate(results[:3]):
        color = _HINT_COLORS[i % len(_HINT_COLORS)]
        pen = QPen(color, _HINT_WIDTH)
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        path = result.path
        for a, b in zip(path[:-1], path[1:]):
            line = QGraphicsLineItem(a.x, a.y, b.x, b.y)
            line.setPen(pen)
            scene.addItem(line)
            items.append(line)
    return items
