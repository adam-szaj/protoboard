# SPDX-License-Identifier: MIT
"""Select tool — click to select, drag to move, R to rotate, Del to delete.

Wire editing:
  After selecting a wire, Ctrl+click on a node (visible dot) marks the start
  of a segment edit.  Ctrl+click on a second node marks the end.  Then:
    - Press A to auto-route the fragment via A*.
    - Or click new intermediate points (like RouteTool) and press Enter.
  Esc cancels the edit and restores the original wire.
"""

from __future__ import annotations

import dataclasses
import math
import uuid

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPen
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsItemGroup,
    QGraphicsLineItem,
    QGraphicsRectItem,
)

from protoboard.domain.commands.component_cmds import (
    MoveComponentCommand,
    RemoveComponentCommand,
    RemoveWireCommand,
    ReplaceWireSegmentCommand,
)
from protoboard.domain.layout.placement import pin_world_position
from protoboard.domain.layout.wire import WireSegment
from protoboard.geometry import Point
from protoboard.gui.board_view import _Tool
from protoboard.gui.panels.properties_panel import PropertiesPanel

_SELECT_PEN = QPen(Qt.GlobalColor.cyan, 0.08)
_SELECT_PEN.setStyle(Qt.PenStyle.DashLine)
_WIRE_SELECT_PEN = QPen(Qt.GlobalColor.yellow, 0.12)
_NODE_PEN = QPen(Qt.GlobalColor.yellow, 0.04)
_NODE_BRUSH = QColor(255, 220, 0, 200)
_NODE_SELECTED_PEN = QPen(Qt.GlobalColor.red, 0.06)
_NODE_SELECTED_BRUSH = QColor(255, 80, 0, 230)
_EDIT_PREVIEW_PEN = QPen(QColor(255, 120, 0, 200), 0.12)
_EDIT_PREVIEW_PEN.setStyle(Qt.PenStyle.DashLine)

_HIT_RADIUS = 0.6
_BBOX_MARGIN = 0.45
_WIRE_HIT_DIST = 0.35
_NODE_HIT_RADIUS = 0.32  # radius for clicking on a wire node dot
_NODE_RADIUS = 0.13       # visual radius of node dot


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
        self._drag_offset: tuple[float, float] = (0.0, 0.0)
        # Wire selection state.
        self._selected_wire_id: uuid.UUID | None = None
        self._wire_highlight: QGraphicsItemGroup | None = None
        # Wire segment editing state.
        self._edit_start_idx: int | None = None   # index in wire.path
        self._edit_end_idx: int | None = None
        self._edit_points: list[Point] = []        # new intermediate points
        self._edit_preview_items: list[QGraphicsLineItem] = []
        self._edit_live_line: QGraphicsLineItem | None = None

    # ------------------------------------------------------------------
    # _Tool interface
    # ------------------------------------------------------------------

    def deactivate(self) -> None:
        self._dragging = False
        self._drag_orig_pos = None
        self._cancel_wire_edit()
        self._deselect_wire()

    def on_scene_cleared(self) -> None:
        self._highlight = None
        self._wire_highlight = None
        self._edit_preview_items.clear()
        self._edit_live_line = None
        if self._selected_id is not None and not self._dragging:
            self._draw_highlight(self._selected_id)
        if self._selected_wire_id is not None:
            self._draw_wire_highlight(self._selected_wire_id)

    def on_press(self, event: QMouseEvent, scene_pt: QPointF) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)

        # --- Wire edit mode: clicking adds intermediate points ---
        if self._is_editing_wire():
            if not ctrl:
                snapped = self._view.snap(scene_pt)
                self._edit_add_point(snapped)
                return True
            # Ctrl+click in edit mode = try to pick a different end node
            idx = self._hit_test_wire_node(scene_pt)
            if idx is not None and self._selected_wire_id is not None:
                wire = self._get_selected_wire()
                if wire is not None:
                    self._set_edit_end(idx, wire)
                return True
            return True

        # --- Ctrl+click: try component bbox drag first, then wire node ---
        if ctrl:
            # Check for wire node click (starts edit).
            if self._selected_wire_id is not None:
                idx = self._hit_test_wire_node(scene_pt)
                if idx is not None:
                    self._start_wire_edit(idx)
                    return True

            hit = self._hit_test_bbox(scene_pt)
            if hit is not None:
                self._deselect_wire()
                self._select(hit)
                self._dragging = True
                placement = self._session.layout.placements.get(hit)
                if placement is not None:
                    self._drag_orig_pos = (
                        placement.position.x, placement.position.y
                    )
                    snapped = self._view.snap(scene_pt)
                    self._drag_offset = (
                        placement.position.x - snapped.x,
                        placement.position.y - snapped.y,
                    )
                return True
            return False

        # --- Normal left click: component or wire ---
        hit = self._hit_test(scene_pt)
        if hit is not None:
            self._cancel_wire_edit()
            self._deselect_wire()
            self._select(hit)
            self._dragging = True
            placement = self._session.layout.placements.get(hit)
            if placement is not None:
                self._drag_orig_pos = (
                    placement.position.x, placement.position.y
                )
                snapped = self._view.snap(scene_pt)
                self._drag_offset = (
                    placement.position.x - snapped.x,
                    placement.position.y - snapped.y,
                )
            return True

        wire_hit = self._hit_test_wire(scene_pt)
        if wire_hit is not None:
            self._cancel_wire_edit()
            self._deselect()
            self._select_wire(wire_hit)
            return True

        if not ctrl:
            self._cancel_wire_edit()
            self._deselect()
            self._deselect_wire()
        return False

    def on_move(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)

        if self._is_editing_wire():
            # Show live preview line from last edit point (or start node) to cursor.
            self._update_edit_live(scene_pt)
            return

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
        self._select(comp_id)

    def on_key(self, event: QKeyEvent) -> None:
        key = event.key()

        if self._is_editing_wire():
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._commit_wire_edit()
            elif key == Qt.Key.Key_Escape:
                self._cancel_wire_edit()
            elif key == Qt.Key.Key_A:
                self._autoroute_wire_fragment()
            return

        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if self._selected_wire_id is not None:
                self._delete_wire()
            elif self._selected_id is not None:
                self._delete_selected()
        elif key == Qt.Key.Key_R and self._selected_id is not None:
            self._rotate_selected()
        elif key == Qt.Key.Key_Escape:
            self._deselect()
            self._deselect_wire()

    def on_key_release(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Control and not self._dragging:
            self._view.setCursor(Qt.CursorShape.ArrowCursor)

    # ------------------------------------------------------------------
    # Component selection / highlight
    # ------------------------------------------------------------------

    def _hit_test(self, scene_pt: QPointF) -> uuid.UUID | None:
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
            if (min(xs) - m <= px <= max(xs) + m
                    and min(ys) - m <= py <= max(ys) + m):
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
        self._cancel_wire_edit()
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
        group = QGraphicsItemGroup()
        pts = wire.path
        # Draw segments.
        for i in range(len(pts) - 1):
            seg = QGraphicsLineItem(
                pts[i].x, pts[i].y, pts[i + 1].x, pts[i + 1].y
            )
            seg.setPen(_WIRE_SELECT_PEN)
            group.addToGroup(seg)
        # Draw node dots at each vertex.
        for i, pt in enumerate(pts):
            r = _NODE_RADIUS
            is_sel = (
                i == self._edit_start_idx or i == self._edit_end_idx
            )
            dot = QGraphicsEllipseItem(
                pt.x - r, pt.y - r, 2 * r, 2 * r
            )
            pen = _NODE_SELECTED_PEN if is_sel else _NODE_PEN
            dot.setPen(pen)
            dot.setBrush(
                _NODE_SELECTED_BRUSH if is_sel else _NODE_BRUSH
            )
            group.addToGroup(dot)
        self._view.scene().addItem(group)
        self._wire_highlight = group

    def _delete_wire(self) -> None:
        wire_id = self._selected_wire_id
        if wire_id is None:
            return
        self._cancel_wire_edit()
        cmd = RemoveWireCommand(wire_id=wire_id)
        self._session.stack.execute(cmd, self._session)
        self._deselect_wire()
        self._session.layout_changed.emit()

    # ------------------------------------------------------------------
    # Wire segment editing
    # ------------------------------------------------------------------

    def _hit_test_wire_node(self, scene_pt: QPointF) -> int | None:
        """Return index of the wire node closest to scene_pt, or None."""
        wire = self._get_selected_wire()
        if wire is None:
            return None
        px, py = scene_pt.x(), scene_pt.y()
        best_i: int | None = None
        best_d = _NODE_HIT_RADIUS
        for i, pt in enumerate(wire.path):
            d = math.sqrt((px - pt.x) ** 2 + (py - pt.y) ** 2)
            if d < best_d:
                best_d = d
                best_i = i
        return best_i

    def _get_selected_wire(self) -> WireSegment | None:
        if self._selected_wire_id is None:
            return None
        return next(
            (w for w in self._session.layout.wires
             if w.id == self._selected_wire_id),
            None,
        )

    def _start_wire_edit(self, node_idx: int) -> None:
        """Mark node_idx as the start of a segment edit."""
        self._cancel_wire_edit()
        self._edit_start_idx = node_idx
        self._edit_end_idx = None
        self._edit_points = []
        # Redraw highlight to show selected node.
        if self._selected_wire_id is not None:
            self._draw_wire_highlight(self._selected_wire_id)

    def _set_edit_end(self, node_idx: int, wire: WireSegment) -> None:
        """Set the end node of the segment being edited."""
        start = self._edit_start_idx
        if start is None:
            return
        if node_idx == start:
            return
        # Ensure start < end for consistent slicing.
        if node_idx < start:
            start, node_idx = node_idx, start
            self._edit_start_idx = start
        self._edit_end_idx = node_idx
        self._edit_points = []
        self._draw_wire_highlight(wire.id)

    def _is_editing_wire(self) -> bool:
        return (
            self._edit_start_idx is not None
            and self._edit_end_idx is not None
        )

    def _edit_add_point(self, pt: Point) -> None:
        """Add intermediate point during manual edit."""
        self._edit_points.append(pt)
        # Draw committed preview segment.
        if len(self._edit_points) >= 2:
            a = self._edit_points[-2]
            b = self._edit_points[-1]
        else:
            wire = self._get_selected_wire()
            if wire is None or self._edit_start_idx is None:
                return
            a = wire.path[self._edit_start_idx]
            b = self._edit_points[0]
        line = QGraphicsLineItem(a.x, a.y, b.x, b.y)
        line.setPen(_EDIT_PREVIEW_PEN)
        self._view.scene().addItem(line)
        self._edit_preview_items.append(line)
        # Remove old live line; new one will be drawn in on_move.
        self._remove_edit_live()

    def _update_edit_live(self, scene_pt: QPointF) -> None:
        """Update the live (dashed) preview line to cursor."""
        self._remove_edit_live()
        wire = self._get_selected_wire()
        if wire is None or self._edit_start_idx is None:
            return
        snapped = self._view.snap(scene_pt)
        if self._edit_points:
            anchor = self._edit_points[-1]
        else:
            anchor = wire.path[self._edit_start_idx]
        line = QGraphicsLineItem(
            anchor.x, anchor.y, snapped.x, snapped.y
        )
        line.setPen(_EDIT_PREVIEW_PEN)
        self._view.scene().addItem(line)
        self._edit_live_line = line

    def _remove_edit_live(self) -> None:
        if self._edit_live_line is not None:
            self._view.scene().removeItem(self._edit_live_line)
            self._edit_live_line = None

    def _clear_edit_preview(self) -> None:
        for item in self._edit_preview_items:
            self._view.scene().removeItem(item)
        self._edit_preview_items.clear()
        self._remove_edit_live()

    def _cancel_wire_edit(self) -> None:
        self._clear_edit_preview()
        self._edit_start_idx = None
        self._edit_end_idx = None
        self._edit_points = []

    def _commit_wire_edit(self) -> None:
        """Replace the edited fragment with the new path and commit."""
        wire = self._get_selected_wire()
        if wire is None:
            return
        start = self._edit_start_idx
        end = self._edit_end_idx
        if start is None or end is None:
            return

        # Build new full path: prefix + new middle + suffix.
        prefix = list(wire.path[:start + 1])      # includes start node
        suffix = list(wire.path[end:])             # includes end node
        middle = list(self._edit_points)           # user-drawn intermediates

        new_path = prefix + middle + suffix
        # Remove duplicates at junctions (start/end node may equal first/last middle).
        new_path = _dedupe_adjacent(new_path)

        if len(new_path) < 2:
            self._cancel_wire_edit()
            return

        self._clear_edit_preview()
        self._cancel_wire_edit()

        cmd = ReplaceWireSegmentCommand(
            wire_id=wire.id,
            new_path=new_path,
        )
        self._session.stack.execute(cmd, self._session)
        self._session.layout_changed.emit()
        # Reselect the wire after scene refresh.
        self._select_wire(wire.id)

    def _autoroute_wire_fragment(self) -> None:
        """Auto-route the selected fragment via A* and immediately commit."""
        wire = self._get_selected_wire()
        if wire is None:
            return
        start = self._edit_start_idx
        end = self._edit_end_idx
        if start is None or end is None:
            return

        from protoboard.domain.connectivity.graph import ConnectivityGraph
        from protoboard.services.routing.astar import find_paths
        from protoboard.services.routing.cost_model import RoutingCostModel

        session = self._session
        graph = ConnectivityGraph(
            board=session.board,
            layout=session.layout,
            components=session.components,
            instances=session.instances,
        )
        source_pt = wire.path[start]
        target_pt = wire.path[end]

        results = find_paths(
            graph=graph,
            source=source_pt,
            targets=[target_pt],
            side=wire.side,
            cost_model=RoutingCostModel(),
            k=1,
        )
        if not results:
            return  # no path found — leave edit mode open

        routed_middle = results[0].path  # includes source and target
        # Strip source and target (already in prefix/suffix).
        middle = list(routed_middle[1:-1])

        prefix = list(wire.path[:start + 1])
        suffix = list(wire.path[end:])
        new_path = _dedupe_adjacent(prefix + middle + suffix)

        if len(new_path) < 2:
            self._cancel_wire_edit()
            return

        self._clear_edit_preview()
        self._cancel_wire_edit()

        cmd = ReplaceWireSegmentCommand(
            wire_id=wire.id,
            new_path=new_path,
        )
        self._session.stack.execute(cmd, self._session)
        self._session.layout_changed.emit()
        self._select_wire(wire.id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _props_panel(self) -> PropertiesPanel | None:
        win = self._view.window()
        panel = getattr(win, "_props_panel", None)
        return panel if isinstance(panel, PropertiesPanel) else None


# ---------------------------------------------------------------------------
# Wire distance helpers
# ---------------------------------------------------------------------------


def _min_dist_to_wire(px: float, py: float, wire: WireSegment) -> float:
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
    dx, dy = bx - ax, by - ay
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq < 1e-12:
        return math.sqrt((px - ax) ** 2 + (py - ay) ** 2)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_len_sq))
    cx, cy = ax + t * dx, ay + t * dy
    return math.sqrt((px - cx) ** 2 + (py - cy) ** 2)


def _dedupe_adjacent(path: list[Point]) -> list[Point]:
    """Remove consecutive duplicate points."""
    if not path:
        return path
    result = [path[0]]
    for pt in path[1:]:
        if abs(pt.x - result[-1].x) > 1e-9 or abs(pt.y - result[-1].y) > 1e-9:
            result.append(pt)
    return result
