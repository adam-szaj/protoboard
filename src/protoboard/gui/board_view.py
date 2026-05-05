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
from protoboard.gui.layer_state import LayerOverrides, LayerState, layer_state_for_tool
from protoboard.gui.renderers.board_renderer import BOARD_BG, render_board
from protoboard.gui.renderers.component_renderer import render_components, render_groups
from protoboard.gui.renderers.wire_renderer import render_ratsnest, render_wires_with_nets
from protoboard.gui.session import EditorSession
from protoboard.gui.theme import DEFAULT_THEME
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
        self._panning = False
        self._ctrl_panning = False
        self._pan_start = QPointF()
        self._active_tool: _Tool | None = None
        # View state — camera side and visual transform are independent.
        self._camera_side: str = "top"           # "top" | "bottom"
        self._view_transform: str = "normal"     # "normal" | "mirror_h" | "mirror_v"
        self._layer_overrides: LayerOverrides = LayerOverrides()
        self._pad_positions: list[Point] = []
        # Active group for rendering context (set by SelectTool on navigation).
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        self._active_group_id: uuid.UUID = ROOT_GROUP_ID
        session.layout_changed.connect(self.refresh)
        self.refresh()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    @property
    def camera_side(self) -> str:
        return self._camera_side

    @property
    def view_transform(self) -> str:
        return self._view_transform

    @property
    def active_group_id(self) -> uuid.UUID:
        return self._active_group_id

    def set_active_group(self, group_id: uuid.UUID) -> None:
        """Called by SelectTool on group navigation to trigger a re-render."""
        self._active_group_id = group_id
        self.refresh()

    # ------------------------------------------------------------------
    # View control — camera and transform are independent.
    # ------------------------------------------------------------------

    def set_camera_side(self, side: str) -> None:
        """Switch between top and bottom camera.  Syncs tool side."""
        if side == self._camera_side:
            return
        self._camera_side = side
        self._sync_tool_side(side)
        self._apply_view_transform()
        self.refresh()

    def set_view_transform(self, transform: str) -> None:
        """Set the visual transform: 'normal', 'mirror_h', or 'mirror_v'."""
        if transform == self._view_transform:
            return
        self._view_transform = transform
        self._apply_view_transform()
        self.refresh()

    def _apply_view_transform(self) -> None:
        """Recompute the QGraphicsView scale matrix from current state."""
        sx = -1.0 if self._view_transform == "mirror_h" else 1.0
        sy = -1.0 if self._view_transform == "mirror_v" else 1.0
        # Preserve current zoom magnitude, apply sign flips.
        mag = abs(self.transform().m11()) or _GRID_PX
        from PySide6.QtGui import QTransform
        self.setTransform(QTransform(sx * mag, 0.0, 0.0, sy * mag, 0.0, 0.0))

    def _sync_tool_side(self, side: str) -> None:
        from protoboard.gui.tools.route_tool import RouteTool
        if isinstance(self._active_tool, RouteTool):
            self._active_tool._side = side  # type: ignore[assignment]
        win = self.window()
        updater = getattr(win, "_set_wire_side", None)
        if updater is not None:
            updater(side)

    # Convenience toggles (used by menu/keyboard).
    def toggle_mirror_h(self) -> None:
        self.set_view_transform(
            "normal" if self._view_transform == "mirror_h" else "mirror_h"
        )

    def toggle_mirror_v(self) -> None:
        self.set_view_transform(
            "normal" if self._view_transform == "mirror_v" else "mirror_v"
        )

    def toggle_camera(self) -> None:
        self.set_camera_side(
            "bottom" if self._camera_side == "top" else "top"
        )

    # Legacy alias used by MainWindow._mirror_view (now means Mirror H).
    def toggle_mirror(self) -> None:
        self.toggle_mirror_h()

    @property
    def is_mirrored(self) -> bool:
        return self._view_transform == "mirror_h"

    def fit_board(self) -> None:
        """Scale and center the view to fit the board bounds."""
        b = self._session.board.bounds
        self.fitInView(
            QRectF(b.x - 1, b.y - 1, b.width + 2, b.height + 2),
            Qt.AspectRatioMode.KeepAspectRatio,
        )

    def fit_board(self) -> None:
        """Scale and center the view to fit the board bounds."""
        b = self._session.board.bounds
        self.fitInView(
            QRectF(b.x - 1, b.y - 1, b.width + 2, b.height + 2),
            Qt.AspectRatioMode.KeepAspectRatio,
        )

    def set_session(self, session: EditorSession) -> None:
        """Replace the current session (used when opening a recent project)."""
        if self._active_tool is not None:
            self._active_tool.deactivate()
            self._active_tool = None
        self._session = session
        session.layout_changed.connect(self.refresh)
        self.refresh()

    def set_tool(self, tool: _Tool | None) -> None:
        if self._active_tool is not None:
            self._active_tool.deactivate()
        self._active_tool = tool
        if tool is not None:
            tool.activate(self._session, self)

    def board_pad_positions(self) -> list[Point]:
        """Cached list of all board pad positions (raster units)."""
        return self._pad_positions

    def _rebuild_pad_cache(self) -> None:
        """Rebuild _pad_positions from the current board layout."""
        positions: list[Point] = []
        for atom, center in self._session.board.atoms():
            if isinstance(atom, StripAtom):
                n = int(atom.length) if atom.length != "fill" else 0
                if atom.orientation == "horizontal":
                    start_x = center.x - n / 2.0 + 0.5
                    for i in range(n):
                        positions.append(Point(start_x + i, center.y))
                else:
                    start_y = center.y - n / 2.0 + 0.5
                    for i in range(n):
                        positions.append(Point(center.x, start_y + i))
            else:
                from protoboard.domain.board.boxes import GapAtom
                if not isinstance(atom, GapAtom):
                    positions.append(center)
        self._pad_positions = positions

    def refresh(self) -> None:
        """Rebuild the scene from the current session state."""
        self._rebuild_pad_cache()
        self._scene.clear()
        # Notify active tool that scene was wiped so it can drop stale item refs.
        if self._active_tool is not None:
            self._active_tool.on_scene_cleared()

        # Derive rendering context from camera side, view transform and tool.
        layers = layer_state_for_tool(
            self._active_tool,
            self._camera_side,  # type: ignore[arg-type]
            self._view_transform,  # type: ignore[arg-type]
            overrides=self._layer_overrides,
        )

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
        from dataclasses import replace as _dc_replace
        _theme = _dc_replace(DEFAULT_THEME,
                             raster_pitch_mm=self._session.board.raster_pitch_mm,
                             body_types=self._session.body_types)
        render_components(
            self._scene,
            self._session.layout.placements,
            self._session.instances,
            self._session.components,
            schematic=self._session.schematic if self._session.schematic.nets else None,
            routed_nets=routed,
            layers=layers,
            theme=_theme,
            active_group_id=self._active_group_id,
            layout=self._session.layout,
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
                active_group_id=self._active_group_id,
            )
        # Fit scene rect to board bounds.
        b = self._session.board.bounds
        self._scene.setSceneRect(QRectF(b.x - 1, b.y - 1, b.width + 2, b.height + 2))

        # When the scene is flipped, apply a counter-transform to text items so
        # labels remain readable.  Mirror H flips X → counter-scale X by -1.
        # Mirror V flips Y → counter-scale Y by -1.  Both may be combined.
        if layers.needs_text_counter_transform():
            from PySide6.QtGui import QTransform
            from PySide6.QtWidgets import QGraphicsTextItem
            sx = layers.scene_scale_x()   # ±1
            sy = layers.scene_scale_y()   # ±1
            for item in self._scene.items():
                if isinstance(item, QGraphicsTextItem):
                    br = item.boundingRect()
                    tx = -br.width() if sx < 0 else 0.0
                    ty = -br.height() if sy < 0 else 0.0
                    item.setTransform(
                        QTransform().scale(sx, sy).translate(tx, ty)
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
