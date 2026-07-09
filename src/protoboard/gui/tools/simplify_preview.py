# SPDX-License-Identifier: MIT
"""``SimplifyPreview`` — SimplifyTool's draft overlay as a semantic object.

The qt-ports §3 pattern: the tool states "this simplified path is drafted"
and this object owns the dashed-polyline ``QGraphicsItem`` mechanism.  The
draft *state* (which wire, which path) stays on the tool; the preview only
carries the visible overlay.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

    from PySide6.QtWidgets import QGraphicsLineItem, QGraphicsScene

    from protoboard.geometry import Point
    from protoboard.gui.theme import Theme

_PREVIEW_WIDTH = 0.12


class SimplifyPreview(Protocol):
    """The dashed draft polyline SimplifyTool shows before applying."""

    def show_draft(self, path: list[Point]) -> None:
        """Draw the drafted path as a dashed overlay (replaces the previous)."""
        ...

    def clear_draft(self) -> None: ...

    def on_scene_cleared(self) -> None:
        """Drop stale item refs after ``scene.clear()`` (C++ objects gone)."""
        ...


class QtSimplifyPreview:
    """SimplifyPreview over ``QGraphicsScene`` items (built by SimplifyTool)."""

    def __init__(
        self,
        scene: Callable[[], QGraphicsScene],
        theme: Callable[[], Theme],
    ) -> None:
        self._scene = scene
        self._theme = theme
        self._items: list[QGraphicsLineItem] = []

    @property
    def items(self) -> list[QGraphicsLineItem]:
        """The live overlay items (presence assertions in tests)."""
        return self._items

    def show_draft(self, path: list[Point]) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor, QPen
        from PySide6.QtWidgets import QGraphicsLineItem

        self.clear_draft()
        scene = self._scene()
        if scene is None:
            return
        pen = QPen(QColor(self._theme().routing_preview_color), _PREVIEW_WIDTH)
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setCosmetic(False)
        for a, b in zip(path, path[1:]):
            line = QGraphicsLineItem(a.x, a.y, b.x, b.y)
            line.setPen(pen)
            line.setZValue(1000.0)
            scene.addItem(line)
            self._items.append(line)

    def clear_draft(self) -> None:
        scene = self._scene()
        for item in self._items:
            if scene is not None:
                try:
                    scene.removeItem(item)
                except RuntimeError:
                    pass  # C++ object already gone
        self._items.clear()

    def on_scene_cleared(self) -> None:
        self._items.clear()
