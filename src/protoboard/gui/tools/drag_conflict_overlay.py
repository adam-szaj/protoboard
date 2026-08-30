# SPDX-License-Identifier: MIT
"""Live drag conflict overlay — draws coloured outlines and pad markers during drag."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPen
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsRectItem,
    QGraphicsScene,
)

if TYPE_CHECKING:
    from protoboard.domain.layout.pad_conflict import MoveConflictResult
    from protoboard.geometry import Point

_Z = 50.0
_OUTLINE_WIDTH = 0.12
_MARKER_RADIUS = 0.22

_COLOR_REMOVABLE = QColor(220, 180, 0, 200)  # yellow-amber
_COLOR_BLOCKED = QColor(220, 50, 30, 220)  # red


class DragConflictOverlay:
    """Manages Qt scene items that give live conflict feedback during drag.

    Remembers the last conflict result so it can be re-rendered after a
    scene rebuild (scene.clear() destroys all items).

    Usage::

        overlay = DragConflictOverlay()
        # in on_move():
        overlay.update(scene, result, drag_bbox, snapped)
        # after scene rebuild (on_scene_cleared):
        overlay.rerender(scene)
        # on deactivate / after successful move:
        overlay.reset(scene)
    """

    def __init__(self) -> None:
        self._items: list[QGraphicsItem] = []
        self._last_snapped: Point | None = None
        # Remembered for rerender after scene rebuild.
        self._last_result: MoveConflictResult | None = None
        self._last_bbox: tuple[float, float, float, float] | None = None

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
        """Redraw overlay for current conflict result."""
        self._drop_items(scene)
        self._last_snapped = snapped
        self._last_result = result
        self._last_bbox = drag_bbox
        self._render(scene, result, drag_bbox)

    def rerender(self, scene: QGraphicsScene | None) -> None:
        """Re-add overlay items after a scene rebuild. No-op if no result stored."""
        self._items.clear()  # old C++ objects are gone after scene.clear()
        if scene is not None and self._last_result is not None:
            self._render(scene, self._last_result, self._last_bbox)

    def reset(self, scene: QGraphicsScene) -> None:
        """Clear overlay and forget all state."""
        self._drop_items(scene)
        self._last_snapped = None
        self._last_result = None
        self._last_bbox = None

    # ------------------------------------------------------------------

    def _drop_items(self, scene: QGraphicsScene) -> None:
        for item in self._items:
            try:
                scene.removeItem(item)
            except RuntimeError:
                pass  # C++ object already deleted by scene.clear()
        self._items.clear()

    def _render(
        self,
        scene: QGraphicsScene,
        result: MoveConflictResult,
        drag_bbox: tuple[float, float, float, float] | None,
    ) -> None:
        from protoboard.domain.layout.pad_conflict import MoveConflictSeverity

        if result.severity == MoveConflictSeverity.CLEAR:
            return

        color = (
            _COLOR_REMOVABLE
            if result.severity == MoveConflictSeverity.REMOVABLE
            else _COLOR_BLOCKED
        )

        if drag_bbox is not None:
            x0, y0, x1, y1 = drag_bbox
            pad = 0.2
            rect = QGraphicsRectItem(
                QRectF(
                    x0 - pad,
                    y0 - pad,
                    (x1 - x0) + 2 * pad,
                    (y1 - y0) + 2 * pad,
                )
            )
            pen = QPen(color, _OUTLINE_WIDTH)
            pen.setStyle(Qt.PenStyle.DashLine)
            rect.setPen(pen)
            rect.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            rect.setZValue(_Z)
            scene.addItem(rect)
            self._items.append(rect)

        if result.severity == MoveConflictSeverity.BLOCKED:
            for pad_pos in result.blocking_pad_positions:
                self._add_cross(scene, pad_pos.x, pad_pos.y, color)

    def _add_cross(
        self, scene: QGraphicsScene, cx: float, cy: float, color: QColor
    ) -> None:
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
