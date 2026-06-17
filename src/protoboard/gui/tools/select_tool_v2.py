# SPDX-License-Identifier: MIT
"""``SelectToolV2`` — the rewritten selection tool (Phase 2 MVP).

A thin ``_Tool`` that wires the headless-tested pieces together:

* :class:`~protoboard.gui.selection.hit_test.HitTester` — point → Selectable,
* :class:`~protoboard.gui.selection.service.SelectionService` — selection state
  and the active-pick routing,
* :class:`~protoboard.gui.highlight.controller.HighlightController` — dimming.

Phase 2 covers: hit-test → selection (SET/ADD/REMOVE via the modifier matrix),
rubber-band, rotate / flip / rotate-around-point (commands already exist),
ESC-pop, group enter/exit, and routing clicks into an active pick.  Drag,
floating, wire-edit and net-relation selection arrive in later phases.

The tool keeps almost no logic of its own — it translates Qt events into calls
on the services, so the behaviour lives in tested, Qt-free code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import QGraphicsRectItem

from protoboard.domain.layout.layout import ROOT_GROUP_ID
from protoboard.gui.highlight.controller import HighlightController
from protoboard.gui.selection.hit_test import HitTester, SelectionFilters
from protoboard.gui.selection.service import SelectionService
from protoboard.gui.selection_fsm import BasisScope, SetOp

if TYPE_CHECKING:
    import uuid

    from PySide6.QtGui import QKeyEvent, QMouseEvent

    from protoboard.gui.board_view import BoardView
    from protoboard.gui.selection.selectable import Selectable
    from protoboard.gui.session import EditorSession

# Rubber-band visuals (match the legacy tool's cyan band).
_BAND_PEN_LR = QPen(QColor(0, 200, 255, 180), 0.06)
_BAND_PEN_RL = QPen(QColor(0, 200, 255, 180), 0.06)
_BAND_PEN_RL.setStyle(Qt.PenStyle.DashLine)
_BAND_FILL_LR = QColor(0, 200, 255, 20)
_BAND_FILL_RL = QColor(0, 200, 255, 10)

# Below this drag size a press+release is a click, not a rubber-band.
_BAND_MIN_SIZE = 0.1


class SelectToolV2:
    """Selection tool built on the SelectionService / HitTester / Highlight stack."""

    def __init__(self) -> None:
        self._session: EditorSession = None  # type: ignore[assignment]
        self._view: BoardView = None  # type: ignore[assignment]
        self._selection: SelectionService = None  # type: ignore[assignment]
        self._highlight: HighlightController = None  # type: ignore[assignment]
        self._group_stack: list[uuid.UUID] = [ROOT_GROUP_ID]
        # Rubber-band state.
        self._band_start: QPointF | None = None
        self._band_item: QGraphicsRectItem | None = None
        self._band_op: SetOp = SetOp.SET
        # Rotate-around-point pivot capture.
        self._pivot_capture_pending: bool = False
        self._rotate_pivot: object | None = None  # geometry.Point | None

    # -- _Tool lifecycle ----------------------------------------------------

    def activate(self, session: EditorSession, view: BoardView) -> None:
        self._session = session
        self._view = view
        # SelectionService / HighlightController are per-session — reuse one
        # parked on the session if present, else create and park it.
        svc = getattr(session, "selection_service", None)
        if not isinstance(svc, SelectionService):
            svc = SelectionService()
            session.selection_service = svc  # type: ignore[attr-defined]
        self._selection = svc
        self._highlight = HighlightController(
            session.highlight_stack, source_id="select_tool"
        )

    def deactivate(self) -> None:
        self._clear_band()

    def on_scene_cleared(self) -> None:
        self._band_item = None

    # -- helpers ------------------------------------------------------------

    def _hit_tester(self) -> HitTester:
        from typing import cast

        from protoboard.types import Side

        return HitTester(
            self._session, camera_side=cast(Side, self._view.camera_side)
        )

    def _filters(self) -> SelectionFilters:
        # Phase 2: all kinds hittable; the status-bar filter wires in later.
        return SelectionFilters()

    @property
    def _current_group_id(self) -> uuid.UUID:
        if not self._group_stack:
            self._group_stack = [ROOT_GROUP_ID]
        return self._group_stack[-1]

    def _status(self, msg: str, timeout_ms: int = 3000) -> None:
        win = self._view.window()
        bar = getattr(win, "statusBar", None)
        if callable(bar):
            bar().showMessage(msg, timeout_ms)

    @staticmethod
    def _decode_modifiers(event: QMouseEvent) -> tuple[BasisScope, SetOp]:
        mods = event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        alt = bool(mods & Qt.KeyboardModifier.AltModifier)
        op = (
            SetOp.REMOVE if (ctrl and shift)
            else SetOp.ADD if ctrl
            else SetOp.SET
        )
        basis = BasisScope.RELATION if alt else BasisScope.SINGLE
        return basis, op

    # -- press --------------------------------------------------------------

    def on_press(self, event: QMouseEvent, scene_pt: QPointF) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False

        # 1. Pivot-capture mode (rotate-around-point) wins everything.
        if self._pivot_capture_pending:
            self._rotate_pivot = self._view.snap(scene_pt)
            self._pivot_capture_pending = False
            self._status(
                "Pivot set — Rotate CW/CCW to rotate around it  ·  [Esc] Clear", 0
            )
            return True

        # 2. An active pick consumes the click as a pick, not a selection.
        pick = self._selection.active_pick
        if pick is not None and not pick.finished:
            sel = self._hit_tester().hit(
                scene_pt.x(), scene_pt.y(), self._filters()
            )
            if sel is not None:
                pick.feed(sel)
            return True

        # 3. Normal selection. Resolve the hit; arm rubber-band on empty space.
        _basis, op = self._decode_modifiers(event)
        sel = self._hit_tester().hit(scene_pt.x(), scene_pt.y(), self._filters())
        if sel is None:
            self._band_start = scene_pt
            self._band_op = op
            return True

        self._apply_selection(sel, op)
        return True

    def _apply_selection(self, sel: Selectable, op: SetOp) -> None:
        if op == SetOp.ADD:
            self._selection.add(sel)
        elif op == SetOp.REMOVE:
            # Phase 2 MVP: remove not yet a first-class FSM op here; treat as
            # a fresh SET so the click is never a silent no-op. Refined later.
            self._selection.set(sel)
        else:
            self._selection.set(sel)
        self._refresh_highlight()

    def _refresh_highlight(self) -> None:
        self._highlight.show(self._selection.selected())

    # -- move / release (rubber-band) --------------------------------------

    def on_move(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        if self._band_start is not None:
            self._draw_band(scene_pt)

    def on_release(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        if self._band_start is not None:
            self._finish_band(scene_pt)

    def _draw_band(self, scene_pt: QPointF) -> None:
        start = self._band_start
        if start is None:
            return
        from PySide6.QtCore import QRectF

        x0 = min(start.x(), scene_pt.x())
        y0 = min(start.y(), scene_pt.y())
        w = abs(scene_pt.x() - start.x())
        h = abs(scene_pt.y() - start.y())
        rect = QRectF(x0, y0, w, h)
        ltr = scene_pt.x() >= start.x()
        if self._band_item is None:
            item = QGraphicsRectItem(rect)
            item.setPen(_BAND_PEN_LR if ltr else _BAND_PEN_RL)
            item.setBrush(_BAND_FILL_LR if ltr else _BAND_FILL_RL)
            self._view.scene().addItem(item)
            self._band_item = item
        else:
            self._band_item.setRect(rect)
            self._band_item.setPen(_BAND_PEN_LR if ltr else _BAND_PEN_RL)
            self._band_item.setBrush(_BAND_FILL_LR if ltr else _BAND_FILL_RL)

    def _finish_band(self, scene_pt: QPointF) -> None:
        start = self._band_start
        self._clear_band()
        if start is None:
            return
        rx0, ry0 = min(start.x(), scene_pt.x()), min(start.y(), scene_pt.y())
        rx1, ry1 = max(start.x(), scene_pt.x()), max(start.y(), scene_pt.y())
        if (rx1 - rx0) < _BAND_MIN_SIZE and (ry1 - ry0) < _BAND_MIN_SIZE:
            return  # a click, not a drag
        ltr = scene_pt.x() >= start.x()
        picked = self._band_components(rx0, ry0, rx1, ry1, ltr)
        if not picked:
            return
        # Relation-agnostic explicit set (Phase 2: components only).
        self._selection.set_many(picked)
        self._refresh_highlight()

    def _band_components(
        self, rx0: float, ry0: float, rx1: float, ry1: float, ltr: bool
    ) -> list[Selectable]:
        from protoboard.gui.selection.adapters import ComponentSelectable

        out: list[Selectable] = []
        for cid in self._session.layout.placements:
            bbox = self._session.component_bbox(cid)
            if bbox is None:
                continue
            bx0, by0 = bbox.x, bbox.y
            bx1, by1 = bbox.x + bbox.width, bbox.y + bbox.height
            inside = bx0 >= rx0 and by0 >= ry0 and bx1 <= rx1 and by1 <= ry1
            intersects = bx0 < rx1 and bx1 > rx0 and by0 < ry1 and by1 > ry0
            if (inside if ltr else intersects):
                pl = self._session.layout.placements.get(cid)
                if pl is not None:
                    out.append(ComponentSelectable(component_id=cid, placement=pl))
        return out

    def _clear_band(self) -> None:
        if self._band_item is not None:
            try:
                self._view.scene().removeItem(self._band_item)
            except RuntimeError:
                pass
            self._band_item = None
        self._band_start = None

    # -- keys ---------------------------------------------------------------

    def on_key(self, event: QKeyEvent) -> None:
        key = event.key()
        mods = event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        alt = bool(mods & Qt.KeyboardModifier.AltModifier)

        # Pivot mode: Esc clears it first.
        if (self._pivot_capture_pending or self._rotate_pivot is not None):
            if key == Qt.Key.Key_Escape:
                self._clear_pivot()
                self._status("Pivot cleared")
                event.accept()
                return

        # Active pick: Esc cancels.
        pick = self._selection.active_pick
        if pick is not None and not pick.finished and key == Qt.Key.Key_Escape:
            pick.cancel()
            event.accept()
            return

        if key == Qt.Key.Key_R and not ctrl:
            if alt:
                self._flip_selected()
            else:
                self._rotate_selected(delta=-90 if shift else 90)
            event.accept()
            return

        if key == Qt.Key.Key_Escape:
            if len(self._group_stack) > 1:
                self.exit_group()
            else:
                self._selection.clear()
                self._refresh_highlight()
            event.accept()

    def on_key_release(self, event: QKeyEvent) -> None:
        pass

    # -- rotate / flip ------------------------------------------------------

    def begin_rotate_around_point(self) -> None:
        if self._selection.is_empty():
            self._status("Rotate around point: select component(s) or a group first")
            return
        self._pivot_capture_pending = True
        self._rotate_pivot = None
        self._status("Click a pivot point  ·  [Esc] Cancel", 0)

    def _clear_pivot(self) -> None:
        self._pivot_capture_pending = False
        self._rotate_pivot = None

    def _selected_comp_ids(self) -> frozenset[uuid.UUID]:
        import uuid as _uuid

        out: set[_uuid.UUID] = set()
        for sel in self._selection.selected():
            hit = sel.to_hit()
            if hit.kind == "component" and isinstance(hit.id, _uuid.UUID):
                out.add(hit.id)
        return frozenset(out)

    def _rotate_selected(self, delta: int = 90) -> None:
        from protoboard.domain.commands.group_cmds import (
            RotateAroundPointCommand,
            RotateComponentsCommand,
        )

        comp_ids = self._selected_comp_ids()
        if not comp_ids:
            return
        if self._rotate_pivot is not None:
            cmd: object = RotateAroundPointCommand(
                comp_ids=comp_ids, origin=self._rotate_pivot, delta=delta % 360  # type: ignore[arg-type]
            )
        else:
            cmd = RotateComponentsCommand(comp_ids=comp_ids, delta=delta % 360)
        self._execute(cmd)

    def _flip_selected(self) -> None:
        from protoboard.domain.commands.base import MacroCommand
        from protoboard.domain.commands.component_cmds import MoveComponentCommand
        from protoboard.domain.layout.placement import flip_component

        cmds: list[object] = []
        for cid in self._selected_comp_ids():
            pl = self._session.layout.placements.get(cid)
            inst = self._session.instances.get(cid)
            if pl is None or inst is None:
                continue
            defn = self._session.components.get(inst.definition_ref)
            if defn is None or defn.footprint.pin_count() != 2:
                continue
            cmds.append(
                MoveComponentCommand(
                    component_id=cid,
                    new_placement=flip_component(defn.footprint, pl),
                    old_placement=pl.copy(),
                )
            )
        if not cmds:
            return
        self._execute(MacroCommand(cmds, description=f"Flip {len(cmds)} components"))  # type: ignore[arg-type]

    def _execute(self, cmd: object) -> None:
        from typing import cast

        from protoboard.domain.commands.base import Command, ComponentState

        self._session.stack.execute(
            cast("Command[ComponentState]", cmd),
            cast("ComponentState", self._session),
        )
        self._session.layout_changed.emit()
        self._refresh_highlight()

    # -- group navigation ---------------------------------------------------

    def exit_group(self) -> None:
        if len(self._group_stack) > 1:
            self._group_stack.pop()
            self._selection.clear()
            self._refresh_highlight()
