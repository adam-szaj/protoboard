# SPDX-License-Identifier: MIT
"""Route wire tool — click-click-...-Enter/Escape to finish a multi-segment wire."""

from __future__ import annotations

import uuid

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPen
from PySide6.QtWidgets import QGraphicsLineItem

from protoboard.domain.layout.wire import WireSegment
from protoboard.geometry import Point
from protoboard.gui.board_view import _Tool

_PREVIEW_COLOR = QColor(255, 100, 0, 180)
_PREVIEW_WIDTH = 0.12


class RouteTool(_Tool):
    """Multi-segment wire routing: click to add points, Enter/Esc to commit."""

    def __init__(self, insulated: bool = True) -> None:
        self._insulated = insulated
        self._points: list[Point] = []
        self._preview_lines: list[QGraphicsLineItem] = []

    def deactivate(self) -> None:
        self._cancel()

    def on_press(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        snapped = self._view.snap(scene_pt)
        self._points.append(snapped)
        self._update_preview()

    def on_move(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        if not self._points:
            return
        # Draw a live preview from the last committed point to the cursor.
        self._clear_preview_last()
        snapped = self._view.snap(scene_pt)
        last = self._points[-1]
        self._add_preview_line(last, snapped)

    def on_key(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._commit()
        elif event.key() == Qt.Key.Key_Escape:
            self._cancel()

    # ------------------------------------------------------------------

    def _commit(self) -> None:
        if len(self._points) < 2:
            self._cancel()
            return
        wire = WireSegment(
            id=uuid.uuid4(),
            path=list(self._points),
            insulated=self._insulated,
            side="top",
        )
        self._session.layout.wires.append(wire)
        self._session.layout_changed.emit()
        self._clear_all_preview()
        self._points.clear()
        self._view.refresh()

    def _cancel(self) -> None:
        self._clear_all_preview()
        self._points.clear()

    def _update_preview(self) -> None:
        self._clear_all_preview()
        pts = self._points
        for i in range(len(pts) - 1):
            self._add_preview_line(pts[i], pts[i + 1])

    def _add_preview_line(self, a: Point, b: Point) -> None:
        pen = QPen(_PREVIEW_COLOR, _PREVIEW_WIDTH)
        line = QGraphicsLineItem(a.x, a.y, b.x, b.y)
        line.setPen(pen)
        self._view.scene().addItem(line)
        self._preview_lines.append(line)

    def _clear_preview_last(self) -> None:
        if self._preview_lines:
            item = self._preview_lines.pop()
            self._view.scene().removeItem(item)

    def _clear_all_preview(self) -> None:
        for item in self._preview_lines:
            self._view.scene().removeItem(item)
        self._preview_lines.clear()
