# SPDX-License-Identifier: MIT
"""BoardView — QGraphicsView showing the board with all layers."""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QWheelEvent,
)
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView, QWidget

from protoboard.geometry import Point
from protoboard.gui.renderers.board_renderer import BOARD_BG, render_board, render_cuts
from protoboard.gui.renderers.component_renderer import render_components
from protoboard.gui.renderers.wire_renderer import render_wires
from protoboard.gui.session import EditorSession

# Scene units = raster units. 1 raster unit = 1 "pixel" in scene space.
_GRID_PX = 60.0  # initial pixels-per-raster-unit


class BoardView(QGraphicsView):
    """Main board canvas. Handles zoom/pan and forwards mouse events to tools."""

    cursor_moved = Signal(float, float)  # raster x, y of cursor
    pad_clicked = Signal(float, float)  # snapped raster x, y
    escape_tool = Signal()  # right-click or Esc — return to Select

    def __init__(self, session: EditorSession, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._session = session
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setBackgroundBrush(QBrush(BOARD_BG))
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self._scale = _GRID_PX
        self.scale(_GRID_PX, _GRID_PX)
        self._panning = False
        self._pan_start = QPointF()
        self._active_tool: _Tool | None = None
        session.layout_changed.connect(self.refresh)
        self.refresh()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def set_tool(self, tool: _Tool | None) -> None:
        if self._active_tool is not None:
            self._active_tool.deactivate()
        self._active_tool = tool
        if tool is not None:
            tool.activate(self._session, self)

    def refresh(self) -> None:
        """Rebuild the scene from the current session state."""
        self._scene.clear()
        # Notify active tool that scene was wiped so it can drop stale item refs.
        if self._active_tool is not None:
            self._active_tool.on_scene_cleared()
        render_board(self._scene, self._session.board)
        render_cuts(self._scene, self._session.layout.cuts, self._session.board)
        render_wires(self._scene, self._session.layout.wires)
        render_components(
            self._scene,
            self._session.layout.placements,
            self._session.instances,
            self._session.components,
        )
        # Fit scene rect to board bounds.
        b = self._session.board.bounds
        self._scene.setSceneRect(QRectF(b.x - 1, b.y - 1, b.width + 2, b.height + 2))

    def snap(self, scene_pt: QPointF) -> Point:
        """Snap scene-space point to nearest pad center."""
        board = self._session.board
        best: Point | None = None
        best_d = math.inf
        for _, pad_pos in board.atoms():
            dx = scene_pt.x() - pad_pos.x
            dy = scene_pt.y() - pad_pos.y
            d = dx * dx + dy * dy
            if d < best_d:
                best_d = d
                best = pad_pos
        return best if best is not None else Point(scene_pt.x(), scene_pt.y())

    # ------------------------------------------------------------------
    # Qt event handlers
    # ------------------------------------------------------------------

    def wheelEvent(self, event: QWheelEvent) -> None:
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            self._panning = True
            self._pan_start = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif event.button() == Qt.MouseButton.RightButton:
            self.escape_tool.emit()
        elif self._active_tool is not None:
            scene_pt = self.mapToScene(event.pos())
            self._active_tool.on_press(event, scene_pt)
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._panning:
            delta = event.position() - self._pan_start
            self._pan_start = event.position()
            self.horizontalScrollBar().setValue(
                int(self.horizontalScrollBar().value() - delta.x())
            )
            self.verticalScrollBar().setValue(
                int(self.verticalScrollBar().value() - delta.y())
            )
        scene_pt = self.mapToScene(event.pos())
        self.cursor_moved.emit(scene_pt.x(), scene_pt.y())
        if self._active_tool is not None:
            self._active_tool.on_move(event, scene_pt)
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            self._panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
        elif self._active_tool is not None:
            scene_pt = self.mapToScene(event.pos())
            self._active_tool.on_release(event, scene_pt)
        else:
            super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            # Let tool handle Esc first (e.g. RouteTool cancel), then go to Select.
            if self._active_tool is not None:
                self._active_tool.on_key(event)
            self.escape_tool.emit()
            return
        if self._active_tool is not None:
            self._active_tool.on_key(event)
        else:
            super().keyPressEvent(event)


class _Tool:
    """Base class for board-view interaction tools."""

    def activate(self, session: EditorSession, view: BoardView) -> None:
        self._session = session
        self._view = view

    def deactivate(self) -> None:
        pass

    def on_press(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        pass

    def on_move(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        pass

    def on_release(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        pass

    def on_key(self, event: QKeyEvent) -> None:
        pass

    def on_scene_cleared(self) -> None:
        """Called after scene.clear() so tool can drop stale item references."""
        pass
