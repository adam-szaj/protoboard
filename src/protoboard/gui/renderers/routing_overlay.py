# SPDX-License-Identifier: MIT
"""Routing hint overlay — renders top-K candidate paths in the scene."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import QGraphicsLineItem, QGraphicsScene

from protoboard.gui.theme import DEFAULT_THEME, Theme
from protoboard.services.routing.astar import RoutingResult


def render_routing_hints(
    scene: QGraphicsScene,
    results: list[RoutingResult],
    theme: Theme = DEFAULT_THEME,
) -> list[QGraphicsLineItem]:
    """Add path-hint items to the scene. Returns the added items for cleanup."""
    items: list[QGraphicsLineItem] = []
    for i, result in enumerate(results[:3]):
        r, g, b, a = theme.hint_colors[i % len(theme.hint_colors)]
        color = QColor(r, g, b, a)
        pen = QPen(color, theme.hint_width)
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        path = result.path
        for a_pt, b_pt in zip(path[:-1], path[1:]):
            line = QGraphicsLineItem(a_pt.x, a_pt.y, b_pt.x, b_pt.y)
            line.setPen(pen)
            scene.addItem(line)
            items.append(line)
    return items
