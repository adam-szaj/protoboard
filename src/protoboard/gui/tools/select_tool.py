# SPDX-License-Identifier: MIT
"""Select tool — click to select, drag to move, R to rotate, Del to delete."""

from __future__ import annotations

import dataclasses
import math
import uuid

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent, QPen
from PySide6.QtWidgets import QGraphicsLineItem, QGraphicsRectItem

from protoboard.domain.commands.component_cmds import (
    MoveComponentCommand,
    RemoveComponentCommand,
    RemoveWireCommand,
)
from protoboard.domain.layout.placement import pin_world_position
from protoboard.domain.layout.wire import WireSegment
from protoboard.geometry import Point
from protoboard.gui.board_view import _Tool
from protoboard.gui.panels.properties_panel import PropertiesPanel

_SELECT_PEN = QPen(Qt.GlobalColor.cyan, 0.08)
_SELECT_PEN.setStyle(Qt.PenStyle.DashLine)
_WIRE_SELECT_PEN = QPen(Qt.GlobalColor.yellow, 0.12)

_HIT_RADIUS = 0.6
_BBOX_MARGIN = 0.45  # same margin as _draw_highlight
_WIRE_HIT_DIST = 0.35  # max distance to select a wire segment


class SelectTool(_Tool):
    """Click to select a component; drag to move; R to rotate 90°; Del to delete.

    Ctrl+hover over a component changes cursor to SizeAll.
    Ctrl+click anywhere inside a component's bounding box starts a drag.
    """

    def __init__(self) -> None:
        self._selected_id: uuid.UUID | None = None
        self._highlight: QGraphicsRectItem | None = None
        self._dragging = False
        self._drag_orig_pos: None | tuple[float, float] = None
        # Offset from component position to click point (kept during drag).
        self._drag_offset: tuple[float, float] = (0.0, 0.0)
        # Wire selection state.
        self._selected_wire_id: uuid.UUID | None = None
        self._wire_highlight: QGraphicsLineItem | None = None

    def deactivate(self) -> None:
        self._dragging = False
        self._drag_orig_pos = None
        self._deselect_wire()

    def on_scene_cleared(self) -> None:
        # Drop stale graphics item references — scene.clear() destroyed them.
        self._highlight = None
        self._wire_highlight = None
        # Redraw highlights after scene is rebuilt.
        if self._selected_id is not None and not self._dragging:
            self._draw_highlight(self._selected_id)
        if self._selected_wire_id is not None:
            self._draw_wire_highlight(self._selected_wire_id)

    def on_press(self, event: QMouseEvent, scene_pt: QPointF) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        # With Ctrl: hit-test the full bbox (easier to grab).
        hit = (
            self._hit_test_bbox(scene_pt)
            if ctrl
            else self._hit_test(scene_pt)
        )
        if hit is not None:
            self._deselect_wire()
            self._select(hit)
            self._dragging = True
            placement = self._session.layout.placements.get(hit)
            if placement is not None:
                self._drag_orig_pos = (placement.position.x, placement.position.y)
                snapped = self._view.snap(scene_pt)
                self._drag_offset = (
                    placement.position.x - snapped.x,
                    placement.position.y - snapped.y,
                )
            return True  # consumed — no view pan

        # No component hit — try wire.
        wire_hit = self._hit_test_wire(scene_pt)
        if wire_hit is not None:
            self._deselect()
            self._select_wire(wire_hit)
            return True

        # Nothing hit.
        if not ctrl:
            self._deselect()
            self._deselect_wire()
        return False  # let board_view pan if Ctrl held

    def on_move(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        if self._dragging and self._selected_id is not None:
            snapped = self._view.snap(scene_pt)
            dx, dy = self._drag_offset
            new_pos = Point(snapped.x + dx, snapped.y + dy)
            placement = self._session.layout.placements.get(self._selected_id)
            if placement is not None:
                self._session.layout.placements[self._selected_id] = (
                    dataclasses.replace(placement, position=new_pos)
                )
                self._session.layout_changed.emit()
        elif ctrl:
            # Update cursor: SizeAll when hovering over a component bbox.
            hit = self._hit_test_bbox(scene_pt)
            if hit is not None:
                self._view.setCursor(Qt.CursorShape.SizeAllCursor)
            else:
                self._view.setCursor(Qt.CursorShape.ArrowCursor)

    def on_release(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        if not self._dragging or self._selected_id is None:
            self._dragging = False
            return
        self._dragging = False
        comp_id = self._selected_id
        orig = self._drag_orig_pos
        self._drag_orig_pos = None
        if orig is None:
            return
        placement = self._session.layout.placements.get(comp_id)
        if placement is None:
            return

        old_pos = Point(orig[0], orig[1])
        if placement.position == old_pos:
            return
        old_pl = dataclasses.replace(placement, position=old_pos)

        # Check pad conflicts at the new position before committing.
        inst = self._session.instances.get(comp_id)
        defn = self._session.components.get(inst.definition_ref) if inst else None
        if defn is not None:
            from protoboard.gui.placement_conflict import check_and_resolve

            resolved = check_and_resolve(
                session=self._session,
                placement=placement,
                defn=defn,
                instance_id=comp_id,
                parent_widget=self._view.window(),
            )
            if resolved is None:
                # Blocked — snap back to original position.
                self._session.layout.placements[comp_id] = old_pl
                self._session.layout_changed.emit()
                self._select(comp_id)
                return
            placement = resolved
            self._session.layout.placements[comp_id] = placement

        cmd = MoveComponentCommand(
            component_id=comp_id,
            new_placement=placement,
            old_placement=old_pl,
        )
        self._session.stack.execute(cmd, self._session)
        self._session.layout_changed.emit()
        # Restore selection after refresh wiped on_scene_cleared state.
        self._select(comp_id)

    def on_key(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if self._selected_wire_id is not None:
                self._delete_wire()
            elif self._selected_id is not None:
                self._delete_selected()
        elif event.key() == Qt.Key.Key_R and self._selected_id is not None:
            self._rotate_selected()

    def on_key_release(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Control and not self._dragging:
            self._view.setCursor(Qt.CursorShape.ArrowCursor)

    # ------------------------------------------------------------------

    def _hit_test(self, scene_pt: QPointF) -> uuid.UUID | None:
        """Hit-test by distance to nearest pin (for normal click)."""
        session = self._session
        best_id: uuid.UUID | None = None
        best_d = float("inf")
        for comp_id, placement in session.layout.placements.items():
            inst = session.instances.get(comp_id)
            if inst is None:
                continue
            defn = session.components.get(inst.definition_ref)
            if defn is None:
                continue
            for n in range(defn.footprint.pin_count()):
                pos = pin_world_position(defn.footprint, placement, n + 1)
                dx = scene_pt.x() - pos.x
                dy = scene_pt.y() - pos.y
                d = math.sqrt(dx * dx + dy * dy)
                if d < best_d:
                    best_d = d
                    best_id = comp_id
        return best_id if best_d <= _HIT_RADIUS else None

    def _hit_test_bbox(self, scene_pt: QPointF) -> uuid.UUID | None:
        """Hit-test by component bounding box (for Ctrl+click/hover)."""
        session = self._session
        px, py = scene_pt.x(), scene_pt.y()
        for comp_id, placement in session.layout.placements.items():
            inst = session.instances.get(comp_id)
            if inst is None:
                continue
            defn = session.components.get(inst.definition_ref)
            if defn is None:
                continue
            positions = [
                pin_world_position(defn.footprint, placement, n + 1)
                for n in range(defn.footprint.pin_count())
            ]
            if not positions:
                continue
            xs = [p.x for p in positions]
            ys = [p.y for p in positions]
            m = _BBOX_MARGIN
            if (min(xs) - m <= px <= max(xs) + m and
                    min(ys) - m <= py <= max(ys) + m):
                return comp_id
        return None

    def _select(self, comp_id: uuid.UUID) -> None:
        self._deselect()
        self._selected_id = comp_id
        self._draw_highlight(comp_id)
        inst = self._session.instances.get(comp_id)
        placement = self._session.layout.placements.get(comp_id)
        if inst is None:
            return
        defn = self._session.components.get(inst.definition_ref)
        panel = self._props_panel()
        if panel is not None:
            panel.show_component(inst, placement, defn)

    def _deselect(self) -> None:
        if self._highlight is not None:
            self._view.scene().removeItem(self._highlight)
            self._highlight = None
        self._selected_id = None
        panel = self._props_panel()
        if panel is not None:
            panel.clear_component()

    def _draw_highlight(self, comp_id: uuid.UUID) -> None:
        if self._highlight is not None:
            self._view.scene().removeItem(self._highlight)
            self._highlight = None
        session = self._session
        inst = session.instances.get(comp_id)
        placement = session.layout.placements.get(comp_id)
        if inst is None or placement is None:
            return
        defn = session.components.get(inst.definition_ref)
        if defn is None:
            return
        positions = [
            pin_world_position(defn.footprint, placement, n + 1)
            for n in range(defn.footprint.pin_count())
        ]
        if not positions:
            return
        margin = _BBOX_MARGIN
        xs = [p.x for p in positions]
        ys = [p.y for p in positions]
        x0 = min(xs) - margin
        y0 = min(ys) - margin
        w = max(xs) - min(xs) + 2 * margin
        h = max(ys) - min(ys) + 2 * margin
        self._highlight = QGraphicsRectItem(QRectF(x0, y0, w, h))
        self._highlight.setPen(_SELECT_PEN)
        self._highlight.setBrush(Qt.BrushStyle.NoBrush)
        self._view.scene().addItem(self._highlight)

    def _rotate_selected(self) -> None:
        comp_id = self._selected_id
        if comp_id is None:
            return
        placement = self._session.layout.placements.get(comp_id)
        if placement is None:
            return
        new_rot = (placement.rotation + 90) % 360
        new_pl = dataclasses.replace(placement, rotation=new_rot)
        cmd = MoveComponentCommand(
            component_id=comp_id,
            new_placement=new_pl,
            old_placement=placement,
        )
        self._session.stack.execute(cmd, self._session)
        self._session.layout_changed.emit()
        self._draw_highlight(comp_id)
        panel = self._props_panel()
        if panel is not None:
            panel._rotation_combo.setCurrentIndex(new_rot // 90)

    def _delete_selected(self) -> None:
        comp_id = self._selected_id
        if comp_id is None:
            return
        cmd = RemoveComponentCommand(component_id=comp_id)
        self._session.stack.execute(cmd, self._session)
        self._deselect()
        self._session.layout_changed.emit()

    # ------------------------------------------------------------------
    # Wire selection
    # ------------------------------------------------------------------

    def _hit_test_wire(self, scene_pt: QPointF) -> uuid.UUID | None:
        """Return the id of the closest wire within _WIRE_HIT_DIST, or None."""
        px, py = scene_pt.x(), scene_pt.y()
        best_id: uuid.UUID | None = None
        best_d = _WIRE_HIT_DIST
        for wire in self._session.layout.wires:
            d = _min_dist_to_wire(px, py, wire)
            if d < best_d:
                best_d = d
                best_id = wire.id
        return best_id

    def _select_wire(self, wire_id: uuid.UUID) -> None:
        self._deselect_wire()
        self._selected_wire_id = wire_id
        self._draw_wire_highlight(wire_id)

    def _deselect_wire(self) -> None:
        if self._wire_highlight is not None:
            self._view.scene().removeItem(self._wire_highlight)
            self._wire_highlight = None
        self._selected_wire_id = None

    def _draw_wire_highlight(self, wire_id: uuid.UUID) -> None:
        if self._wire_highlight is not None:
            self._view.scene().removeItem(self._wire_highlight)
            self._wire_highlight = None
        wire = next(
            (w for w in self._session.layout.wires if w.id == wire_id), None
        )
        if wire is None or len(wire.path) < 2:
            return
        # Draw highlighted polyline over the wire.
        from PySide6.QtWidgets import QGraphicsItemGroup
        group = QGraphicsItemGroup()
        pts = wire.path
        for i in range(len(pts) - 1):
            seg = QGraphicsLineItem(
                pts[i].x, pts[i].y, pts[i + 1].x, pts[i + 1].y
            )
            seg.setPen(_WIRE_SELECT_PEN)
            group.addToGroup(seg)
        self._view.scene().addItem(group)
        # Store group as _wire_highlight (typed as QGraphicsLineItem for simplicity,
        # but it's actually a QGraphicsItemGroup — removeItem still works).
        self._wire_highlight = group  # type: ignore[assignment]

    def _delete_wire(self) -> None:
        wire_id = self._selected_wire_id
        if wire_id is None:
            return
        cmd = RemoveWireCommand(wire_id=wire_id)
        self._session.stack.execute(cmd, self._session)
        self._deselect_wire()
        self._session.layout_changed.emit()

    def _props_panel(self) -> PropertiesPanel | None:
        win = self._view.window()
        panel = getattr(win, "_props_panel", None)
        return panel if isinstance(panel, PropertiesPanel) else None


# ---------------------------------------------------------------------------
# Wire distance helper
# ---------------------------------------------------------------------------


def _min_dist_to_wire(px: float, py: float, wire: WireSegment) -> float:
    """Minimum distance from point (px, py) to any segment of *wire*."""
    pts = wire.path
    best = math.inf
    for i in range(len(pts) - 1):
        d = _dist_point_to_segment(
            px, py, pts[i].x, pts[i].y, pts[i + 1].x, pts[i + 1].y
        )
        if d < best:
            best = d
    return best


def _dist_point_to_segment(
    px: float, py: float,
    ax: float, ay: float,
    bx: float, by: float,
) -> float:
    """Distance from point P to segment AB."""
    dx, dy = bx - ax, by - ay
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq < 1e-12:
        return math.sqrt((px - ax) ** 2 + (py - ay) ** 2)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_len_sq))
    cx, cy = ax + t * dx, ay + t * dy
    return math.sqrt((px - cx) ** 2 + (py - cy) ** 2)
