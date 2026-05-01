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
from protoboard.gui.layer_state import LayerState, layer_state_for_tool
from protoboard.gui.renderers.board_renderer import BOARD_BG, render_board
from protoboard.gui.renderers.component_renderer import render_components, render_groups
from protoboard.gui.renderers.wire_renderer import render_ratsnest, render_wires_with_nets
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
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self._scale = _GRID_PX
        self.scale(_GRID_PX, _GRID_PX)
        self._panning = False           # middle-button or Ctrl+drag pan
        self._ctrl_panning = False      # True when panning via Ctrl+LeftButton
        self._pan_start = QPointF()
        self._active_tool: _Tool | None = None
        self._mirrored: bool = False
        session.layout_changed.connect(self.refresh)
        self.refresh()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    @property
    def is_mirrored(self) -> bool:
        return self._mirrored

    def toggle_mirror(self) -> None:
        """Flip the view horizontally (mirror = looking from the bottom side).

        Camera side and active editing side are always in sync:
        mirrored → camera=bottom → RouteTool switches to bottom automatically.
        """
        self._mirrored = not self._mirrored
        t = self.transform()
        from PySide6.QtGui import QTransform
        self.setTransform(QTransform(-t.m11(), t.m12(), t.m21(), t.m22(), 0.0, 0.0))

        # Synchronise RouteTool._side with the new camera side.
        from protoboard.gui.tools.route_tool import RouteTool
        new_side = "bottom" if self._mirrored else "top"
        if isinstance(self._active_tool, RouteTool):
            self._active_tool._side = new_side  # type: ignore[assignment]
        # Notify main window to update its wire-side state and status bar.
        win = self.window()
        updater = getattr(win, "_set_wire_side", None)
        if updater is not None:
            updater(new_side)

        self.refresh()

    def fit_board(self) -> None:
        """Scale and center the view to fit the board bounds."""
        b = self._session.board.bounds
        self.fitInView(
            QRectF(b.x - 1, b.y - 1, b.width + 2, b.height + 2),
            Qt.AspectRatioMode.KeepAspectRatio,
        )

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

        # Derive rendering context from camera side and active tool.
        camera_side = "bottom" if self._mirrored else "top"
        layers = layer_state_for_tool(self._active_tool, camera_side)  # type: ignore[arg-type]

        render_board(self._scene, self._session.board, layers=layers)

        wire_net_map = _build_wire_net_map(self._session)
        net_color_map = {
            net_id: net.color
            for net_id, net in self._session.schematic.nets.items()
        }
        render_wires_with_nets(
            self._scene,
            self._session.layout.wires,
            wire_net_map,
            net_color_map,
            layers=layers,
        )
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
            layers=layers,
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
        if self._session.layout.groups:
            render_groups(
                self._scene,
                self._session.layout,
                self._session.layout.placements,
                self._session.instances,
                self._session.components,
            )
        # Fit scene rect to board bounds.
        b = self._session.board.bounds
        self._scene.setSceneRect(QRectF(b.x - 1, b.y - 1, b.width + 2, b.height + 2))

        # When mirrored, counter-transform all text items so labels stay readable.
        if self._mirrored:
            from PySide6.QtGui import QTransform
            from PySide6.QtWidgets import QGraphicsTextItem
            for item in self._scene.items():
                if isinstance(item, QGraphicsTextItem):
                    br = item.boundingRect()
                    item.setTransform(
                        QTransform().scale(-1.0, 1.0).translate(-br.width(), 0.0)
                    )

    def select_component(self, comp_id: uuid.UUID) -> None:
        """Switch to SelectTool and select the given component."""
        from protoboard.gui.tools.select_tool import SelectTool

        if not isinstance(self._active_tool, SelectTool):
            self.set_tool(SelectTool())
        tool = self._active_tool
        if isinstance(tool, SelectTool):
            tool._select_primary(comp_id)

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
        old_pos = self.mapToScene(event.position().toPoint())
        self.scale(factor, factor)
        new_pos = self.mapToScene(event.position().toPoint())
        delta = new_pos - old_pos
        self.translate(delta.x(), delta.y())

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        left = Qt.MouseButton.LeftButton
        if event.button() == left and self._active_tool is not None:
            scene_pt = self.mapToScene(event.pos())
            handler = getattr(self._active_tool, "on_double_press", None)
            if handler is not None:
                handler(scene_pt)

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
            scene_pt = self.mapToScene(event.pos())
            handler = getattr(self._active_tool, "on_right_press", None)
            consumed = handler(scene_pt) if handler is not None else False
            if not consumed:
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
        if event.button() == Qt.MouseButton.MiddleButton and self._panning:
            self._panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
        elif event.button() == Qt.MouseButton.LeftButton and self._ctrl_panning:
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
        # Forward to MainWindow so global shortcuts (S/W/C/H/F5/F6/Ctrl+0…) work
        # regardless of which widget has focus.  Keys handled exclusively by tools
        # (R, Del, A, Enter) are not claimed by MainWindow.keyPressEvent, so they
        # fall through to its super() call harmlessly.  If a new MainWindow handler
        # is ever added for a key that tools also use, the tool must call
        # event.accept() to prevent the double-dispatch from causing side-effects.
        self.window().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if self._active_tool is not None:
            self._active_tool.on_key_release(event)
        super().keyReleaseEvent(event)

    def leaveEvent(self, event: object) -> None:
        """Reset cursor when mouse leaves the viewport."""
        self.setCursor(Qt.CursorShape.ArrowCursor)
        super().leaveEvent(event)  # type: ignore[arg-type]


def _build_wire_net_map(session: EditorSession) -> dict[uuid.UUID, uuid.UUID]:
    """Return a mapping wire.id → net.id for every wire whose endpoint lies on
    a placed pin that belongs to a net."""
    from protoboard.domain.layout.placement import pin_world_position

    _EPS = 1e-4

    # Build point → net_id from placed pins.
    point_to_net: dict[tuple[float, float], uuid.UUID] = {}
    for net in session.schematic.nets.values():
        for ref in net.pins:
            pl = session.layout.placements.get(ref.component_id)
            inst = session.instances.get(ref.component_id)
            if pl is None or inst is None:
                continue
            defn = session.components.get(inst.definition_ref)
            if defn is None or ref.pin_number > defn.pin_count:
                continue
            p = pin_world_position(defn.footprint, pl, ref.pin_number)
            point_to_net[(round(p.x, 4), round(p.y, 4))] = net.id

    result: dict[uuid.UUID, uuid.UUID] = {}
    for wire in session.layout.wires:
        for pt in (wire.path[0], wire.path[-1]):
            key = (round(pt.x, 4), round(pt.y, 4))
            net_id = point_to_net.get(key)
            if net_id is not None:
                result[wire.id] = net_id
                break
    return result


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
