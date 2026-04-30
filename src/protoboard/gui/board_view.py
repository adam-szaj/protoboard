# SPDX-License-Identifier: MIT
"""BoardView — QGraphicsView showing the board with all layers."""

from __future__ import annotations

import math
import uuid

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QWheelEvent,
)
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView, QWidget

from protoboard.domain.board.boxes import StripAtom
from protoboard.domain.connectivity.graph import ConnectivityGraph
from protoboard.geometry import Point
from protoboard.gui.renderers.board_renderer import BOARD_BG, render_board
from protoboard.gui.renderers.component_renderer import render_components
from protoboard.gui.renderers.wire_renderer import render_ratsnest, render_wires
from protoboard.gui.session import EditorSession
from protoboard.services.routing.net_status import routed_net_ids

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
        self._panning = False           # middle-button or Ctrl+drag pan
        self._ctrl_panning = False      # True when panning via Ctrl+LeftButton
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
        render_wires(self._scene, self._session.layout.wires)
        # Compute net routing status for pin colouring.
        routed: set[uuid.UUID] | None = None
        if self._session.schematic.nets:
            graph = ConnectivityGraph(
                board=self._session.board,
                layout=self._session.layout,
                components=self._session.components,
                instances=self._session.instances,
            )
            routed = routed_net_ids(self._session.schematic, graph)
        render_components(
            self._scene,
            self._session.layout.placements,
            self._session.instances,
            self._session.components,
            schematic=self._session.schematic if self._session.schematic.nets else None,
            routed_nets=routed,
        )
        if self._session.schematic.nets:
            render_ratsnest(
                self._scene,
                self._session.schematic,
                self._session.layout.placements,
                self._session.instances,
                self._session.components,
                routed_nets=routed,
            )
        # Fit scene rect to board bounds.
        b = self._session.board.bounds
        self._scene.setSceneRect(QRectF(b.x - 1, b.y - 1, b.width + 2, b.height + 2))

    def select_component(self, comp_id: uuid.UUID) -> None:
        """Switch to SelectTool and select the given component."""
        from protoboard.gui.tools.select_tool import SelectTool

        if not isinstance(self._active_tool, SelectTool):
            self.set_tool(SelectTool())
        tool = self._active_tool
        if isinstance(tool, SelectTool):
            tool._select(comp_id)

    def snap(self, scene_pt: QPointF) -> Point:
        """Snap scene-space point to nearest pad or component pin center."""
        from protoboard.domain.layout.placement import pin_world_position

        best: Point | None = None
        best_d = math.inf

        def _check(px: float, py: float) -> None:
            nonlocal best, best_d
            dx = scene_pt.x() - px
            dy = scene_pt.y() - py
            d = dx * dx + dy * dy
            if d < best_d:
                best_d = d
                best = Point(px, py)

        # Board pads / strip holes.
        for atom, center in self._session.board.atoms():
            if isinstance(atom, StripAtom):
                n = int(atom.length) if isinstance(atom.length, int) else 1
                if atom.orientation == "horizontal":
                    start_x = center.x - n / 2.0 + 0.5
                    for i in range(n):
                        _check(start_x + i, center.y)
                else:
                    start_y = center.y - n / 2.0 + 0.5
                    for i in range(n):
                        _check(center.x, start_y + i)
            else:
                _check(center.x, center.y)

        # Component pin positions (placed instances).
        for comp_id, placement in self._session.layout.placements.items():
            inst = self._session.instances.get(comp_id)
            if inst is None:
                continue
            defn = self._session.components.get(inst.definition_ref)
            if defn is None:
                continue
            for n in range(defn.footprint.pin_count()):
                p = pin_world_position(defn.footprint, placement, n + 1)
                _check(p.x, p.y)

        return best if best is not None else Point(scene_pt.x(), scene_pt.y())

    # ------------------------------------------------------------------
    # Qt event handlers
    # ------------------------------------------------------------------

    def wheelEvent(self, event: QWheelEvent) -> None:
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        ctrl = event.modifiers() & Qt.KeyboardModifier.ControlModifier
        if event.button() == Qt.MouseButton.MiddleButton:
            self._panning = True
            self._pan_start = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif event.button() == Qt.MouseButton.LeftButton and ctrl:
            # Ctrl+LeftButton: check if tool wants to handle it first (component
            # drag); if the tool consumed it (returns True), skip view pan.
            scene_pt = self.mapToScene(event.pos())
            if self._active_tool is not None and self._active_tool.on_press(
                event, scene_pt
            ):
                return
            # No tool consumed it — pan the view.
            self._ctrl_panning = True
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
        if not self._panning and self._active_tool is not None:
            self._active_tool.on_move(event, scene_pt)
        elif not self._panning:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() in (
            Qt.MouseButton.MiddleButton,
            Qt.MouseButton.LeftButton,
        ) and self._panning:
            self._panning = False
            self._ctrl_panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
        elif self._active_tool is not None:
            scene_pt = self.mapToScene(event.pos())
            self._active_tool.on_release(event, scene_pt)
        else:
            super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape:
            if self._active_tool is not None:
                self._active_tool.on_key(event)
            self.escape_tool.emit()
            return
        # Arrow keys — scroll by one raster unit (only without modifier).
        if not event.modifiers() and key in (
            Qt.Key.Key_Left, Qt.Key.Key_Right,
            Qt.Key.Key_Up, Qt.Key.Key_Down,
        ):
            # Convert 1 raster unit to viewport pixels using current transform.
            px = max(int(self.transform().m11()), 1)
            py = max(int(self.transform().m22()), 1)
            if key == Qt.Key.Key_Left:
                self.horizontalScrollBar().setValue(
                    self.horizontalScrollBar().value() - px
                )
            elif key == Qt.Key.Key_Right:
                self.horizontalScrollBar().setValue(
                    self.horizontalScrollBar().value() + px
                )
            elif key == Qt.Key.Key_Up:
                self.verticalScrollBar().setValue(
                    self.verticalScrollBar().value() - py
                )
            elif key == Qt.Key.Key_Down:
                self.verticalScrollBar().setValue(
                    self.verticalScrollBar().value() + py
                )
            return
        if self._active_tool is not None:
            self._active_tool.on_key(event)
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if self._active_tool is not None:
            self._active_tool.on_key_release(event)
        super().keyReleaseEvent(event)

    def leaveEvent(self, event: object) -> None:
        """Reset cursor when mouse leaves the viewport."""
        self.setCursor(Qt.CursorShape.ArrowCursor)
        super().leaveEvent(event)  # type: ignore[arg-type]


class _Tool:
    """Base class for board-view interaction tools."""

    def activate(self, session: EditorSession, view: BoardView) -> None:
        self._session = session
        self._view = view

    def deactivate(self) -> None:
        pass

    def on_press(self, event: QMouseEvent, scene_pt: QPointF) -> bool:
        """Handle mouse press. Return True if the event was consumed."""
        return False

    def on_move(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        pass

    def on_release(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        pass

    def on_key(self, event: QKeyEvent) -> None:
        pass

    def on_key_release(self, event: QKeyEvent) -> None:
        pass

    def on_scene_cleared(self) -> None:
        """Called after scene.clear() so tool can drop stale item references."""
        pass
