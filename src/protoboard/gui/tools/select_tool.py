# SPDX-License-Identifier: MIT
"""Select tool — click to select, drag to move, R to rotate, Del to delete.

Multi-selection:
  Drag left-to-right (rubber band) = only components fully inside the rect.
  Drag right-to-left = all components whose bbox intersects the rect.
  Ctrl+click = add/remove one component to/from the selection.

Wire editing:
  After selecting a single wire, Ctrl+click on a node dot marks the edit
  start.  Ctrl+click on a second node marks the end.  Then:
    - Press A to auto-route the fragment via A*.
    - Or click new intermediate points and press Enter.
  Esc cancels the edit and restores the original wire.
"""

from __future__ import annotations

import math
import uuid

import structlog
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPen
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsItemGroup,
    QGraphicsLineItem,
    QGraphicsRectItem,
)

from protoboard.domain.commands.base import MacroCommand
from protoboard.domain.commands.component_cmds import (
    ComponentState,
    MoveComponentCommand,
    RemoveComponentCommand,
    RemoveWireCommand,
    ReplaceWireSegmentCommand,
)
from protoboard.domain.layout.group import (
    all_component_ids,
    top_level_group_for,
)
from protoboard.domain.layout.placement import component_bbox, flip_component, pin_world_position, unrotate
from protoboard.domain.layout.wire import WireSegment
from protoboard.geometry import Point
from protoboard.gui.board_view import _Tool
from protoboard.gui.panels.properties_panel import PropertiesPanel

_SELECT_PEN = QPen(Qt.GlobalColor.cyan, 0.08)
_SELECT_PEN.setStyle(Qt.PenStyle.DashLine)
_MULTI_SELECT_PEN = QPen(Qt.GlobalColor.cyan, 0.06)
_MULTI_SELECT_PEN.setStyle(Qt.PenStyle.DashLine)


def _make_select_pen(color_str: str, width: float) -> QPen:
    pen = QPen(QColor(color_str), width)
    pen.setStyle(Qt.PenStyle.DashLine)
    return pen
_RUBBER_BAND_PEN_LR = QPen(QColor(0, 200, 255, 180), 0.06)   # left-to-right: solid
_RUBBER_BAND_PEN_RL = QPen(QColor(0, 200, 255, 180), 0.06)   # right-to-left: dashed
_RUBBER_BAND_PEN_RL.setStyle(Qt.PenStyle.DashLine)
_RUBBER_BAND_FILL_LR = QColor(0, 200, 255, 20)
_RUBBER_BAND_FILL_RL = QColor(0, 200, 255, 10)
_WIRE_SELECT_PEN = QPen(QColor(255, 220, 0, 200), 0.38)
_WIRE_SELECT_PEN.setCapStyle(Qt.PenCapStyle.RoundCap)
_WIRE_SELECT_PEN.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
_NODE_PEN = QPen(Qt.GlobalColor.yellow, 0.04)
_NODE_BRUSH = QColor(255, 220, 0, 200)
_NODE_SELECTED_PEN = QPen(Qt.GlobalColor.red, 0.06)
_NODE_SELECTED_BRUSH = QColor(255, 80, 0, 230)
_EDIT_PREVIEW_PEN = QPen(QColor(255, 120, 0, 200), 0.12)
_EDIT_PREVIEW_PEN.setStyle(Qt.PenStyle.DashLine)

_HIT_RADIUS = 0.6
_BBOX_MARGIN = 0.45
_WIRE_HIT_DIST = 0.35
_NODE_HIT_RADIUS = 0.32
_NODE_RADIUS = 0.13

_log = structlog.get_logger(__name__)


class SelectTool(_Tool):
    """Click/rubber-band to select; drag to move; R to rotate; Del to delete."""

    def __init__(self) -> None:
        # Single component selection (for Properties panel and drag).
        self._selected_id: uuid.UUID | None = None
        self._highlight: QGraphicsRectItem | None = None
        self._origin_cross: list[QGraphicsLineItem] = []
        # Multi-selection.
        self._selected_ids: set[uuid.UUID] = set()
        self._multi_highlights: list[QGraphicsRectItem] = []
        # Drag state (works for both single and multi).
        self._dragging = False
        self._drag_orig_positions: dict[uuid.UUID, Point] = {}
        self._drag_orig_wire_paths: dict[uuid.UUID, list[Point]] = {}
        self._drag_anchor_offset: tuple[float, float] = (0.0, 0.0)
        # Rubber-band state.
        self._rubber_band_start: QPointF | None = None
        self._rubber_band_item: QGraphicsRectItem | None = None
        # Wire selection (single — for editing).
        self._selected_wire_id: uuid.UUID | None = None
        self._wire_highlight: QGraphicsItemGroup | None = None
        # Multi-wire selection.
        self._selected_wire_ids: set[uuid.UUID] = set()
        self._wire_multi_highlights: list[QGraphicsItemGroup] = []
        # Wire segment editing.
        self._edit_start_idx: int | None = None
        self._edit_end_idx: int | None = None
        self._edit_points: list[Point] = []
        self._edit_preview_items: list[QGraphicsLineItem] = []
        self._edit_live_line: QGraphicsLineItem | None = None
        # Group navigation stack: list of group IDs currently entered (innermost last).
        # Starts at root group; never empty.
        from protoboard.domain.layout.layout import ROOT_GROUP_ID as _ROOT_GID
        self._group_stack: list[uuid.UUID] = [_ROOT_GID]
        # Set when the user clicks a group outline — enables group-as-monolith rotation.
        self._selected_group_id: uuid.UUID | None = None
        # Re-drag state (Ctrl+R on a component with bendable_pins).
        self._redrag_comp_id: uuid.UUID | None = None
        self._redrag_pin_num: int | None = None
        self._redrag_items: list[QGraphicsItem] = []
        # Live drag conflict overlay.
        from protoboard.gui.tools.drag_conflict_overlay import DragConflictOverlay
        self._drag_overlay = DragConflictOverlay()

    # ------------------------------------------------------------------
    # _Tool interface
    # ------------------------------------------------------------------

    def activate(self, session: object, view: object) -> None:
        super().activate(session, view)  # type: ignore[arg-type]
        # Ensure group stack is never empty (guard against stale instances).
        if not self._group_stack:
            from protoboard.domain.layout.layout import ROOT_GROUP_ID
            self._group_stack = [ROOT_GROUP_ID]

    def deactivate(self) -> None:
        self._cancel_redrag()
        self._dragging = False
        self._drag_orig_positions = {}
        self._drag_orig_wire_paths = {}
        if self._view is not None:
            self._drag_overlay.reset(self._view.scene())
        self._cancel_wire_edit()
        self._deselect_wire()
        self._clear_multi_wire_selection()
        self._clear_rubber_band()

    def on_scene_cleared(self) -> None:
        self._highlight = None
        self._origin_cross.clear()
        self._multi_highlights.clear()
        self._wire_highlight = None
        self._wire_multi_highlights.clear()
        self._edit_preview_items.clear()
        self._edit_live_line = None
        self._rubber_band_item = None
        self._redrag_items = []  # scene wiped, items gone
        # Scene wiped — rerender overlay if drag is in progress.
        self._drag_overlay.rerender(self._view.scene() if self._view is not None else None)  # type: ignore[arg-type]
        # Redraw highlights after scene rebuild.
        if self._selected_id is not None and not self._dragging:
            self._draw_highlight(self._selected_id)
        for cid in self._selected_ids:
            self._draw_multi_highlight(cid)
        if self._selected_wire_id is not None:
            self._draw_wire_highlight(self._selected_wire_id)
        for wid in self._selected_wire_ids:
            self._draw_multi_wire_highlight(wid)

    def on_press(self, event: QMouseEvent, scene_pt: QPointF) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)

        # --- Re-drag mode: confirm new pin2 position ---
        if self._redrag_comp_id is not None:
            snapped = self._view.snap(scene_pt)
            self._commit_redrag(snapped)
            return True

        # --- Wire edit mode: clicking adds intermediate points ---
        if self._is_editing_wire():
            if not ctrl:
                snapped = self._view.snap(scene_pt)
                self._edit_add_point(snapped)
                return True
            idx = self._hit_test_wire_node(scene_pt)
            if idx is not None and self._selected_wire_id is not None:
                wire = self._get_selected_wire()
                if wire is not None:
                    self._set_edit_end(idx, wire)
            return True

        # --- First node selected, waiting for second (Ctrl+click sets end) ---
        if self._edit_start_idx is not None and ctrl:
            idx = self._hit_test_wire_node(scene_pt)
            if idx is not None and self._selected_wire_id is not None:
                wire = self._get_selected_wire()
                if wire is not None:
                    self._set_edit_end(idx, wire)
            return True

        # --- Ctrl+click: wire node or add/remove from multi-selection ---
        if ctrl:
            # Check for wire node click on the single-selected wire (starts edit).
            if self._selected_wire_id is not None:
                idx = self._hit_test_wire_node(scene_pt)
                if idx is not None:
                    self._start_wire_edit(idx)
                    return True

            # Check component bbox.
            hit = self._hit_test_bbox(scene_pt)
            if hit is not None:
                if hit in self._selected_ids:
                    self._selected_ids.discard(hit)
                    self._redraw_multi_highlights()
                else:
                    self._selected_ids.add(hit)
                    self._draw_multi_highlight(hit)
                self._deselect_wire()
                self._select_primary(hit)
                return True

            # Check wire.
            wire_hit = self._hit_test_wire(scene_pt)
            if wire_hit is not None:
                if wire_hit in self._selected_wire_ids:
                    self._selected_wire_ids.discard(wire_hit)
                    self._redraw_multi_wire_highlights()
                else:
                    self._selected_wire_ids.add(wire_hit)
                    self._draw_multi_wire_highlight(wire_hit)
                return True

            return False

        # --- Klik na obrysie grupy — zaznacz grupę jako całość ---
        group_outline_hit = self._hit_test_group_outline(scene_pt)
        if group_outline_hit is not None:
            self._cancel_wire_edit()
            self._deselect_wire()
            groups = self._session.layout.groups
            grp_members = frozenset(all_component_ids(groups, group_outline_hit))
            if grp_members:
                self._clear_multi_selection()
                self._selected_group_id = group_outline_hit
                self._selected_ids = set(grp_members)
                first = next(iter(grp_members))
                self._select_primary(first)
                for cid in grp_members - {first}:
                    self._draw_multi_highlight(cid)
                # Begin drag of all group members.
                self._drag_overlay.reset(self._view.scene())
                self._dragging = True
                self._drag_orig_positions = {}
                self._drag_orig_wire_paths = {}
                snapped = self._view.snap(scene_pt)
                for cid in grp_members:
                    pl = self._session.layout.placements.get(cid)
                    if pl is not None:
                        self._drag_orig_positions[cid] = pl.position
                pl_first = self._session.layout.placements.get(first)
                if pl_first is not None:
                    self._drag_anchor_offset = (
                        pl_first.position.x - snapped.x,
                        pl_first.position.y - snapped.y,
                    )
                # Collect internal wires (all path points on group pads).
                from protoboard.domain.commands.group_cmds import _group_bbox, _wire_is_internal
                bbox = _group_bbox(
                    grp_members,
                    self._session.layout.placements,
                    self._session.instances,
                    self._session.components,
                )
                if bbox is not None:
                    for wire in self._session.layout.wires:
                        if _wire_is_internal(wire, bbox):
                            self._drag_orig_wire_paths[wire.id] = list(wire.path)
            return True

        # --- Normal left click: hit-test component or wire ---
        hit = self._hit_test(scene_pt)
        if hit is not None:
            # Block interaction with components outside the active group context.
            if not self._is_accessible(hit):
                return False
            # If the hit component is in a group that we haven't entered yet,
            # redirect the selection to that group's representative ID.
            hit = self._resolve_group_hit(hit)
            self._cancel_wire_edit()
            if hit not in self._selected_ids:
                # Fresh single-component click — clear all existing selection.
                self._deselect_wire()
                self._clear_multi_selection()
                self._select_primary(hit)
            else:
                # Click on already-selected component — keep multi-selection
                # (including wires) and set as primary for properties panel.
                self._select_primary(hit)
            # Begin drag.
            self._drag_overlay.reset(self._view.scene())
            self._dragging = True
            self._drag_orig_positions = {}
            self._drag_orig_wire_paths = {}
            drag_ids = self._selected_ids if self._selected_ids else {hit}
            snapped = self._view.snap(scene_pt)
            for cid in drag_ids:
                pl = self._session.layout.placements.get(cid)
                if pl is not None:
                    self._drag_orig_positions[cid] = pl.position
            if hit in drag_ids:
                pl = self._session.layout.placements.get(hit)
                if pl is not None:
                    self._drag_anchor_offset = (
                        pl.position.x - snapped.x,
                        pl.position.y - snapped.y,
                    )
            # Snapshot original wire paths for any selected wires.
            for wire in self._session.layout.wires:
                if wire.id in self._selected_wire_ids:
                    self._drag_orig_wire_paths[wire.id] = list(wire.path)
            return True

        wire_hit = self._hit_test_wire(scene_pt)
        if wire_hit is not None:
            self._cancel_wire_edit()
            if wire_hit in self._selected_wire_ids:
                # Klik na już zaznaczony drut → drag całej selekcji.
                snapped = self._view.snap(scene_pt)
                self._dragging = True
                self._drag_orig_positions = {}
                self._drag_orig_wire_paths = {}
                # Anchor offset: użyj pierwszego punktu klikniętego drutu.
                wire_obj = next(
                    (w for w in self._session.layout.wires if w.id == wire_hit),
                    None,
                )
                if wire_obj and wire_obj.path:
                    self._drag_anchor_offset = (
                        wire_obj.path[0].x - snapped.x,
                        wire_obj.path[0].y - snapped.y,
                    )
                    # Use wire anchor as synthetic "selected_id" anchor.
                    self._selected_id = None
                for wid in self._selected_wire_ids:
                    wobj = next(
                        (w for w in self._session.layout.wires if w.id == wid),
                        None,
                    )
                    if wobj:
                        self._drag_orig_wire_paths[wid] = list(wobj.path)
                for cid in self._selected_ids:
                    pl = self._session.layout.placements.get(cid)
                    if pl is not None:
                        self._drag_orig_positions[cid] = pl.position
            else:
                self._clear_multi_selection()
                self._clear_multi_wire_selection()
                self._deselect()
                self._select_wire(wire_hit)
            return True

        # Check if click is inside bbox of an already-selected component.
        # This allows dragging a multi-selection by clicking anywhere inside
        # a selected component's body, not just near a pin.
        bbox_hit = self._hit_test_bbox(scene_pt)
        if bbox_hit is not None and (
            bbox_hit in self._selected_ids
            or bbox_hit == self._selected_id
        ):
            self._cancel_wire_edit()
            self._select_primary(bbox_hit)
            self._dragging = True
            self._drag_orig_positions = {}
            self._drag_orig_wire_paths = {}
            drag_ids = self._selected_ids if self._selected_ids else {bbox_hit}
            snapped = self._view.snap(scene_pt)
            for cid in drag_ids:
                pl = self._session.layout.placements.get(cid)
                if pl is not None:
                    self._drag_orig_positions[cid] = pl.position
            pl = self._session.layout.placements.get(bbox_hit)
            if pl is not None:
                self._drag_anchor_offset = (
                    pl.position.x - snapped.x,
                    pl.position.y - snapped.y,
                )
            for wire in self._session.layout.wires:
                if wire.id in self._selected_wire_ids:
                    self._drag_orig_wire_paths[wire.id] = list(wire.path)
            return True

        # Nothing hit — start rubber-band.
        self._cancel_wire_edit()
        self._clear_multi_selection()
        self._deselect()
        self._deselect_wire()
        self._rubber_band_start = scene_pt
        return True

    def on_double_press(self, scene_pt: QPointF) -> bool:
        """Called on double-click by BoardView (if forwarded). Enters a group."""
        hit = self._hit_test(scene_pt)
        if hit is None:
            hit = self._hit_test_bbox(scene_pt)
        groups = self._session.layout.groups
        current_group_id = self._group_stack[-1]
        parent_group = groups.get(current_group_id)
        _log.info(
            "group.double_click",
            hit=str(hit) if hit else None,
            current_group=str(current_group_id),
            child_group_ids=[str(g) for g in (parent_group.child_group_ids if parent_group else [])],
            group_stack_depth=len(self._group_stack),
        )
        if hit is None:
            return False
        # Find which child group of the current group contains this component.
        if parent_group is not None:
            for child_gid in parent_group.child_group_ids:
                members = all_component_ids(groups, child_gid)
                if hit in members:
                    self._group_stack.append(child_gid)
                    _log.info("group.entered", group_id=str(child_gid), stack_depth=len(self._group_stack))
                    self._clear_multi_selection()
                    self._deselect()
                    self._select_primary(hit)
                    self._notify_active_group()
                    return True
        _log.info("group.double_click_no_entry", reason="hit not in any child group")
        return False

    def on_move(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)

        if self._redrag_comp_id is not None:
            snapped = self._view.snap(scene_pt)
            self._update_redrag(snapped)
            return

        if self._is_editing_wire():
            self._update_edit_live(scene_pt)
            return

        if self._rubber_band_start is not None:
            self._update_rubber_band(scene_pt)
            return

        if self._dragging:
            snapped = self._view.snap(scene_pt)
            dx_off, dy_off = self._drag_anchor_offset
            anchor_new = Point(snapped.x + dx_off, snapped.y + dy_off)

            # Compute delta from anchor.
            anchor_id = self._selected_id
            delta: Point | None = None
            if anchor_id is not None:
                anchor_orig = self._drag_orig_positions.get(anchor_id)
                if anchor_orig is not None:
                    delta = Point(
                        anchor_new.x - anchor_orig.x,
                        anchor_new.y - anchor_orig.y,
                    )
            if delta is None and self._drag_orig_wire_paths:
                # Drag started from a wire — compute delta from first wire's
                # first point vs. anchor_new.
                first_wid = next(iter(self._drag_orig_wire_paths))
                first_orig = self._drag_orig_wire_paths[first_wid]
                if first_orig:
                    delta = Point(
                        anchor_new.x - first_orig[0].x,
                        anchor_new.y - first_orig[0].y,
                    )
            if delta is None:
                return
            _delta: Point = delta  # narrow type for mypy

            # Move components.
            drag_ids = (
                self._selected_ids if self._selected_ids
                else ({anchor_id} if anchor_id else set())
            )
            for cid in drag_ids:
                pl = self._session.layout.placements.get(cid)
                orig = self._drag_orig_positions.get(cid)
                if pl is not None and orig is not None:
                    pl.position = Point(orig.x + _delta.x, orig.y + _delta.y)

            # Move wires (translate all path points).
            for wire in self._session.layout.wires:
                if wire.id not in self._drag_orig_wire_paths:
                    continue
                orig_path: list[Point] = self._drag_orig_wire_paths[wire.id]
                wire.path = [
                    Point(p.x + _delta.x, p.y + _delta.y) for p in orig_path
                ]

            self._session.layout_changed.emit()
            self._update_drag_overlay(drag_ids, snapped)
            return

        if ctrl:
            hit = self._hit_test_bbox(scene_pt)
            if hit is not None:
                self._view.setCursor(Qt.CursorShape.SizeAllCursor)
            else:
                self._view.setCursor(Qt.CursorShape.ArrowCursor)

    def on_release(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        # Rubber-band release: apply selection.
        if self._rubber_band_start is not None:
            self._finish_rubber_band(scene_pt)
            return

        if not self._dragging:
            return
        self._dragging = False

        drag_ids = (
            self._selected_ids
            if self._selected_ids
            else ({self._selected_id} if self._selected_id else set())
        )
        if not drag_ids and not self._drag_orig_wire_paths:
            return

        # Build move commands for all dragged components.

        from protoboard.domain.commands.base import Command

        commands: list[Command[ComponentState]] = []

        # Resolve effective group for this drag (either from outline-click or
        # by detecting that all drag_ids belong to the same group).
        effective_group_id = self._selected_group_id
        if effective_group_id is None and drag_ids:
            effective_group_id = self._common_group_for(drag_ids)

        _log.debug("drag.release",
                   drag_ids=[str(i) for i in drag_ids],
                   selected_group_id=str(self._selected_group_id) if self._selected_group_id else None,
                   effective_group_id=str(effective_group_id) if effective_group_id else None,
                   orig_positions={str(k): (v.x, v.y) for k, v in self._drag_orig_positions.items()},
                   current_positions={str(k): (v.position.x, v.position.y)
                                      for k, v in self._session.layout.placements.items()
                                      if k in drag_ids})

        if effective_group_id is not None:
            # --- Atomic group drag: check all conflicts first, revert all on block ---
            # Ensure drag_ids covers ALL members of the group (not just a subset).
            from protoboard.domain.layout.group import all_component_ids
            full_ids = set(all_component_ids(
                self._session.layout.groups, effective_group_id
            ))
            # Keep only those that actually moved.
            full_ids &= set(self._drag_orig_positions.keys())
            effective_drag = full_ids if full_ids else drag_ids
            _log.debug("drag.group_path",
                       full_group_ids=[str(i) for i in full_ids],
                       effective_drag=[str(i) for i in effective_drag])
            _saved_group_id = self._selected_group_id
            self._selected_group_id = effective_group_id
            commands = self._build_group_drag_commands(effective_drag) or []
            self._selected_group_id = _saved_group_id
            _log.debug("drag.group_commands", count=len(commands))
        else:
            # --- Per-component conflict check (non-group drag) ---
            for cid in drag_ids:
                pl = self._session.layout.placements.get(cid)
                orig = self._drag_orig_positions.get(cid)
                if pl is None or orig is None:
                    continue
                if pl.position == orig:
                    continue
                old_pl = pl.copy()
                old_pl.position = orig

                inst = self._session.instances.get(cid)
                defn = (
                    self._session.components.get(inst.definition_ref)
                    if inst else None
                )
                if defn is not None:
                    from protoboard.gui.placement_conflict import check_and_resolve

                    resolved = check_and_resolve(
                        session=self._session,
                        placement=pl,
                        defn=defn,
                        instance_id=cid,
                        parent_widget=self._view.window(),
                    )
                    if resolved is None:
                        self._session.layout.placements[cid] = old_pl
                        continue
                    pl = resolved
                    self._session.layout.placements[cid] = pl

                commands.append(
                    MoveComponentCommand(
                        component_id=cid,
                        new_placement=pl,
                        old_placement=old_pl,
                    )
                )

            # Build wire move commands.
            for wire in self._session.layout.wires:
                if wire.id not in self._drag_orig_wire_paths:
                    continue
                orig_wire_path: list[Point] = self._drag_orig_wire_paths[wire.id]
                if wire.path == orig_wire_path:
                    continue
                commands.append(
                    ReplaceWireSegmentCommand(
                        wire_id=wire.id,
                        new_path=list(wire.path),
                        old_path=orig_wire_path,
                    )
                )

            # Remove wires that now cross alien pads.
            commands.extend(self._remove_crossing_wires(drag_ids))

        self._drag_orig_wire_paths = {}

        if commands:
            if len(commands) == 1:
                self._session.stack.execute(commands[0], self._session)
            else:
                macro = MacroCommand(commands, description="Move selection")
                self._session.stack.execute(macro, self._session)
            self._session.layout_changed.emit()
            # After successful layout rebuild, overlay was rerendered by
            # on_scene_cleared → rerender(). Reset it now (move done).
            self._drag_overlay.reset(self._view.scene())
        else:
            # No commands: nothing moved, or blocked group drag already reverted.
            # Group is back at original position — overlay location is stale.
            self._drag_overlay.reset(self._view.scene())

        # Restore selection highlights.
        if self._selected_id is not None:
            self._select_primary(self._selected_id)

    def on_key(self, event: QKeyEvent) -> None:
        key = event.key()

        if self._is_editing_wire():
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._commit_wire_edit()
            elif key == Qt.Key.Key_Escape:
                self._cancel_wire_edit()
            elif key == Qt.Key.Key_A:
                self._autoroute_wire_fragment()
            elif key == Qt.Key.Key_F:
                self._flip_wire_fragment()
            return

        ctrl  = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        alt   = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)

        # Re-drag mode: Esc cancels
        if self._redrag_comp_id is not None:
            if key == Qt.Key.Key_Escape:
                self._cancel_redrag()
            return

        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if self._selected_wire_ids:
                self._delete_multi_wires()
            elif self._selected_wire_id is not None:
                self._delete_wire()
            elif self._selected_ids:
                self._delete_multi()
            elif self._selected_id is not None:
                self._delete_selected()
        elif key == Qt.Key.Key_R and ctrl:
            if self._selected_id is not None:
                self._start_redrag(self._selected_id)
        elif key == Qt.Key.Key_R and alt and not ctrl:
            if self._selected_ids:
                self._flip_multi()
            elif self._selected_id is not None:
                self._flip_selected()
        elif key == Qt.Key.Key_R and not ctrl and not alt:
            if self._selected_ids:
                self._rotate_multi(delta=-90 if shift else 90)
            elif self._selected_id is not None:
                self._rotate_selected(delta=-90 if shift else 90)
        elif key == Qt.Key.Key_G and ctrl:
            self._group_for_selection()
        elif key == Qt.Key.Key_Escape:
            from protoboard.domain.layout.layout import ROOT_GROUP_ID
            if len(self._group_stack) > 1:  # inside a subgroup
                self.exit_group()
            else:
                self._clear_multi_selection()
                self._deselect()
                self._deselect_wire()

    def on_key_release(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Control and not self._dragging:
            self._view.setCursor(Qt.CursorShape.ArrowCursor)

    def on_right_press(self, scene_pt: QPointF) -> bool:
        """Show context menu on right-click. Returns True if consumed."""
        # --- Wire context menu (insulated toggle) ---
        wire_id = self._selected_wire_id
        if wire_id is None:
            wire_id = self._hit_test_wire(scene_pt)
        if wire_id is not None:
            wire = next(
                (w for w in self._session.layout.wires if w.id == wire_id), None
            )
            if wire is not None:
                return self._show_wire_menu(wire, scene_pt)

        # --- Component context menu (disconnect pin) ---
        # Hit-test: prefer selected component, then any component at cursor.
        comp_id = self._selected_id
        if comp_id is None:
            comp_id = self._hit_test(scene_pt)
            if comp_id is None:
                comp_id = self._hit_test_bbox(scene_pt)
        if comp_id is None:
            return False

        inst = self._session.instances.get(comp_id)
        defn = (
            self._session.components.get(inst.definition_ref) if inst else None
        )
        if inst is None or defn is None:
            return False

        # Build list of pins that have a net.
        from protoboard.domain.schematic.net import PinRef

        netted_pins: list[tuple[int, str, str]] = []  # (pin_num, pin_name, net_name)
        for pin_num in range(1, defn.footprint.pin_count() + 1):
            ref = PinRef(component_id=comp_id, pin_number=pin_num)
            net = self._session.schematic.net_for_pin(ref)
            if net is not None:
                pin_name = defn.pin_names.get(pin_num, str(pin_num))
                netted_pins.append((pin_num, pin_name, net.name))

        from PySide6.QtWidgets import QMenu
        from protoboard.domain.layout.layout import ROOT_GROUP_ID

        menu = QMenu(self._view)

        # --- Export pin submenu (only when inside a non-root group) ---
        active_gid = self._group_stack[-1] if self._group_stack else ROOT_GROUP_ID
        if active_gid != ROOT_GROUP_ID:
            active_grp = self._session.layout.groups.get(active_gid)
            if active_grp is not None and comp_id in all_component_ids(
                self._session.layout.groups, active_gid
            ):
                from protoboard.domain.commands.group_cmds import SetExportedPinsCommand
                from protoboard.domain.layout.group import ExportedPin

                menu.addSection(f"{inst.refdes} — export/unexport pins")
                for pin_num in range(1, defn.footprint.pin_count() + 1):
                    ep = ExportedPin(component_id=comp_id, pin_number=pin_num)
                    pin_name = defn.pin_names.get(pin_num, str(pin_num))
                    already = ep in active_grp.exported_pins
                    label = (
                        f"✓ Unexport pin {pin_num} ({pin_name})"
                        if already
                        else f"  Export pin {pin_num} ({pin_name})"
                    )
                    act = menu.addAction(label)
                    act.setData(("export_pin", active_gid, ep, already))

        if not netted_pins and active_gid == ROOT_GROUP_ID:
            return False  # no netted pins and not in a group — nothing to show

        if netted_pins:
            menu.addSection(f"{inst.refdes} — disconnect pin from net")
        for pin_num, pin_name, net_name in netted_pins:
            action = menu.addAction(
                f"Pin {pin_num} ({pin_name})  ←  {net_name}"
            )
            action.setData((comp_id, pin_num))

        chosen = menu.exec(
            self._view.mapToGlobal(
                self._view.mapFromScene(scene_pt)
            )
        )
        if chosen is None:
            return True  # menu shown but dismissed — still consume the event

        data = chosen.data()
        if isinstance(data, tuple) and data[0] == "export_pin":
            _, group_id, ep, already = data
            from protoboard.domain.commands.group_cmds import SetExportedPinsCommand
            grp = self._session.layout.groups.get(group_id)
            if grp is not None:
                if already:
                    new_exported = grp.exported_pins - {ep}
                else:
                    new_exported = grp.exported_pins | {ep}
                cmd_exp = SetExportedPinsCommand(
                    group_id=group_id, new_exported=new_exported
                )
                self._session.stack.execute(cmd_exp, self._session)
                self._session.layout_changed.emit()
            return True

        cid, pin_num = data
        from typing import cast
        from protoboard.domain.commands.base import Command
        from protoboard.domain.commands.component_cmds import DisconnectPinCommand

        cmd = cast(
            Command[ComponentState],
            DisconnectPinCommand(component_id=cid, pin_number=pin_num),
        )
        self._session.stack.execute(cmd, self._session)
        self._session.layout_changed.emit()
        return True

    def _show_wire_menu(
        self, wire: "WireSegment", scene_pt: QPointF
    ) -> bool:
        from PySide6.QtWidgets import QMenu
        from protoboard.domain.commands.component_cmds import (
            SetWireInsulatedCommand,
            SetWireSideCommand,
        )
        from typing import cast
        from protoboard.domain.commands.base import Command

        # Build the effective selection: rubber-band/_selected_wire_ids (multi)
        # plus _selected_wire_id (primary single click).  If the clicked wire
        # belongs to that combined set, act on the whole set; otherwise act
        # only on the clicked wire.
        effective_ids: set[uuid.UUID] = set(self._selected_wire_ids)
        if self._selected_wire_id is not None:
            effective_ids.add(self._selected_wire_id)

        ins_label = "insulated" if wire.insulated else "bare"
        if wire.id in effective_ids and len(effective_ids) > 1:
            target_ids = effective_ids
            section = f"Wires ({len(target_ids)}) — {ins_label}, {wire.side}"
        else:
            target_ids = {wire.id}
            section = f"Wire — {ins_label}, {wire.side}"

        menu = QMenu(self._view)
        menu.addSection(section)

        if wire.insulated:
            act = menu.addAction("Set bare (no insulation)")
            act.setData(("insulated", False))
        else:
            act = menu.addAction("Set insulated (add insulation jacket)")
            act.setData(("insulated", True))

        other_side = "bottom" if wire.side == "top" else "top"
        act_side = menu.addAction(f"Move to {other_side} layer")
        act_side.setData(("side", other_side))

        # Fragment flip — only available when a fragment is selected (start+end)
        if (wire.id in target_ids and len(target_ids) == 1
                and self._is_editing_wire()
                and self._selected_wire_id == wire.id):
            act_flip = menu.addAction(f"Flip fragment to {other_side} layer  [F]")
            act_flip.setData(("flip_fragment", other_side))

        chosen = menu.exec(
            self._view.mapToGlobal(self._view.mapFromScene(scene_pt))
        )
        if chosen is None:
            return True

        action, value = chosen.data()
        if action == "flip_fragment":
            self._flip_wire_fragment()
            return True
        raw_cmds: list[Command[ComponentState]]
        if action == "insulated":
            raw_cmds = [
                cast(Command[ComponentState],
                     SetWireInsulatedCommand(wire_id=wid, insulated=value))
                for wid in target_ids
            ]
        else:
            raw_cmds = [
                cast(Command[ComponentState],
                     SetWireSideCommand(wire_id=wid, side=value))
                for wid in target_ids
            ]

        if len(raw_cmds) == 1:
            cmd: Command[ComponentState] = raw_cmds[0]
        else:
            cmd = MacroCommand(raw_cmds, description=f"Set {len(raw_cmds)} wires")
        self._session.stack.execute(cmd, self._session)
        self._session.layout_changed.emit()
        return True

    # ------------------------------------------------------------------
    # Single component selection
    # ------------------------------------------------------------------

    def _camera_side(self) -> str:
        """Return the current camera side from the parent BoardView."""
        return getattr(self._view, "_camera_side", "top")

    def _layer_state(self) -> object:
        """Return a minimal LayerState for hit-test alpha checks."""
        from protoboard.gui.layer_state import LayerState, LayerOverrides
        overrides = getattr(self._view, "_layer_overrides", LayerOverrides())
        camera = self._camera_side()
        return LayerState(
            camera_side=camera,  # type: ignore[arg-type]
            overrides=overrides,
        )

    def _hit_test(self, scene_pt: QPointF) -> uuid.UUID | None:
        """Return the nearest component within _HIT_RADIUS.

        Front-side components are preferred.  Hidden components (alpha==0) are skipped.
        """
        session = self._session
        camera = self._camera_side()
        layers = self._layer_state()
        front_best: uuid.UUID | None = None
        front_best_d = float("inf")
        back_best: uuid.UUID | None = None
        back_best_d = float("inf")

        for comp_id, placement in session.layout.placements.items():
            side = placement.side  # type: ignore[assignment]
            if layers.alpha_for_component(side) == 0.0:  # type: ignore[union-attr]
                continue
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
                if placement.side == camera:
                    if d < front_best_d:
                        front_best_d = d
                        front_best = comp_id
                else:
                    if d < back_best_d:
                        back_best_d = d
                        back_best = comp_id

        if front_best is not None and front_best_d <= _HIT_RADIUS:
            return front_best
        if back_best is not None and back_best_d <= _HIT_RADIUS:
            return back_best
        return None

    def _hit_test_bbox(self, scene_pt: QPointF) -> uuid.UUID | None:
        """Return the first component whose bbox contains scene_pt.

        Front-side first; hidden components skipped.
        """
        session = self._session
        camera = self._camera_side()
        layers = self._layer_state()
        px, py = scene_pt.x(), scene_pt.y()

        def _check(comp_id: uuid.UUID, placement: object) -> bool:
            from protoboard.domain.layout.placement import ComponentPlacement as CP
            assert isinstance(placement, CP)
            if layers.alpha_for_component(placement.side) == 0.0:  # type: ignore[union-attr]
                return False
            inst = session.instances.get(comp_id)
            if inst is None:
                return False
            defn = session.components.get(inst.definition_ref)
            if defn is None:
                return False
            positions = [
                pin_world_position(defn.footprint, placement, n + 1)
                for n in range(defn.footprint.pin_count())
            ]
            if not positions:
                return False
            xs = [p.x for p in positions]
            ys = [p.y for p in positions]
            m = _BBOX_MARGIN
            return (min(xs) - m <= px <= max(xs) + m
                    and min(ys) - m <= py <= max(ys) + m)

        for comp_id, placement in session.layout.placements.items():
            if placement.side == camera and _check(comp_id, placement):
                return comp_id
        for comp_id, placement in session.layout.placements.items():
            if placement.side != camera and _check(comp_id, placement):
                return comp_id
        return None

    def _select_primary(self, comp_id: uuid.UUID) -> None:
        """Set comp_id as the primary (Properties panel) selection."""
        self._deselect()
        self._selected_id = comp_id
        self._draw_highlight(comp_id)
        inst = self._session.instances.get(comp_id)
        _log.debug("selection.component", comp_id=str(comp_id), refdes=inst.refdes if inst else "?")
        placement = self._session.layout.placements.get(comp_id)
        if inst is None:
            return
        defn = self._session.components.get(inst.definition_ref)
        panel = self._props_panel()
        if panel is not None:
            panel.show_component(inst, placement, defn)

    def _deselect(self) -> None:
        if self._highlight is not None:
            try:
                self._view.scene().removeItem(self._highlight)
            except RuntimeError:
                pass  # C++ object already deleted by scene.clear()
            self._highlight = None
        for line in self._origin_cross:
            try:
                self._view.scene().removeItem(line)
            except RuntimeError:
                pass
        self._origin_cross.clear()
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
        xs = [p.x for p in positions]
        ys = [p.y for p in positions]
        m = _BBOX_MARGIN
        self._highlight = QGraphicsRectItem(
            QRectF(min(xs) - m, min(ys) - m,
                   max(xs) - min(xs) + 2 * m, max(ys) - min(ys) + 2 * m)
        )
        theme = getattr(self._view, "_theme_override", None)
        if theme is not None:
            pen = _make_select_pen(theme.select_color, theme.select_width)
        else:
            pen = _SELECT_PEN
        self._highlight.setPen(pen)
        self._highlight.setBrush(Qt.BrushStyle.NoBrush)
        self._view.scene().addItem(self._highlight)

        # Draw a cross at the component origin (placement.position = local (0,0)).
        for line in self._origin_cross:
            self._view.scene().removeItem(line)
        self._origin_cross.clear()
        ox, oy = placement.position.x, placement.position.y
        arm = 0.35
        t = self._theme()
        cross_pen = QPen(QColor(t.component_origin_cross_color), t.component_origin_cross_width)
        h_line = QGraphicsLineItem(ox - arm, oy, ox + arm, oy)
        h_line.setPen(cross_pen)
        v_line = QGraphicsLineItem(ox, oy - arm, ox, oy + arm)
        v_line.setPen(cross_pen)
        self._view.scene().addItem(h_line)
        self._view.scene().addItem(v_line)
        self._origin_cross = [h_line, v_line]

    def _flip_selected(self) -> None:
        """Alt+R: geometrically swap pin1 and pin2 (anchor moves to old pin2 pad)."""
        comp_id = self._selected_id
        if comp_id is None:
            return
        placement = self._session.layout.placements.get(comp_id)
        inst = self._session.instances.get(comp_id)
        if placement is None or inst is None:
            return
        defn = self._session.components.get(inst.definition_ref)
        if defn is None or defn.footprint.pin_count() != 2:
            return
        new_pl = flip_component(defn.footprint, placement)
        cmd = MoveComponentCommand(
            component_id=comp_id,
            new_placement=new_pl,
            old_placement=placement.copy(),
        )
        self._session.stack.execute(cmd, self._session)
        self._session.layout_changed.emit()
        self._draw_highlight(comp_id)

    def _rotate_multi(self, delta: int = 90) -> None:
        if self._selected_group_id is not None:
            from protoboard.domain.commands.base import Command
            from protoboard.domain.commands.group_cmds import RotateGroupCommand
            from typing import cast
            rotate_cmd: Command[ComponentState] = cast(
                Command[ComponentState],
                RotateGroupCommand(group_id=self._selected_group_id, delta=delta % 360),
            )
            # Collect group member IDs for crossing-wire check after rotation.
            from protoboard.domain.layout.group import all_component_ids
            grp_comp_ids = all_component_ids(
                self._session.layout.groups, self._selected_group_id
            )
            self._session.stack.execute(rotate_cmd, self._session)
            remove_cmds = self._remove_crossing_wires(grp_comp_ids)
            if remove_cmds:
                self._session.stack.execute(
                    MacroCommand(remove_cmds, description="Remove crossing wires"),
                    self._session,
                )
            self._session.layout_changed.emit()
            self._redraw_multi_highlights()
            return
        ids = list(self._selected_ids)
        if not ids:
            return
        cmds: list[MoveComponentCommand] = []
        for cid in ids:
            placement = self._session.layout.placements.get(cid)
            if placement is None:
                continue
            new_pl = placement.copy()
            new_pl.rotation = (placement.rotation + delta) % 360
            cmds.append(MoveComponentCommand(
                component_id=cid,
                new_placement=new_pl,
                old_placement=placement.copy(),
            ))
        if not cmds:
            return
        from typing import cast
        from protoboard.domain.commands.base import Command
        macro = MacroCommand(
            cast(list[Command[ComponentState]], cmds),
            description=f"Rotate {len(cmds)} components",
        )
        self._session.stack.execute(macro, self._session)
        remove_cmds = self._remove_crossing_wires(set(ids))
        if remove_cmds:
            self._session.stack.execute(
                MacroCommand(remove_cmds, description="Remove crossing wires"),
                self._session,
            )
        self._session.layout_changed.emit()
        self._redraw_multi_highlights()

    def _flip_multi(self) -> None:
        ids = list(self._selected_ids)
        if not ids:
            return
        cmds: list[MoveComponentCommand] = []
        for cid in ids:
            placement = self._session.layout.placements.get(cid)
            inst = self._session.instances.get(cid)
            if placement is None or inst is None:
                continue
            defn = self._session.components.get(inst.definition_ref)
            if defn is None or defn.footprint.pin_count() != 2:
                continue
            new_pl = flip_component(defn.footprint, placement)
            cmds.append(MoveComponentCommand(
                component_id=cid,
                new_placement=new_pl,
                old_placement=placement.copy(),
            ))
        if not cmds:
            return
        from typing import cast
        from protoboard.domain.commands.base import Command
        macro = MacroCommand(
            cast(list[Command[ComponentState]], cmds),
            description=f"Flip {len(cmds)} components",
        )
        self._session.stack.execute(macro, self._session)
        self._session.layout_changed.emit()
        self._redraw_multi_highlights()

    def _rotate_selected(self, delta: int = 90) -> None:
        comp_id = self._selected_id
        if comp_id is None:
            return
        placement = self._session.layout.placements.get(comp_id)
        if placement is None:
            return
        new_rot = (placement.rotation + delta) % 360
        old_pl = placement.copy()
        new_pl = placement.copy()
        new_pl.rotation = new_rot
        cmd = MoveComponentCommand(
            component_id=comp_id,
            new_placement=new_pl,
            old_placement=old_pl,
        )
        self._session.stack.execute(cmd, self._session)
        remove_cmds = self._remove_crossing_wires({comp_id})
        if remove_cmds:
            self._session.stack.execute(
                MacroCommand(remove_cmds, description="Remove crossing wires"),
                self._session,
            )
        self._session.layout_changed.emit()
        self._draw_highlight(comp_id)
        panel = self._props_panel()
        if panel is not None:
            panel._rotation_combo.setCurrentIndex(new_rot // 90)

    def _common_group_for(self, comp_ids: set[uuid.UUID]) -> uuid.UUID | None:
        """Return the group ID if ALL comp_ids belong to the same non-root group, else None."""
        from protoboard.domain.layout.group import all_component_ids
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        groups = self._session.layout.groups
        # Find which group directly contains each component.
        comp_to_group: dict[uuid.UUID, uuid.UUID] = {}
        for gid, g in groups.items():
            if gid == ROOT_GROUP_ID:
                continue
            for cid in g.component_ids:
                if cid in comp_ids:
                    comp_to_group[cid] = gid
        _log.debug("common_group_for",
                   comp_ids=[str(i) for i in comp_ids],
                   comp_to_group={str(k): str(v) for k, v in comp_to_group.items()})
        if not comp_to_group:
            _log.debug("common_group_for.result", result=None, reason="no_group_members")
            return None
        group_ids = set(comp_to_group.values())
        if len(group_ids) != 1:
            _log.debug("common_group_for.result", result=None, reason="multiple_groups",
                       group_ids=[str(i) for i in group_ids])
            return None  # components from different groups
        gid = next(iter(group_ids))
        # Check that ALL drag_ids are members of this group (recursively).
        all_members = all_component_ids(groups, gid)
        if comp_ids <= set(all_members):
            _log.debug("common_group_for.result", result=str(gid))
            return gid
        _log.debug("common_group_for.result", result=None, reason="not_all_members",
                   missing=[str(i) for i in comp_ids - set(all_members)])
        return None

    def _update_drag_overlay(
        self, drag_ids: set[uuid.UUID], snapped: Point
    ) -> None:
        """Recompute and redraw drag conflict overlay (throttled to snapped grid)."""
        if not self._drag_overlay.needs_update(snapped):
            return
        from protoboard.domain.layout.pad_conflict import (
            MoveConflictSeverity,
            MoveConflictResult,
            check_group_move_conflicts,
        )
        active_gid = self._selected_group_id
        if active_gid is None and not drag_ids:
            self._drag_overlay.reset(self._view.scene())
            return

        if active_gid is not None:
            result = check_group_move_conflicts(
                moved_ids=drag_ids,
                active_group_id=active_gid,
                layout_placements=self._session.layout.placements,
                layout_wires=self._session.layout.wires,
                layout_groups=self._session.layout.groups,
                instances=self._session.instances,
                components=self._session.components,
                schematic=self._session.schematic,
            )
        else:
            # Non-group drag: quick check via find_wires_crossing_pads + find_conflicts
            from protoboard.domain.layout.pad_conflict import (
                find_wires_crossing_pads, find_conflicts, ConflictKind,
            )
            from protoboard.geometry import Point as _Point
            blocking: list[_Point] = []
            for cid in drag_ids:
                pl = self._session.layout.placements.get(cid)
                inst = self._session.instances.get(cid)
                if pl is None or inst is None:
                    continue
                defn = self._session.components.get(inst.definition_ref)
                if defn is None:
                    continue
                for c in find_conflicts(
                    pl, defn, cid,
                    self._session.layout.placements,
                    self._session.instances,
                    self._session.components,
                    self._session.schematic,
                ):
                    if c.kind == ConflictKind.DIFFERENT_NETS:
                        blocking.append(c.position)
            crossing = find_wires_crossing_pads(
                wires=self._session.layout.wires,
                moved_comp_ids=drag_ids,
                layout_placements=self._session.layout.placements,
                instances=self._session.instances,
                components=self._session.components,
                schematic=self._session.schematic,
            )
            if blocking:
                result = MoveConflictResult(
                    severity=MoveConflictSeverity.BLOCKED,
                    blocking_pad_positions=blocking,
                    removable_wire_ids=[],
                )
            elif crossing:
                result = MoveConflictResult(
                    severity=MoveConflictSeverity.REMOVABLE,
                    blocking_pad_positions=[],
                    removable_wire_ids=crossing,
                )
            else:
                result = MoveConflictResult(
                    severity=MoveConflictSeverity.CLEAR,
                    blocking_pad_positions=[],
                    removable_wire_ids=[],
                )

        # Compute drag bbox for outline.
        bbox = self._drag_bbox(drag_ids)
        self._drag_overlay.update(self._view.scene(), result, bbox, snapped)

    def _drag_bbox(
        self, drag_ids: set[uuid.UUID]
    ) -> tuple[float, float, float, float] | None:
        """Current bounding box of all dragged component pads."""
        from protoboard.domain.layout.pad_conflict import _compute_bbox
        from protoboard.domain.components.definition import ComponentDefinition
        from protoboard.domain.components.instance import ComponentInstance
        return _compute_bbox(
            frozenset(drag_ids),
            self._session.layout.placements,
            self._session.instances,
            self._session.components,
        )

    def _build_group_drag_commands(
        self, drag_ids: set[uuid.UUID]
    ) -> list[object] | None:
        """Build commands for an atomic group drag.

        Returns the command list (possibly including RemoveWireCommands for
        removable wire conflicts), or None when the move is fully blocked and
        all components have been reverted to their original positions.
        """
        from protoboard.domain.commands.base import Command
        from protoboard.domain.layout.pad_conflict import (
            MoveConflictSeverity,
            check_group_move_conflicts,
        )

        active_gid = self._selected_group_id
        if active_gid is None:
            _log.debug("build_group_drag.no_group")
            return []

        _log.debug("build_group_drag.start",
                   drag_ids=[str(i) for i in drag_ids],
                   active_gid=str(active_gid))

        result = check_group_move_conflicts(
            moved_ids=drag_ids,
            active_group_id=active_gid,
            layout_placements=self._session.layout.placements,
            layout_wires=self._session.layout.wires,
            layout_groups=self._session.layout.groups,
            instances=self._session.instances,
            components=self._session.components,
            schematic=self._session.schematic,
        )

        _log.info("build_group_drag.conflict_result",
                  severity=result.severity.value,
                  blocking_pads=[(p.x, p.y) for p in result.blocking_pad_positions],
                  removable_wires=[str(i) for i in result.removable_wire_ids])

        if result.severity == MoveConflictSeverity.BLOCKED:
            _log.info("build_group_drag.reverting",
                      drag_ids=[str(i) for i in drag_ids],
                      orig_positions={str(k): (v.x, v.y)
                                      for k, v in self._drag_orig_positions.items()
                                      if k in drag_ids})
            # Revert ALL group members atomically.
            for cid in drag_ids:
                orig = self._drag_orig_positions.get(cid)
                if orig is not None:
                    pl = self._session.layout.placements.get(cid)
                    if pl is not None:
                        _log.debug("build_group_drag.revert_component",
                                   cid=str(cid),
                                   from_pos=(pl.position.x, pl.position.y),
                                   to_pos=(orig.x, orig.y))
                        pl.position = orig
            # Revert internal wire paths.
            reverted_wires = 0
            for wire in self._session.layout.wires:
                if wire.id in self._drag_orig_wire_paths:
                    wire.path = list(self._drag_orig_wire_paths[wire.id])
                    reverted_wires += 1
            _log.info("build_group_drag.reverted", wires=reverted_wires)
            sb = getattr(self._view.window(), "statusBar", lambda: None)()
            if sb is not None:
                sb.showMessage("Przesunięcie grupy zablokowane — konflikt sieci.", 4000)
            self._session.layout_changed.emit()
            return None

        # Build move + wire-replace commands.
        cmds: list[Command[ComponentState]] = []
        for cid in drag_ids:
            pl = self._session.layout.placements.get(cid)
            orig = self._drag_orig_positions.get(cid)
            if pl is None or orig is None:
                continue
            if pl.position == orig:
                continue
            old_pl = pl.copy()
            old_pl.position = orig
            cmds.append(MoveComponentCommand(
                component_id=cid,
                new_placement=pl,
                old_placement=old_pl,
                skip_conflict_check=True,  # already verified by check_group_move_conflicts
            ))

        for wire in self._session.layout.wires:
            if wire.id not in self._drag_orig_wire_paths:
                continue
            orig_path = self._drag_orig_wire_paths[wire.id]
            if wire.path == orig_path:
                continue
            cmds.append(ReplaceWireSegmentCommand(
                wire_id=wire.id,
                new_path=list(wire.path),
                old_path=orig_path,
            ))

        # Add removal commands for removable crossing wires.
        if result.severity == MoveConflictSeverity.REMOVABLE:
            id_set = set(result.removable_wire_ids)
            for wire in self._session.layout.wires:
                if wire.id in id_set:
                    cmds.append(RemoveWireCommand(wire_id=wire.id))
            sb = getattr(self._view.window(), "statusBar", lambda: None)()
            if sb is not None:
                sb.showMessage(
                    f"Usunięto {len(result.removable_wire_ids)} drut(ów) kolidujących po przesunięciu grupy.",
                    4000,
                )

        return cmds  # type: ignore[return-value]

    def _remove_crossing_wires(
        self, moved_comp_ids: set[uuid.UUID]
    ) -> list[RemoveWireCommand]:
        """Return RemoveWireCommand list for wires that cross alien pads after a move.

        Does not execute the commands — caller folds them into a MacroCommand or
        executes them directly.  Shows a status-bar message when wires are removed.
        """
        from protoboard.domain.layout.pad_conflict import find_wires_crossing_pads

        bad_ids = find_wires_crossing_pads(
            wires=self._session.layout.wires,
            moved_comp_ids=moved_comp_ids,
            layout_placements=self._session.layout.placements,
            instances=self._session.instances,
            components=self._session.components,
            schematic=self._session.schematic,
        )
        if not bad_ids:
            return []
        id_set = set(bad_ids)
        cmds = [
            RemoveWireCommand(wire_id=w.id)
            for w in self._session.layout.wires
            if w.id in id_set
        ]
        sb = getattr(self._view.window(), "statusBar", lambda: None)()
        if sb is not None:
            sb.showMessage(
                f"Usunięto {len(cmds)} drut(ów) kolidujących po przesunięciu.", 4000
            )
        return cmds

    def _delete_selected(self) -> None:
        comp_id = self._selected_id
        if comp_id is None:
            return
        from typing import cast
        from protoboard.domain.commands.base import Command
        cmd = cast(Command[ComponentState], RemoveComponentCommand(component_id=comp_id))
        self._session.stack.execute(cmd, self._session)
        self._deselect()
        self._session.layout_changed.emit()

    # ------------------------------------------------------------------
    # Ctrl+R re-drag — reposition pin 2 interactively
    # ------------------------------------------------------------------

    def _start_redrag(self, comp_id: uuid.UUID) -> None:
        """Enter re-drag mode: the anchor pin is frozen, cursor picks a new pad
        for the bendable (free-range) pin.

        For 2-pin components after flip (vertical_flipped=True) pin 1 holds the
        bend offset, so we redrag pin 1 instead of pin 2.  This keeps the
        semantics consistent: the user always moves the "far" end of the lead.
        """
        placement = self._session.layout.placements.get(comp_id)
        inst = self._session.instances.get(comp_id)
        if placement is None or inst is None:
            return
        defn = self._session.components.get(inst.definition_ref)
        if defn is None or not defn.bendable_pins:
            return

        if placement.vertical_flipped and defn.footprint.pin_count() == 2:
            # After flip the bend offset lives on pin 1 — that is the "far" end
            # the user wants to reposition.  Use pin 2's spec for reach limits
            # (the library only declares bendable_pins[2] for axial parts).
            pin_num: int = 1
            spec = defn.bendable_pins.get(1) or defn.bendable_pins.get(2)
            if spec is None or not spec.is_free():
                return
        else:
            free_pin = next(
                (p for p, s in sorted(defn.bendable_pins.items()) if s.is_free()), None
            )
            if free_pin is None:
                return
            pin_num = free_pin
        self._redrag_comp_id = comp_id
        self._redrag_pin_num = pin_num
        self._draw_redrag_overlay(comp_id, pin_num, placement)

    def _draw_redrag_overlay(
        self,
        comp_id: uuid.UUID,
        pin_num: int,
        placement: "ComponentPlacement",  # type: ignore[name-defined]
    ) -> None:
        self._clear_redrag_overlay()
        inst = self._session.instances.get(comp_id)
        if inst is None:
            return
        defn = self._session.components.get(inst.definition_ref)
        if defn is None:
            return
        spec = defn.bendable_pins.get(pin_num) or next(
            (s for s in defn.bendable_pins.values() if s.is_free()), None
        )
        if spec is None or not spec.is_free():
            return
        # Reach is measured from the component origin (placement.position = local
        # (0,0) = rotation centre), not from any specific pin world position.
        origin_world = placement.position
        r = 0.18
        from PySide6.QtGui import QBrush, QPen
        from PySide6.QtWidgets import QGraphicsEllipseItem
        green = QColor(0, 200, 80, 120)
        for pt in self._view.board_pad_positions():
            if spec.is_reachable_from_origin(origin_world, pt):
                item = QGraphicsEllipseItem(pt.x - r, pt.y - r, 2 * r, 2 * r)
                item.setBrush(QBrush(green))
                item.setPen(QPen(Qt.GlobalColor.transparent, 0))
                item.setZValue(20.0)
                self._view.scene().addItem(item)
                self._redrag_items.append(item)

    def _update_redrag(self, cursor: Point) -> None:
        """Update the feedback line during re-drag."""
        comp_id = self._redrag_comp_id
        pin_num = self._redrag_pin_num
        if comp_id is None or pin_num is None:
            return
        # Remove old feedback line (last item if it's a line).
        if self._redrag_items and isinstance(self._redrag_items[-1], QGraphicsLineItem):
            self._view.scene().removeItem(self._redrag_items.pop())
        placement = self._session.layout.placements.get(comp_id)
        inst = self._session.instances.get(comp_id)
        if placement is None or inst is None:
            return
        defn = self._session.components.get(inst.definition_ref)
        if defn is None:
            return
        # Feedback line from component origin to cursor.
        origin = placement.position
        line = QGraphicsLineItem(origin.x, origin.y, cursor.x, cursor.y)
        from PySide6.QtGui import QPen
        pen = QPen(QColor(0, 200, 80, 180), 0.05)
        pen.setStyle(Qt.PenStyle.DashLine)
        line.setPen(pen)
        line.setZValue(20.0)
        self._view.scene().addItem(line)
        self._redrag_items.append(line)

    def _commit_redrag(self, cursor: Point) -> None:
        """Commit the re-drag: compute new pin_offsets and save via command."""
        comp_id = self._redrag_comp_id
        pin_num = self._redrag_pin_num
        if comp_id is None or pin_num is None:
            self._cancel_redrag()
            return
        placement = self._session.layout.placements.get(comp_id)
        inst = self._session.instances.get(comp_id)
        if placement is None or inst is None:
            self._cancel_redrag()
            return
        defn = self._session.components.get(inst.definition_ref)
        if defn is None:
            self._cancel_redrag()
            return
        spec = defn.bendable_pins.get(pin_num) or next(
            (s for s in defn.bendable_pins.values() if s.is_free()), None
        )
        # Reach is measured from the component origin (placement.position).
        origin_world = placement.position
        # Snap to nearest board pad
        from PySide6.QtCore import QPointF as _QPointF
        snapped = self._view.snap(_QPointF(cursor.x, cursor.y))
        # Validate reach from origin
        if spec is not None and spec.is_free() and not spec.is_reachable_from_origin(origin_world, snapped):
            self._cancel_redrag()
            return
        # Convert snapped world position to local offset for the pin.
        temp_no_off = placement.copy()
        temp_no_off.pin_offsets = {k: v for k, v in placement.pin_offsets.items()
                                   if k != pin_num}
        baseline = pin_world_position(defn.footprint, temp_no_off, pin_num)
        world_delta = Point(snapped.x - baseline.x, snapped.y - baseline.y)
        # Invert the full pipeline: unrotate, then undo X-mirror for bottom side.
        local_off = unrotate(world_delta, placement.rotation)
        if placement.side == "bottom":
            local_off = Point(-local_off.x, local_off.y)
        # Build new placement
        old_pl = placement.copy()
        new_pl = placement.copy()
        new_offsets = dict(placement.pin_offsets)
        if local_off.x == 0.0 and local_off.y == 0.0:
            new_offsets.pop(pin_num, None)
        else:
            new_offsets[pin_num] = local_off
        new_pl.pin_offsets = new_offsets
        cmd = MoveComponentCommand(
            component_id=comp_id,
            new_placement=new_pl,
            old_placement=old_pl,
        )
        self._session.stack.execute(cmd, self._session)
        self._cancel_redrag()
        self._session.layout_changed.emit()

    def _cancel_redrag(self) -> None:
        self._clear_redrag_overlay()
        self._redrag_comp_id = None
        self._redrag_pin_num = None

    def _clear_redrag_overlay(self) -> None:
        for item in self._redrag_items:
            self._view.scene().removeItem(item)
        self._redrag_items = []

    # ------------------------------------------------------------------
    # Multi-selection
    # ------------------------------------------------------------------

    def _draw_multi_highlight(self, comp_id: uuid.UUID) -> None:
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
        xs = [p.x for p in positions]
        ys = [p.y for p in positions]
        m = _BBOX_MARGIN + 0.05
        rect = QGraphicsRectItem(
            QRectF(min(xs) - m, min(ys) - m,
                   max(xs) - min(xs) + 2 * m, max(ys) - min(ys) + 2 * m)
        )
        theme = getattr(self._view, "_theme_override", None)
        if theme is not None:
            multi_pen = _make_select_pen(theme.multi_select_color, theme.multi_select_width)
        else:
            multi_pen = _MULTI_SELECT_PEN
        rect.setPen(multi_pen)
        rect.setBrush(Qt.BrushStyle.NoBrush)
        self._view.scene().addItem(rect)
        self._multi_highlights.append(rect)

    def _redraw_multi_highlights(self) -> None:
        for item in self._multi_highlights:
            self._view.scene().removeItem(item)
        self._multi_highlights.clear()
        for cid in self._selected_ids:
            self._draw_multi_highlight(cid)

    def _clear_multi_selection(self) -> None:
        for item in self._multi_highlights:
            self._view.scene().removeItem(item)
        self._multi_highlights.clear()
        self._selected_ids.clear()
        self._selected_group_id = None

    def _delete_multi(self) -> None:
        ids = list(self._selected_ids)
        if not ids:
            return
        from typing import cast

        from protoboard.domain.commands.base import Command

        from protoboard.domain.commands.component_cmds import SchematicState

        commands = cast(
            list[Command[SchematicState]],
            [RemoveComponentCommand(component_id=cid) for cid in ids],
        )
        macro = cast(
            Command[ComponentState],
            MacroCommand(commands, description="Delete components"),
        )
        self._session.stack.execute(macro, self._session)
        self._clear_multi_selection()
        self._deselect()
        self._session.layout_changed.emit()

    # ------------------------------------------------------------------
    # Rubber-band selection
    # ------------------------------------------------------------------

    def _update_rubber_band(self, scene_pt: QPointF) -> None:
        if self._rubber_band_start is None:
            return
        if self._rubber_band_item is not None:
            self._view.scene().removeItem(self._rubber_band_item)
            self._rubber_band_item = None

        start = self._rubber_band_start
        x0 = min(start.x(), scene_pt.x())
        y0 = min(start.y(), scene_pt.y())
        w = abs(scene_pt.x() - start.x())
        h = abs(scene_pt.y() - start.y())

        left_to_right = scene_pt.x() >= start.x()
        pen = _RUBBER_BAND_PEN_LR if left_to_right else _RUBBER_BAND_PEN_RL
        fill = _RUBBER_BAND_FILL_LR if left_to_right else _RUBBER_BAND_FILL_RL

        item = QGraphicsRectItem(QRectF(x0, y0, w, h))
        item.setPen(pen)
        item.setBrush(fill)
        self._view.scene().addItem(item)
        self._rubber_band_item = item

    def _finish_rubber_band(self, scene_pt: QPointF) -> None:
        self._clear_rubber_band()
        if self._rubber_band_start is None:
            return
        start = self._rubber_band_start
        self._rubber_band_start = None

        left_to_right = scene_pt.x() >= start.x()
        rx0 = min(start.x(), scene_pt.x())
        ry0 = min(start.y(), scene_pt.y())
        rx1 = max(start.x(), scene_pt.x())
        ry1 = max(start.y(), scene_pt.y())

        # Minimum drag size to avoid accidental selection.
        if (rx1 - rx0) < 0.1 and (ry1 - ry0) < 0.1:
            self._rubber_band_start = None
            return

        self._rubber_band_start = None
        session = self._session
        selected: set[uuid.UUID] = set()
        for comp_id, placement in session.layout.placements.items():
            inst = session.instances.get(comp_id)
            if inst is None:
                continue
            defn = session.components.get(inst.definition_ref)
            if defn is None:
                continue
            bbox = component_bbox(defn.footprint, placement, defn.component_class)
            bx0 = bbox.x
            by0 = bbox.y
            bx1 = bbox.x + bbox.width
            by1 = bbox.y + bbox.height
            if left_to_right:
                # Fully inside.
                if bx0 >= rx0 and by0 >= ry0 and bx1 <= rx1 and by1 <= ry1:
                    selected.add(comp_id)
            else:
                # Intersects.
                if bx0 < rx1 and bx1 > rx0 and by0 < ry1 and by1 > ry0:
                    selected.add(comp_id)

        # Wires.
        selected_wires: set[uuid.UUID] = set()
        for wire in session.layout.wires:
            pts = wire.path
            if left_to_right:
                # Fully inside: all points within rect.
                if all(rx0 <= p.x <= rx1 and ry0 <= p.y <= ry1 for p in pts):
                    selected_wires.add(wire.id)
            else:
                # Intersects: any point inside OR any segment crosses rect.
                if any(rx0 <= p.x <= rx1 and ry0 <= p.y <= ry1 for p in pts):
                    selected_wires.add(wire.id)
                elif any(
                    _segment_intersects_rect(
                        pts[i].x, pts[i].y, pts[i + 1].x, pts[i + 1].y,
                        rx0, ry0, rx1, ry1,
                    )
                    for i in range(len(pts) - 1)
                ):
                    selected_wires.add(wire.id)

        if selected or selected_wires:
            _log.debug("selection.rubber_band",
                       mode="ltr" if left_to_right else "rtl",
                       components=len(selected), wires=len(selected_wires))
            self._selected_ids = selected
            self._redraw_multi_highlights()
            self._selected_wire_ids = selected_wires
            self._redraw_multi_wire_highlights()
            if selected:
                self._select_primary(next(iter(selected)))

    def _clear_rubber_band(self) -> None:
        if self._rubber_band_item is not None:
            self._view.scene().removeItem(self._rubber_band_item)
            self._rubber_band_item = None

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
        _log.debug("selection.wire", wire_id=str(wire_id))
        self._draw_wire_highlight(wire_id)

    def _deselect_wire(self) -> None:
        self._cancel_wire_edit()
        if self._wire_highlight is not None:
            self._view.scene().removeItem(self._wire_highlight)
            self._wire_highlight = None
        self._selected_wire_id = None
        self._clear_multi_wire_selection()

    def _draw_wire_highlight(self, wire_id: uuid.UUID) -> None:
        if self._wire_highlight is not None:
            self._view.scene().removeItem(self._wire_highlight)
            self._wire_highlight = None
        wire = next(
            (w for w in self._session.layout.wires if w.id == wire_id), None
        )
        if wire is None or len(wire.path) < 2:
            return
        from protoboard.gui.layer_state import Z_SELECTION
        group = QGraphicsItemGroup()
        pts = wire.path
        for i in range(len(pts) - 1):
            seg = QGraphicsLineItem(
                pts[i].x, pts[i].y, pts[i + 1].x, pts[i + 1].y
            )
            seg.setPen(_WIRE_SELECT_PEN)
            group.addToGroup(seg)
        for i, pt in enumerate(pts):
            r = _NODE_RADIUS
            is_sel = (i == self._edit_start_idx or i == self._edit_end_idx)
            dot = QGraphicsEllipseItem(pt.x - r, pt.y - r, 2 * r, 2 * r)
            dot.setPen(_NODE_SELECTED_PEN if is_sel else _NODE_PEN)
            dot.setBrush(_NODE_SELECTED_BRUSH if is_sel else _NODE_BRUSH)
            group.addToGroup(dot)
        group.setZValue(Z_SELECTION)
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

    def _draw_multi_wire_highlight(self, wire_id: uuid.UUID) -> None:
        wire = next(
            (w for w in self._session.layout.wires if w.id == wire_id), None
        )
        if wire is None or len(wire.path) < 2:
            return
        from protoboard.gui.layer_state import Z_SELECTION
        group = QGraphicsItemGroup()
        pts = wire.path
        for i in range(len(pts) - 1):
            seg = QGraphicsLineItem(
                pts[i].x, pts[i].y, pts[i + 1].x, pts[i + 1].y
            )
            seg.setPen(_WIRE_SELECT_PEN)
            group.addToGroup(seg)
        group.setZValue(Z_SELECTION)
        self._view.scene().addItem(group)
        self._wire_multi_highlights.append(group)

    def _redraw_multi_wire_highlights(self) -> None:
        for item in self._wire_multi_highlights:
            self._view.scene().removeItem(item)
        self._wire_multi_highlights.clear()
        for wid in self._selected_wire_ids:
            self._draw_multi_wire_highlight(wid)

    def _clear_multi_wire_selection(self) -> None:
        for item in self._wire_multi_highlights:
            self._view.scene().removeItem(item)
        self._wire_multi_highlights.clear()
        self._selected_wire_ids.clear()

    def _delete_multi_wires(self) -> None:
        ids = list(self._selected_wire_ids)
        if not ids:
            return
        from typing import cast

        from protoboard.domain.commands.base import Command

        commands = cast(
            list[Command[ComponentState]],
            [RemoveWireCommand(wire_id=wid) for wid in ids],
        )
        if len(commands) == 1:
            self._session.stack.execute(commands[0], self._session)
        else:
            macro = MacroCommand(commands, description="Delete wires")
            self._session.stack.execute(macro, self._session)
        self._clear_multi_wire_selection()
        self._session.layout_changed.emit()

    # ------------------------------------------------------------------
    # Wire segment editing
    # ------------------------------------------------------------------

    def _hit_test_wire_node(self, scene_pt: QPointF) -> int | None:
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
        self._cancel_wire_edit()
        self._edit_start_idx = node_idx
        self._edit_end_idx = None
        self._edit_points = []
        if self._selected_wire_id is not None:
            self._draw_wire_highlight(self._selected_wire_id)

    def _set_edit_end(self, node_idx: int, wire: WireSegment) -> None:
        start = self._edit_start_idx
        if start is None or node_idx == start:
            return
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
        self._edit_points.append(pt)
        wire = self._get_selected_wire()
        if len(self._edit_points) >= 2:
            a = self._edit_points[-2]
            b = self._edit_points[-1]
        else:
            if wire is None or self._edit_start_idx is None:
                return
            a = wire.path[self._edit_start_idx]
            b = self._edit_points[0]
        line = QGraphicsLineItem(a.x, a.y, b.x, b.y)
        line.setPen(_EDIT_PREVIEW_PEN)
        self._view.scene().addItem(line)
        self._edit_preview_items.append(line)
        self._remove_edit_live()

    def _update_edit_live(self, scene_pt: QPointF) -> None:
        self._remove_edit_live()
        wire = self._get_selected_wire()
        if wire is None or self._edit_start_idx is None:
            return
        snapped = self._view.snap(scene_pt)
        anchor = (
            self._edit_points[-1] if self._edit_points
            else wire.path[self._edit_start_idx]
        )
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
        wire = self._get_selected_wire()
        if wire is None:
            return
        start = self._edit_start_idx
        end = self._edit_end_idx
        if start is None or end is None:
            return
        prefix = list(wire.path[:start + 1])
        suffix = list(wire.path[end:])
        new_path = _dedupe_adjacent(
            prefix + list(self._edit_points) + suffix
        )
        if len(new_path) < 2:
            self._cancel_wire_edit()
            return
        self._clear_edit_preview()
        self._cancel_wire_edit()
        cmd = ReplaceWireSegmentCommand(wire_id=wire.id, new_path=new_path)
        self._session.stack.execute(cmd, self._session)
        self._session.layout_changed.emit()
        self._select_wire(wire.id)

    def _flip_wire_fragment(self) -> None:
        """Flip the selected wire fragment to the opposite layer (F key).

        Prerequisites:
        - A single wire is selected (_selected_wire_id set).
        - A fragment is marked (_edit_start_idx and _edit_end_idx both set).
        - The fragment endpoints must lie on plated-through pads (vias) when
          the fragment does not span the whole wire — otherwise layer change is
          physically impossible.
        - No pads occupied by a different net on the target layer.
        - No existing wire on the target layer shares interior points with the
          fragment.

        On success executes FlipWireFragmentCommand (full undo/redo).
        On failure shows a status-bar message and does nothing.
        """
        from PySide6.QtWidgets import QMessageBox
        from protoboard.domain.commands.component_cmds import FlipWireFragmentCommand
        from protoboard.domain.layout.wire_utils import (
            board_pads_for_side,
            plated_through_pads,
        )
        from protoboard.domain.layout.placement import pin_world_position

        wire = self._get_selected_wire()
        if wire is None or self._edit_start_idx is None or self._edit_end_idx is None:
            return

        start = self._edit_start_idx
        end = self._edit_end_idx
        path = wire.path
        target_side: str = "bottom" if wire.side == "top" else "top"
        fragment = path[start:end + 1]
        is_whole_wire = (start == 0 and end == len(path) - 1)

        session = self._session

        # --- Check 1: endpoints must be plated-through if not whole wire ---
        if not is_whole_wire:
            vias = plated_through_pads(session.board)
            for idx, label in ((start, "start"), (end, "end")):
                pt = path[idx]
                if pt not in vias:
                    self._status_msg(
                        f"Cannot flip: {label} point ({pt.x:.1f},{pt.y:.1f}) "
                        f"is not a plated-through pad (via required for layer change)."
                    )
                    return

        # --- Check 2: all fragment points must exist on target_side ---
        target_pads = board_pads_for_side(session.board, target_side)
        for pt in fragment:
            if pt not in target_pads:
                self._status_msg(
                    f"Cannot flip: pad ({pt.x:.1f},{pt.y:.1f}) "
                    f"is not available on {target_side} layer."
                )
                return

        # --- Check 3: no pin of another net on target_side ---
        # Build (Point, side) → net_id from placed component pins
        pin_net: dict[tuple[float, float], "uuid.UUID | None"] = {}
        for net in session.schematic.nets.values():
            for ref in net.pins:
                placement = session.layout.placements.get(ref.component_id)
                if placement is None:
                    continue
                defn = session.components.get(
                    session.instances[ref.component_id].definition_ref
                )
                if defn is None:
                    continue
                wp = pin_world_position(defn.footprint, placement, ref.pin_number)
                pin_net[(wp.x, wp.y)] = net.id

        # Determine which net this wire belongs to (if any)
        wire_net_id: "uuid.UUID | None" = None
        for net in session.schematic.nets.values():
            for ref in net.pins:
                placement = session.layout.placements.get(ref.component_id)
                if placement is None:
                    continue
                defn = session.components.get(
                    session.instances[ref.component_id].definition_ref
                )
                if defn is None:
                    continue
                wp = pin_world_position(defn.footprint, placement, ref.pin_number)
                if any(abs(wp.x - p.x) < 1e-4 and abs(wp.y - p.y) < 1e-4
                       for p in wire.path):
                    wire_net_id = net.id
                    break
            if wire_net_id is not None:
                break

        for pt in fragment:
            nid = pin_net.get((pt.x, pt.y))
            if nid is not None and nid != wire_net_id:
                self._status_msg(
                    f"Cannot flip: pad ({pt.x:.1f},{pt.y:.1f}) is occupied "
                    f"by a different net on {target_side} layer."
                )
                return

        # --- Check 4: no existing wire on target_side overlaps interior points ---
        interior = set(fragment[1:-1])
        if interior:
            for other in session.layout.wires:
                if other.id == wire.id or other.side != target_side:
                    continue
                other_pts = set(other.path)
                overlap = interior & other_pts
                if overlap:
                    pt = next(iter(overlap))
                    self._status_msg(
                        f"Cannot flip: interior point ({pt.x:.1f},{pt.y:.1f}) "
                        f"is already occupied by another wire on {target_side}."
                    )
                    return

        # --- All checks passed — execute ---
        cmd = FlipWireFragmentCommand(
            wire_id=wire.id,
            start_idx=start,
            end_idx=end,
            target_side=target_side,
        )
        self._cancel_wire_edit()
        self._session.stack.execute(cmd, self._session)
        self._session.layout_changed.emit()
        self._status_msg(f"Fragment flipped to {target_side} layer.")

    def _status_msg(self, msg: str, timeout_ms: int = 4000) -> None:
        """Show a message in the main window status bar."""
        win = self._view.window()
        status = getattr(win, "_status", None)
        if status is not None:
            status.showMessage(msg, timeout_ms)

    def _autoroute_wire_fragment(self) -> None:
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
        results = find_paths(
            graph=graph,
            source=wire.path[start],
            targets=[wire.path[end]],
            side=wire.side,
            cost_model=RoutingCostModel(),
            k=1,
        )
        if not results:
            return
        routed = [pt for pt, _ in results[0].path]
        middle = list(routed[1:-1])
        new_path = _dedupe_adjacent(
            list(wire.path[:start + 1]) + middle + list(wire.path[end:])
        )
        if len(new_path) < 2:
            self._cancel_wire_edit()
            return
        self._clear_edit_preview()
        self._cancel_wire_edit()
        cmd = ReplaceWireSegmentCommand(wire_id=wire.id, new_path=new_path)
        self._session.stack.execute(cmd, self._session)
        self._session.layout_changed.emit()
        self._select_wire(wire.id)

    # ------------------------------------------------------------------
    # Group navigation helpers
    # ------------------------------------------------------------------

    def _hit_test_group_outline(self, scene_pt: QPointF) -> uuid.UUID | None:
        """Return group_id if the click landed on a group outline rect item."""
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        for item in self._view.scene().items(scene_pt):
            tag = item.data(1) if hasattr(item, "data") else None
            if tag == "group_outline":
                raw = item.data(0)
                if raw:
                    gid = uuid.UUID(str(raw))
                    # Only outline-clicks for child groups of active group.
                    groups = self._session.layout.groups
                    active = groups.get(self._group_stack[-1])
                    if active and gid in active.child_group_ids:
                        return gid
        return None

    def _resolve_group_hit(self, comp_id: uuid.UUID) -> uuid.UUID:
        """If comp_id is inside a child group not yet entered, return the first
        component ID from that child group as a proxy for group-level selection.
        Returns comp_id unchanged if it's a direct member at the current depth."""
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        groups = self._session.layout.groups
        current = self._group_stack[-1]
        g = groups.get(current)
        # Check if comp_id is directly in a child group — redirect to proxy.
        for child_id in (g.child_group_ids if g else frozenset()):
            if comp_id in all_component_ids(groups, child_id):
                child_g = groups.get(child_id)
                if child_g and child_g.component_ids:
                    return next(iter(child_g.component_ids))
        return comp_id

    def _current_group_component_ids(self) -> frozenset[uuid.UUID]:
        """Return the set of all component IDs accessible at the current group depth."""
        groups = self._session.layout.groups
        gid = self._group_stack[-1]
        g = groups.get(gid)
        if g is None:
            return frozenset()
        return all_component_ids(groups, gid)

    def _is_accessible(self, comp_id: uuid.UUID) -> bool:
        """True if comp_id can be interacted with at the current group depth.

        Root group is special: its direct members are all components NOT inside
        any child group (components float freely at root level).
        Non-root groups: component must be in component_ids or a child group.
        """
        if not self._group_stack:
            return True
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        groups = self._session.layout.groups
        active_gid = self._group_stack[-1]
        active = groups.get(active_gid)
        if active is None:
            return True  # no group info — allow all

        if active_gid == ROOT_GROUP_ID:
            # At root level: accessible if NOT inside any child group of root.
            for child_gid in active.child_group_ids:
                if comp_id in all_component_ids(groups, child_gid):
                    return True  # in a child group — selectable as proxy
            # Not in any child group = free component at root level.
            return True

        # Inside a non-root group: must be a direct member or in a child group.
        if comp_id in active.component_ids:
            return True
        for child_gid in active.child_group_ids:
            if comp_id in all_component_ids(groups, child_gid):
                return True
        _log.debug("selection.blocked", comp_id=str(comp_id), active_group=str(active_gid))
        return False

    def _group_for_selection(self) -> uuid.UUID | None:
        """Group the current selection: show dialog to create new or add to existing."""
        ids = frozenset(self._selected_ids)
        if self._selected_id and self._selected_id not in ids:
            ids = ids | {self._selected_id}
        if not ids:
            return None

        from protoboard.domain.commands.group_cmds import AddToGroupCommand, CreateGroupCommand
        from protoboard.domain.layout.group import ComponentGroup
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        from protoboard.gui.dialogs.group_target_dialog import GroupTargetDialog

        # Collect groups at the current level (child groups of active group),
        # excluding root.
        groups = self._session.layout.groups
        active_gid = self._group_stack[-1]
        active = groups.get(active_gid)
        child_groups = [
            groups[gid]
            for gid in (active.child_group_ids if active else frozenset())
            if gid in groups and gid != ROOT_GROUP_ID
        ]

        default_name = f"Group{len(groups) + 1}"
        dlg = GroupTargetDialog(
            existing_groups=child_groups,
            default_name=default_name,
            parent=self._view.window(),  # type: ignore[arg-type]
        )
        if dlg.exec() != GroupTargetDialog.DialogCode.Accepted:
            return None

        if dlg.mode == "new":
            new_group = ComponentGroup(
                id=uuid.uuid4(),
                name=dlg.new_name or default_name,
                component_ids=ids,
            )
            cmd = CreateGroupCommand(group=new_group)
            self._session.stack.execute(cmd, self._session)
            # Register new group as a child of the active group.
            cmd_attach = AddToGroupCommand(
                group_id=active_gid,
                child_group_ids=frozenset({new_group.id}),
            )
            self._session.stack.execute(cmd_attach, self._session)
            self._session.layout_changed.emit()
            return new_group.id
        else:
            # Add to existing group.
            target_id = dlg.target_group_id
            if target_id is None:
                return None
            cmd_add = AddToGroupCommand(
                group_id=target_id,
                component_ids=ids,
            )
            self._session.stack.execute(cmd_add, self._session)
            self._session.layout_changed.emit()
            return target_id

    def exit_group(self) -> None:
        """Pop the innermost group from the navigation stack (Esc)."""
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        if len(self._group_stack) > 1:  # never pop root
            exiting_gid = self._group_stack[-1]
            _log.debug("group.exited", group_id=str(exiting_gid), remaining=len(self._group_stack) - 1)
            self._group_stack.pop()
            self._clear_multi_selection()
            self._deselect()
            self._reset_group_metadata(exiting_gid)
            self._notify_active_group()

    def _reset_group_metadata(self, group_id: uuid.UUID) -> None:
        """Recompute origin and zero rotation after editing a group. Physical positions unchanged."""
        from protoboard.domain.commands.group_cmds import _group_origin
        from protoboard.domain.layout.group import all_component_ids as _all_ids
        group = self._session.layout.groups.get(group_id)
        if group is None:
            return
        comp_ids = _all_ids(self._session.layout.groups, group_id)
        new_origin = _group_origin(
            comp_ids,
            self._session.layout.placements,
            self._session.instances,
            self._session.components,
        )
        from protoboard.domain.layout.group import ComponentGroup
        self._session.layout.groups[group_id] = ComponentGroup(
            id=group.id,
            name=group.name,
            component_ids=group.component_ids,
            child_group_ids=group.child_group_ids,
            exported_pins=group.exported_pins,
            rotation=0,
            origin=new_origin,
        )

    def _notify_active_group(self) -> None:
        """Inform BoardView of the current active group for rendering."""
        if hasattr(self._view, "set_active_group"):
            self._view.set_active_group(self._group_stack[-1])  # type: ignore[union-attr]

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


def _segment_intersects_rect(
    ax: float, ay: float, bx: float, by: float,
    rx0: float, ry0: float, rx1: float, ry1: float,
) -> bool:
    """Cohen-Sutherland clip: True if segment AB intersects rect."""
    def _code(x: float, y: float) -> int:
        c = 0
        if x < rx0:
            c |= 1
        elif x > rx1:
            c |= 2
        if y < ry0:
            c |= 4
        elif y > ry1:
            c |= 8
        return c

    c0, c1 = _code(ax, ay), _code(bx, by)
    while True:
        if not (c0 | c1):
            return True   # both inside
        if c0 & c1:
            return False  # both outside same half-plane
        c = c0 if c0 else c1
        if c & 1:
            y = ay + (by - ay) * (rx0 - ax) / (bx - ax + 1e-15)
            x = rx0
        elif c & 2:
            y = ay + (by - ay) * (rx1 - ax) / (bx - ax + 1e-15)
            x = rx1
        elif c & 4:
            x = ax + (bx - ax) * (ry0 - ay) / (by - ay + 1e-15)
            y = ry0
        else:
            x = ax + (bx - ax) * (ry1 - ay) / (by - ay + 1e-15)
            y = ry1
        if c == c0:
            ax, ay, c0 = x, y, _code(x, y)
        else:
            bx, by, c1 = x, y, _code(x, y)
