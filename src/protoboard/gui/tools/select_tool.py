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

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPen
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
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
from protoboard.domain.layout.placement import component_bbox, pin_world_position
from protoboard.domain.layout.wire import WireSegment
from protoboard.geometry import Point
from protoboard.gui.board_view import _Tool
from protoboard.gui.panels.properties_panel import PropertiesPanel

_SELECT_PEN = QPen(Qt.GlobalColor.cyan, 0.08)
_SELECT_PEN.setStyle(Qt.PenStyle.DashLine)
_MULTI_SELECT_PEN = QPen(Qt.GlobalColor.cyan, 0.06)
_MULTI_SELECT_PEN.setStyle(Qt.PenStyle.DashLine)
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


class SelectTool(_Tool):
    """Click/rubber-band to select; drag to move; R to rotate; Del to delete."""

    def __init__(self) -> None:
        # Single component selection (for Properties panel and drag).
        self._selected_id: uuid.UUID | None = None
        self._highlight: QGraphicsRectItem | None = None
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
        self._group_stack: list[uuid.UUID] = []

    # ------------------------------------------------------------------
    # _Tool interface
    # ------------------------------------------------------------------

    def deactivate(self) -> None:
        self._dragging = False
        self._drag_orig_positions = {}
        self._drag_orig_wire_paths = {}
        self._cancel_wire_edit()
        self._deselect_wire()
        self._clear_multi_wire_selection()
        self._clear_rubber_band()

    def on_scene_cleared(self) -> None:
        self._highlight = None
        self._multi_highlights.clear()
        self._wire_highlight = None
        self._wire_multi_highlights.clear()
        self._edit_preview_items.clear()
        self._edit_live_line = None
        self._rubber_band_item = None
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

        # --- Normal left click: hit-test component or wire ---
        hit = self._hit_test(scene_pt)
        if hit is not None:
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
        if hit is None:
            return False
        # If the component belongs to a group we're inside, enter a deeper group.
        groups = self._session.layout.groups
        # Find which group (at current level) contains this component.
        current_group_id = self._group_stack[-1] if self._group_stack else None
        if current_group_id is not None:
            parent_group = groups.get(current_group_id)
            if parent_group is not None:
                for child_gid in parent_group.child_group_ids:
                    child_group = groups.get(child_gid)
                    if child_group and hit in all_component_ids(groups, child_gid):
                        self._group_stack.append(child_gid)
                        self._clear_multi_selection()
                        self._deselect()
                        self._select_primary(hit)
                        return True
        else:
            top = top_level_group_for(groups, hit)
            if top is not None:
                self._group_stack.append(top)
                self._clear_multi_selection()
                self._deselect()
                self._select_primary(hit)
                return True
        return False

    def on_move(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)

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
        for cid in drag_ids:
            pl = self._session.layout.placements.get(cid)
            orig = self._drag_orig_positions.get(cid)
            if pl is None or orig is None:
                continue
            if pl.position == orig:
                continue
            old_pl = pl.copy()
            old_pl.position = orig

            # Check pad conflicts.
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

        self._drag_orig_wire_paths = {}

        if commands:
            if len(commands) == 1:
                self._session.stack.execute(commands[0], self._session)
            else:
                macro = MacroCommand(commands, description="Move selection")
                self._session.stack.execute(macro, self._session)
            self._session.layout_changed.emit()

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
            return

        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if self._selected_wire_ids:
                self._delete_multi_wires()
            elif self._selected_wire_id is not None:
                self._delete_wire()
            elif self._selected_ids:
                self._delete_multi()
            elif self._selected_id is not None:
                self._delete_selected()
        elif key == Qt.Key.Key_R:
            if self._selected_id is not None:
                self._rotate_selected()
        elif key == Qt.Key.Key_G and ctrl:
            self._group_for_selection()
        elif key == Qt.Key.Key_Escape:
            if self._group_stack:
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

        if not netted_pins:
            return False  # no netted pins — nothing to show, let escape_tool fire

        from PySide6.QtWidgets import QMenu

        menu = QMenu(self._view)
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

        cid, pin_num = chosen.data()
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
        from protoboard.domain.commands.component_cmds import SetWireInsulatedCommand

        menu = QMenu(self._view)
        label = "insulated" if wire.insulated else "bare"
        menu.addSection(f"Wire — currently {label}")
        if wire.insulated:
            act_bare = menu.addAction("Set bare (no insulation)")
            act_bare.setData(("insulated", False))
        else:
            act_ins = menu.addAction("Set insulated (add insulation jacket)")
            act_ins.setData(("insulated", True))

        chosen = menu.exec(
            self._view.mapToGlobal(self._view.mapFromScene(scene_pt))
        )
        if chosen is None:
            return True

        _, new_insulated = chosen.data()
        cmd = SetWireInsulatedCommand(wire_id=wire.id, insulated=new_insulated)
        from typing import cast
        from protoboard.domain.commands.base import Command
        self._session.stack.execute(
            cast(Command[ComponentState], cmd), self._session
        )
        self._session.layout_changed.emit()
        return True

    # ------------------------------------------------------------------
    # Single component selection
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

    def _select_primary(self, comp_id: uuid.UUID) -> None:
        """Set comp_id as the primary (Properties panel) selection."""
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
        xs = [p.x for p in positions]
        ys = [p.y for p in positions]
        m = _BBOX_MARGIN
        self._highlight = QGraphicsRectItem(
            QRectF(min(xs) - m, min(ys) - m,
                   max(xs) - min(xs) + 2 * m, max(ys) - min(ys) + 2 * m)
        )
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
        old_pl = placement.copy()
        new_pl = placement.copy()
        new_pl.rotation = new_rot
        cmd = MoveComponentCommand(
            component_id=comp_id,
            new_placement=new_pl,
            old_placement=old_pl,
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
        from typing import cast
        from protoboard.domain.commands.base import Command
        cmd = cast(Command[ComponentState], RemoveComponentCommand(component_id=comp_id))
        self._session.stack.execute(cmd, self._session)
        self._deselect()
        self._session.layout_changed.emit()

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
        rect.setPen(_MULTI_SELECT_PEN)
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
            bbox = component_bbox(defn.footprint, placement)
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
        group.setZValue(2.0)
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
        group = QGraphicsItemGroup()
        pts = wire.path
        for i in range(len(pts) - 1):
            seg = QGraphicsLineItem(
                pts[i].x, pts[i].y, pts[i + 1].x, pts[i + 1].y
            )
            seg.setPen(_WIRE_SELECT_PEN)
            group.addToGroup(seg)
        group.setZValue(2.0)
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
        routed = results[0].path
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

    def _resolve_group_hit(self, comp_id: uuid.UUID) -> uuid.UUID:
        """If comp_id is inside a group not yet entered, return the first
        component ID from that group (used as proxy for the group selection).
        The caller should treat this as a group-level selection.
        Returns comp_id unchanged if already at the correct depth."""
        groups = self._session.layout.groups
        current = self._group_stack[-1] if self._group_stack else None
        if current is not None:
            # Inside a group — check if comp_id is a direct member.
            g = groups.get(current)
            if g is not None and comp_id in g.component_ids:
                return comp_id
            # Check if it's in a child group.
            for child_id in (g.child_group_ids if g else frozenset()):
                if comp_id in all_component_ids(groups, child_id):
                    # Return first component of child group as proxy.
                    child_g = groups.get(child_id)
                    if child_g and child_g.component_ids:
                        return next(iter(child_g.component_ids))
            return comp_id
        else:
            top = top_level_group_for(groups, comp_id)
            if top is not None:
                g = groups.get(top)
                if g and g.component_ids:
                    return next(iter(g.component_ids))
            return comp_id

    def _current_group_component_ids(self) -> frozenset[uuid.UUID]:
        """Return the set of all component IDs accessible at the current
        group depth (direct members if inside a group, all if at top level)."""
        groups = self._session.layout.groups
        if not self._group_stack:
            return frozenset(self._session.layout.placements.keys())
        gid = self._group_stack[-1]
        g = groups.get(gid)
        if g is None:
            return frozenset()
        return all_component_ids(groups, gid)

    def _group_for_selection(self) -> uuid.UUID | None:
        """Create a new group from the current multi-selection and return its ID."""
        if len(self._selected_ids) < 2 and not self._selected_id:
            return None
        from protoboard.domain.commands.group_cmds import CreateGroupCommand
        from protoboard.domain.layout.group import ComponentGroup

        ids = frozenset(self._selected_ids)
        if self._selected_id and self._selected_id not in ids:
            ids = ids | {self._selected_id}
        if len(ids) < 1:
            return None
        new_group = ComponentGroup(
            id=uuid.uuid4(),
            name=f"Group{len(self._session.layout.groups) + 1}",
            component_ids=ids,
        )
        cmd = CreateGroupCommand(group=new_group)
        self._session.stack.execute(cmd, self._session)
        self._session.layout_changed.emit()
        return new_group.id

    def exit_group(self) -> None:
        """Pop the innermost group from the navigation stack (Esc)."""
        if self._group_stack:
            self._group_stack.pop()
            self._clear_multi_selection()
            self._deselect()

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
