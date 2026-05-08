# SPDX-License-Identifier: MIT
"""BoardView — QGraphicsView showing the board with all layers."""

from __future__ import annotations

import math
import time
import uuid

_DRAG_REFRESH_INTERVAL = 0.033  # 30 fps max during live drag

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
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
    spread_requested = Signal()  # canvas context menu → Spread all components

    def __init__(self, session: EditorSession, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._session = session
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setBackgroundBrush(QBrush(BOARD_BG))
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self._scale = _GRID_PX
        self.scale(_GRID_PX, _GRID_PX)
        self._panning = False
        self._ctrl_panning = False
        self._pan_start = QPointF()
        self._active_tool: _Tool | None = None
        self._permanent_tools: list[_Tool] = []  # always notified on scene_cleared
        # View state — camera side and visual transform are independent.
        self._camera_side: str = "top"           # "top" | "bottom"
        self._view_transform: str = "normal"     # "normal" | "mirror_h" | "mirror_v"
        self._layer_overrides: LayerOverrides = LayerOverrides()
        self._pad_positions: list[Point] = []
        # Active group for rendering context (set by SelectTool on navigation).
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        self._active_group_id: uuid.UUID = ROOT_GROUP_ID
        # Net highlight: when set, all other objects are dimmed to 0.2 alpha.
        self._highlighted_net_id: uuid.UUID | None = None
        # Theme — overridden by MainWindow after Preferences apply.
        from protoboard.gui.theme import Theme
        self._theme_override: Theme | None = None
        self._last_drag_refresh: float = 0.0
        # comp_id → QGraphicsItemGroup; populated by refresh(), used by drag.
        self._comp_groups: dict[uuid.UUID, object] = {}
        # comp_id → placement.position at last full refresh (drag delta origin).
        self._drag_orig_group_pos: dict[uuid.UUID, object] = {}
        session.layout_changed.connect(self.refresh)
        session.drag_moved.connect(self._on_drag_moved)

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

    def set_highlighted_net(self, net_id: uuid.UUID | None) -> None:
        """Highlight one net and dim everything else. None clears the dim."""
        self._highlighted_net_id = net_id
        self.refresh()

    def _on_drag_moved(self) -> None:
        """Live drag: move scene groups to current placement positions (no rebuild).

        Items inside each group are rendered at absolute world coordinates, so the
        group itself sits at (0,0).  We track the *last rendered* position per group
        and call moveBy(delta) when the placement changes — this translates all child
        items without rebuilding the scene.
        """
        from PySide6.QtWidgets import QGraphicsItemGroup
        placements = self._session.layout.placements
        for comp_id, grp in self._comp_groups.items():
            if not isinstance(grp, QGraphicsItemGroup):
                continue
            pl = placements.get(comp_id)
            orig = self._drag_orig_group_pos.get(comp_id)
            if pl is None or orig is None:
                continue
            # Delta from the position when items were baked into the scene.
            dx = pl.position.x - orig.x
            dy = pl.position.y - orig.y
            grp.setPos(dx, dy)

    @property
    def connectness_enabled(self) -> bool:
        """True when the Connectness layer is not explicitly hidden."""
        from protoboard.gui.layer_state import LayerVisibility
        return self._layer_overrides.connectness != LayerVisibility.HIDDEN

    def set_theme(self, theme: object) -> None:
        from protoboard.gui.theme import Theme
        if isinstance(theme, Theme):
            self._theme_override = theme
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
        # Remember the scene point currently at the viewport centre so we can
        # restore it after setTransform (which resets scroll position).
        vp = self.viewport()
        cx, cy = vp.width() / 2, vp.height() / 2
        center_scene = self.mapToScene(int(cx), int(cy))
        from PySide6.QtGui import QTransform
        self.setTransform(QTransform(sx * mag, 0.0, 0.0, sy * mag, 0.0, 0.0))
        # Re-centre on the same scene point.
        self.centerOn(center_scene)

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

    def fit_area(self) -> None:
        """Fit view to the active group's components; falls back to fit_board() for ROOT."""
        from protoboard.domain.layout.group import all_component_ids
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        from protoboard.domain.layout.placement import component_bbox

        session = self._session
        active = self._active_group_id

        import uuid as _uuid
        if active == ROOT_GROUP_ID:
            comp_ids: frozenset[_uuid.UUID] = frozenset(session.layout.placements.keys())
        else:
            comp_ids = all_component_ids(session.layout.groups, active)

        xs: list[float] = []
        ys: list[float] = []
        for cid in comp_ids:
            pl = session.layout.placements.get(cid)
            inst = session.instances.get(cid)
            if pl is None or inst is None:
                continue
            defn = session.components.get(inst.definition_ref)
            if defn is None:
                continue
            bbox = component_bbox(defn.footprint, pl, defn.component_class)
            xs += [bbox.x, bbox.x + bbox.width]
            ys += [bbox.y, bbox.y + bbox.height]

        if not xs:
            self.fit_board()
            return

        margin = 1.0
        self.fitInView(
            QRectF(min(xs) - margin, min(ys) - margin,
                   max(xs) - min(xs) + 2 * margin,
                   max(ys) - min(ys) + 2 * margin),
            Qt.AspectRatioMode.KeepAspectRatio,
        )

    def fit_selection(self) -> None:
        """Fit view to the bounding box of selected components and wires."""
        from protoboard.domain.layout.placement import component_bbox

        tool = self._active_tool
        sel_comp_ids: set = getattr(tool, "_selected_ids", set())
        sel_wire_ids: set = getattr(tool, "_selected_wire_ids", set())

        if not sel_comp_ids and not sel_wire_ids:
            self.fit_area()
            return

        session = self._session
        xs: list[float] = []
        ys: list[float] = []

        for cid in sel_comp_ids:
            pl = session.layout.placements.get(cid)
            inst = session.instances.get(cid)
            if pl is None or inst is None:
                continue
            defn = session.components.get(inst.definition_ref)
            if defn is None:
                continue
            bbox = component_bbox(defn.footprint, pl, defn.component_class)
            xs += [bbox.x, bbox.x + bbox.width]
            ys += [bbox.y, bbox.y + bbox.height]

        for wid in sel_wire_ids:
            wire = next((w for w in session.layout.wires if w.id == wid), None)
            if wire is None:
                continue
            for pt in wire.path:
                xs.append(pt.x)
                ys.append(pt.y)

        if not xs:
            self.fit_area()
            return

        margin = 1.0
        self.fitInView(
            QRectF(min(xs) - margin, min(ys) - margin,
                   max(xs) - min(xs) + 2 * margin,
                   max(ys) - min(ys) + 2 * margin),
            Qt.AspectRatioMode.KeepAspectRatio,
        )

    def set_session(self, session: EditorSession) -> None:
        """Replace the current session (used when opening a recent project)."""
        if self._active_tool is not None:
            self._active_tool.deactivate()
            self._active_tool = None
        self._session = session
        self._highlighted_net_id = None
        session.layout_changed.connect(self.refresh)
        session.drag_moved.connect(self._on_drag_moved)
        self.refresh()

    def set_tool(self, tool: _Tool | None) -> None:
        if self._active_tool is not None:
            self._active_tool.deactivate()
        self._active_tool = tool
        self._highlighted_net_id = None
        if tool is not None:
            tool.activate(self._session, self)
        self.refresh()

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
        # Notify active tool and permanent tools that scene was wiped.
        notified: set[int] = set()
        for tool in self._permanent_tools:
            tool.on_scene_cleared()
            notified.add(id(tool))
        if self._active_tool is not None and id(self._active_tool) not in notified:
            self._active_tool.on_scene_cleared()

        # Derive rendering context from camera side, view transform and tool.
        _base_early = self._theme_override if self._theme_override is not None else DEFAULT_THEME
        layers = layer_state_for_tool(
            self._active_tool,
            self._camera_side,  # type: ignore[arg-type]
            self._view_transform,  # type: ignore[arg-type]
            overrides=self._layer_overrides,
            theme=_base_early,
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
            highlighted_net_id=self._highlighted_net_id,
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
        _base = self._theme_override if self._theme_override is not None else DEFAULT_THEME
        _theme = _dc_replace(_base,
                             raster_pitch_mm=self._session.board.raster_pitch_mm,
                             body_types=self._session.body_types)
        # Snapshot positions so _on_drag_moved can compute deltas.
        self._drag_orig_group_pos = {
            cid: pl.position
            for cid, pl in self._session.layout.placements.items()
        }
        self._comp_groups = render_components(
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
            highlighted_net_id=self._highlighted_net_id,
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
                theme=_theme,
                active_group_id=self._active_group_id,
            )
        # Extend scene rect by 50% of board dimensions on each side so any point
        # on the board (including corners) can be scrolled to the viewport centre.
        b = self._session.board.bounds
        mx, my = b.width * 0.5, b.height * 0.5
        self._scene.setSceneRect(QRectF(b.x - mx, b.y - my, b.width + 2 * mx, b.height + 2 * my))

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
        """Snap scene-space point to nearest accessible pad or component pin.

        Pins belonging to child groups are only accessible if they are exported.
        """
        from protoboard.domain.layout.group import all_component_ids
        from protoboard.domain.layout.group import ExportedPin
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        from protoboard.domain.layout.placement import pin_world_position

        # Build set of component IDs that are locked inside child groups,
        # and their exported pins.
        groups = self._session.layout.groups
        active_group = groups.get(self._active_group_id)
        locked_comp_ids: set[uuid.UUID] = set()
        exported_pins: set[tuple[uuid.UUID, int]] = set()
        if active_group is not None:
            for child_gid in active_group.child_group_ids:
                child_group = groups.get(child_gid)
                if child_group is None:
                    continue
                for cid in all_component_ids(groups, child_gid):
                    locked_comp_ids.add(cid)
                for ep in child_group.exported_pins:
                    exported_pins.add((ep.component_id, ep.pin_number))

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

        # Board pads / strip holes — always accessible.
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

        # Component pin positions — skip locked pins unless exported.
        for comp_id, placement in self._session.layout.placements.items():
            inst = self._session.instances.get(comp_id)
            if inst is None:
                continue
            defn = self._session.components.get(inst.definition_ref)
            if defn is None:
                continue
            for n in range(defn.footprint.pin_count()):
                pin_num = n + 1
                if comp_id in locked_comp_ids and (comp_id, pin_num) not in exported_pins:
                    continue
                p = pin_world_position(defn.footprint, placement, pin_num)
                _check(p.x, p.y)

        return best if best is not None else Point(scene_pt.x(), scene_pt.y())

    def is_locked_pin(self, scene_pt: QPointF, tol: float = 0.6) -> bool:
        """Return True if *scene_pt* is near a pin locked inside a child group
        (i.e. not exported). Used by RouteTool to show a status message."""
        from protoboard.domain.layout.group import all_component_ids
        from protoboard.domain.layout.placement import pin_world_position
        groups = self._session.layout.groups
        active_group = groups.get(self._active_group_id)
        if active_group is None:
            return False
        locked_comp_ids: set[uuid.UUID] = set()
        exported_pins: set[tuple[uuid.UUID, int]] = set()
        for child_gid in active_group.child_group_ids:
            child_group = groups.get(child_gid)
            if child_group is None:
                continue
            for cid in all_component_ids(groups, child_gid):
                locked_comp_ids.add(cid)
            for ep in child_group.exported_pins:
                exported_pins.add((ep.component_id, ep.pin_number))
        if not locked_comp_ids:
            return False
        for comp_id in locked_comp_ids:
            placement = self._session.layout.placements.get(comp_id)
            inst = self._session.instances.get(comp_id)
            if placement is None or inst is None:
                continue
            defn = self._session.components.get(inst.definition_ref)
            if defn is None:
                continue
            for n in range(defn.footprint.pin_count()):
                pin_num = n + 1
                if (comp_id, pin_num) in exported_pins:
                    continue
                p = pin_world_position(defn.footprint, placement, pin_num)
                if (scene_pt.x() - p.x) ** 2 + (scene_pt.y() - p.y) ** 2 < tol ** 2:
                    return True
        return False

    def center_on_scene_point(self, scene_pt: QPointF) -> None:
        """Scroll the view so that scene_pt appears at the centre of the viewport."""
        vp = self.viewport()
        cx = vp.width() / 2.0
        cy = vp.height() / 2.0
        current = self.mapToScene(int(cx), int(cy))
        delta_scene = scene_pt - current
        scale = self.transform().m11()
        hbar = self.horizontalScrollBar()
        vbar = self.verticalScrollBar()
        hbar.setValue(hbar.value() + round(delta_scene.x() * scale))
        vbar.setValue(vbar.value() + round(delta_scene.y() * scale))

    def _show_canvas_menu(self, vp_pos: QPoint, scene_pt: QPointF) -> None:
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        act_center = menu.addAction("Wyśrodkuj widok tutaj")
        act_escape = menu.addAction("Anuluj narzędzie (Esc)")
        menu.addSeparator()
        act_spread = menu.addAction("Spread all components")
        act_spread.setEnabled(bool(self._session.layout.placements))
        chosen = menu.exec(self.mapToGlobal(vp_pos))
        if chosen is act_center:
            self.center_on_scene_point(scene_pt)
        elif chosen is act_escape:
            self.escape_tool.emit()
        elif chosen is act_spread:
            self.spread_requested.emit()

    # ------------------------------------------------------------------
    # Qt event handlers
    # ------------------------------------------------------------------

    def wheelEvent(self, event: QWheelEvent) -> None:
        factor = 1.1 if event.angleDelta().y() > 0 else 1.0 / 1.08
        cursor_vp = event.position().toPoint()
        old_scene = self.mapToScene(cursor_vp)
        self.scale(factor, factor)
        # After scale, mapToScene shifts because Qt rescaled around scene origin.
        # Compensate by scrolling so the scene point under the cursor stays fixed.
        # sceneRect is kept large enough that scrollbars always exist (no Qt
        # auto-centering that would make this correction arrive too late).
        new_scene = self.mapToScene(cursor_vp)
        delta_scene = new_scene - old_scene
        new_scale = self.transform().m11()
        hbar = self.horizontalScrollBar()
        vbar = self.verticalScrollBar()
        hbar.setValue(hbar.value() - round(delta_scene.x() * new_scale))
        vbar.setValue(vbar.value() - round(delta_scene.y() * new_scale))

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
                self._show_canvas_menu(event.pos(), scene_pt)
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
    """Return a mapping wire.id → net.id using ConnectivityGraph.

    Uses the cached graph so strips, bare-wire chains, and plated-through
    holes are all handled correctly — not just wires whose endpoints happen
    to land directly on a component pin.
    """
    from protoboard.domain.schematic.net import PinRef

    if not session.schematic.nets or not session.layout.wires:
        return {}

    graph = session.connectivity_graph()

    # canonical_key → net.id  (one net per electric set that has named pins)
    canonical_to_net: dict[object, uuid.UUID] = {}
    for net in session.schematic.nets.values():
        for ref in net.pins:
            ck = graph.node_for_pin(ref)
            if ck is not None:
                canonical_to_net[ck] = net.id

    result: dict[uuid.UUID, uuid.UUID] = {}
    for wire in session.layout.wires:
        # Insulated wires only make electrical contact at their two endpoints;
        # bare wires contact every pad they pass through.
        contact_pts = (
            ([wire.path[0], wire.path[-1]] if len(wire.path) >= 2 else wire.path)
            if wire.insulated
            else wire.path
        )
        for pt in contact_pts:
            ck = graph.node_for_pad(pt, wire.side)
            if ck is not None:
                net_id = canonical_to_net.get(ck)
                if net_id is not None:
                    result[wire.id] = net_id
                    break
    return result


class _Tool:
    """Base class for board-view interaction tools."""

    def activate(self, session: EditorSession, view: BoardView) -> None:
        self._session = session
        self._view = view

    def _theme(self) -> "Theme":
        from protoboard.gui.theme import DEFAULT_THEME, Theme
        t = getattr(self._view, "_theme_override", None)
        return t if isinstance(t, Theme) else DEFAULT_THEME

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
