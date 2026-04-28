# SPDX-License-Identifier: MIT
"""Select tool — click to select, drag to move, R to rotate, Del to delete."""

from __future__ import annotations

import dataclasses
import math
import uuid

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent, QPen
from PySide6.QtWidgets import QGraphicsRectItem

from protoboard.domain.commands.component_cmds import (
    MoveComponentCommand,
    RemoveComponentCommand,
)
from protoboard.domain.layout.placement import pin_world_position
from protoboard.gui.board_view import _Tool
from protoboard.gui.panels.properties_panel import PropertiesPanel

_SELECT_PEN = QPen(Qt.GlobalColor.cyan, 0.08)
_SELECT_PEN.setStyle(Qt.PenStyle.DashLine)

_HIT_RADIUS = 0.6


class SelectTool(_Tool):
    """Click to select a component; drag to move; R to rotate 90°; Del to delete."""

    def __init__(self) -> None:
        self._selected_id: uuid.UUID | None = None
        self._highlight: QGraphicsRectItem | None = None
        self._dragging = False
        self._drag_orig_pos: None | tuple[float, float] = None

    def deactivate(self) -> None:
        self._dragging = False
        self._drag_orig_pos = None
        self._deselect()

    def on_scene_cleared(self) -> None:
        # Drop the stale graphics item reference — scene.clear() destroyed it.
        # Keep _selected_id so that an in-progress drag can still commit.
        self._highlight = None

    def on_press(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        hit = self._hit_test(scene_pt)
        if hit is None:
            self._deselect()
            return
        self._select(hit)
        self._dragging = True
        placement = self._session.layout.placements.get(hit)
        if placement is not None:
            self._drag_orig_pos = (placement.position.x, placement.position.y)

    def on_move(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        if not self._dragging or self._selected_id is None:
            return
        snapped = self._view.snap(scene_pt)
        placement = self._session.layout.placements.get(self._selected_id)
        if placement is None:
            return
        self._session.layout.placements[self._selected_id] = dataclasses.replace(
            placement, position=snapped
        )
        self._session.layout_changed.emit()

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
        from protoboard.geometry import Point

        old_pos = Point(orig[0], orig[1])
        if placement.position == old_pos:
            return
        old_pl = dataclasses.replace(placement, position=old_pos)
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
        if self._selected_id is None:
            return
        if event.key() == Qt.Key.Key_R:
            self._rotate_selected()
        elif event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self._delete_selected()

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

    def _select(self, comp_id: uuid.UUID) -> None:
        self._deselect()
        self._selected_id = comp_id
        self._draw_highlight(comp_id)
        inst = self._session.instances.get(comp_id)
        placement = self._session.layout.placements.get(comp_id)
        if inst is None:
            return
        panel = self._props_panel()
        if panel is not None:
            panel.show_component(inst)
            if placement is not None:
                panel._rotation_combo.setCurrentIndex(placement.rotation // 90)

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
        margin = 0.45
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

    def _props_panel(self) -> PropertiesPanel | None:
        win = self._view.window()
        panel = getattr(win, "_props_panel", None)
        return panel if isinstance(panel, PropertiesPanel) else None
