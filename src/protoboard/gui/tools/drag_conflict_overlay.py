# SPDX-License-Identifier: MIT
"""Live drag conflict overlay — draws coloured outlines and pad markers during drag."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsRectItem, QGraphicsScene

if TYPE_CHECKING:
    from protoboard.domain.layout.pad_conflict import MoveConflictResult
    from protoboard.geometry import Point

_Z = 50.0
_OUTLINE_WIDTH = 0.12
_MARKER_RADIUS = 0.22

_COLOR_REMOVABLE = QColor(220, 180, 0, 200)   # yellow-amber
_COLOR_BLOCKED   = QColor(220, 50,  30, 220)  # red


class DragConflictOverlay:
    """Manages Qt scene items that give live conflict feedback during drag.

    Usage::

        overlay = DragConflictOverlay()
        # in on_move():
        overlay.update(scene, result, drag_bbox, blocking_pads)
        # in on_release() / deactivate():
        overlay.clear(scene)
    """

    def __init__(self) -> None:
        self._items: list[QGraphicsItem] = []
        self._last_snapped: Point | None = None

    # ------------------------------------------------------------------

    def needs_update(self, snapped: Point) -> bool:
        """Return True if snapped position changed since last update."""
        if self._last_snapped is None:
            return True
        return snapped.x != self._last_snapped.x or snapped.y != self._last_snapped.y

    def update(
        self,
        scene: QGraphicsScene,
        result: MoveConflictResult,
        drag_bbox: tuple[float, float, float, float] | None,
        snapped: Point,
    ) -> None:
        """Redraw overlay for current conflict result.

        *drag_bbox* is (x0, y0, x1, y1) in scene coordinates — bounding box of
        the dragged selection for drawing the outline.
        *blocking_pad_positions* are taken from result.blocking_pad_positions.
        """
        from protoboard.domain.layout.pad_conflict import MoveConflictSeverity

        self.clear(scene)
        self._last_snapped = snapped

        if result.severity == MoveConflictSeverity.CLEAR:
            return

        color = (
            _COLOR_REMOVABLE
            if result.severity == MoveConflictSeverity.REMOVABLE
            else _COLOR_BLOCKED
        )

        # Outline rectangle around dragged selection.
        if drag_bbox is not None:
            x0, y0, x1, y1 = drag_bbox
            pad = 0.2
            rect = QGraphicsRectItem(QRectF(x0 - pad, y0 - pad, (x1 - x0) + 2 * pad, (y1 - y0) + 2 * pad))
            pen = QPen(color, _OUTLINE_WIDTH)
            pen.setStyle(Qt.PenStyle.DashLine)
            rect.setPen(pen)
            rect.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            rect.setZValue(_Z)
            scene.addItem(rect)
            self._items.append(rect)

        # Crosses on blocking pad positions.
        if result.severity == MoveConflictSeverity.BLOCKED:
            for pad_pos in result.blocking_pad_positions:
                self._add_cross(scene, pad_pos.x, pad_pos.y, color)

    def clear(self, scene: QGraphicsScene) -> None:
        for item in self._items:
            scene.removeItem(item)
        self._items.clear()
        self._last_snapped = None

    # ------------------------------------------------------------------

    def _add_cross(
        self, scene: QGraphicsScene, cx: float, cy: float, color: QColor
    ) -> None:
        from PySide6.QtWidgets import QGraphicsLineItem

        r = _MARKER_RADIUS
        pen = QPen(color, 0.08)
        for x1, y1, x2, y2 in [
            (cx - r, cy - r, cx + r, cy + r),
            (cx - r, cy + r, cx + r, cy - r),
        ]:
            line = QGraphicsLineItem(x1, y1, x2, y2)
            line.setPen(pen)
            line.setZValue(_Z)
            scene.addItem(line)
            self._items.append(line)
