# SPDX-License-Identifier: MIT
"""``SelectToolV2`` — the rewritten selection tool (Phases 2–3).

A thin ``_Tool`` that wires the headless-tested pieces together:

* :class:`~protoboard.gui.selection.hit_test.HitTester` — point → Selectable,
* :class:`~protoboard.gui.selection.service.SelectionService` — selection state
  and the active-pick routing,
* :class:`~protoboard.gui.highlight.controller.HighlightController` — dimming,
* :class:`~protoboard.gui.selection.drag_controller.DragController` and
  :class:`~protoboard.gui.selection.rubber_band_controller.RubberBandController`
  — the two press-drag-release gestures, behind one
  :class:`~protoboard.gui.selection.gesture.GestureController` seam.

Covers: hit-test → selection (SET/ADD/REMOVE via the modifier matrix),
rubber-band area-select, drag / floating of the selection, rotate / flip /
rotate-around-point, ESC-pop, group enter/exit, and routing clicks into an
active pick.  Wire-edit and net-relation selection arrive in later phases.

The tool keeps almost no logic of its own — on press it arms a gesture or
mutates the selection; on move / release it forwards to the active gesture; the
behaviour lives in tested, Qt-free controllers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import QGraphicsRectItem

from protoboard.domain.layout.layout import ROOT_GROUP_ID
from protoboard.geometry import Point
from protoboard.gui.highlight.controller import HighlightController
from protoboard.gui.selection.drag_controller import DragController, FloatingPhase
from protoboard.gui.selection.hit_test import HitTester, SelectionFilters
from protoboard.gui.selection.rubber_band_controller import RubberBandController
from protoboard.gui.selection.service import SelectionService
from protoboard.gui.selection_fsm import BasisScope, SetOp

if TYPE_CHECKING:
    import uuid

    from PySide6.QtGui import QKeyEvent, QMouseEvent

    from protoboard.domain.layout.group_tree import GroupTree
    from protoboard.gui.board_view import BoardView
    from protoboard.gui.drag_set import DragSet
    from protoboard.gui.selection.gesture import GestureController
    from protoboard.gui.selection.selectable import Selectable
    from protoboard.gui.session import EditorSession

# Rubber-band visuals (match the legacy tool's cyan band).
_BAND_PEN_LR = QPen(QColor(0, 200, 255, 180), 0.06)
_BAND_PEN_RL = QPen(QColor(0, 200, 255, 180), 0.06)
_BAND_PEN_RL.setStyle(Qt.PenStyle.DashLine)
_BAND_FILL_LR = QColor(0, 200, 255, 20)
_BAND_FILL_RL = QColor(0, 200, 255, 10)


class SelectToolV2:
    """Selection tool built on the SelectionService / HitTester / Highlight stack."""

    def __init__(self) -> None:
        self._session: EditorSession = None  # type: ignore[assignment]
        self._view: BoardView = None  # type: ignore[assignment]
        self._selection: SelectionService = None  # type: ignore[assignment]
        self._highlight: HighlightController = None  # type: ignore[assignment]
        self._group_stack: list[uuid.UUID] = [ROOT_GROUP_ID]
        # The in-flight press-drag-release gesture (drag or rubber-band), or
        # None.  on_move / on_release route here uniformly; the controller owns
        # its own start threshold and outcome.
        self._gesture: GestureController | None = None
        self._band_item: QGraphicsRectItem | None = None
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
        self._cancel_gesture()
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

        # 3. Normal selection. Resolve the hit.
        _basis, op = self._decode_modifiers(event)
        sel = self._hit_tester().hit(scene_pt.x(), scene_pt.y(), self._filters())

        #   3a. Empty space → arm a rubber-band gesture.
        if sel is None:
            self._gesture = self._arm_band(scene_pt)
            return True

        #   3b. A plain (no-modifier) press selects the hit, then arms a drag of
        #   the selection — the gesture's own threshold decides drag-vs-click.
        #   With Ctrl/Shift the press only mutates the set (no drag).
        if op is SetOp.SET:
            self._apply_selection(sel, op)
            drag = self._arm_drag(sel, scene_pt)
            if drag is not None:
                self._gesture = drag
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

    # -- gesture routing (drag + rubber-band) ------------------------------

    def on_move(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        if self._gesture is not None:
            self._gesture.update(Point(scene_pt.x(), scene_pt.y()))

    def on_release(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        gesture = self._gesture
        if gesture is None:
            return
        gesture.update(Point(scene_pt.x(), scene_pt.y()))
        moved = gesture.commit()
        # A drag that's now floating stays the active gesture (keys commit /
        # cancel it).  Everything else — committed, a sub-threshold click, or a
        # finished band — clears the gesture and refreshes the selection.
        if isinstance(gesture, DragController) and gesture.is_floating:
            return
        self._gesture = None
        if moved:
            self._refresh_highlight()
        if self._view is not None:
            self._view.refresh()

    # -- arming gestures ----------------------------------------------------

    def _arm_band(self, scene_pt: QPointF) -> RubberBandController:
        press = Point(scene_pt.x(), scene_pt.y())
        return RubberBandController(
            press_pt=press,
            comp_ids=list(self._session.layout.placements),
            bbox=self._session.component_bbox,
            draw=self._draw_band,
            clear=self._clear_band,
            select=self._band_select,
        )

    def _arm_drag(
        self, sel: Selectable, scene_pt: QPointF
    ) -> DragController | None:
        """Build a drag for the current selection grabbed at *sel*.

        When the grabbed entity is a group, or a component locked inside a group
        below the active context, the whole group moves as a monolith; otherwise
        the free component(s) move.  Returns None when the hit isn't draggable
        (e.g. a bare wire-vertex in a later phase) — the press stays a plain
        selection.
        """
        drag_set = self._build_drag_set(sel)
        if drag_set.is_empty:
            return None
        anchor_id = self._anchor_component(sel)
        if anchor_id is None:
            return None
        pl = self._session.layout.placements.get(anchor_id)
        if pl is None:
            return None
        anchor_pos = pl.position
        press = Point(scene_pt.x(), scene_pt.y())
        # Grab offset: where the cursor sits relative to the anchor, so the
        # anchor snaps under the cursor's grab point (legacy anchor-snap).
        grab = (anchor_pos.x - press.x, anchor_pos.y - press.y)

        from typing import cast

        from protoboard.gui.selection.drag_controller import DragContext

        return DragController(
            cast("DragContext", self._session),
            drag_set=drag_set,
            anchor_id=anchor_id,
            anchor_pos=anchor_pos,
            press_pt=press,
            grab_offset=grab,
            snap=self._snap,
            execute=self._execute_command,
            auto_accept_clear=True,
        )

    def _build_drag_set(self, sel: Selectable) -> DragSet:
        """Snapshot what the grab moves: free components, or whole groups.

        A press on a group (or on a component locked in a group below the active
        context) drags that group as a monolith; otherwise the selected free
        components move.  Built via :meth:`DragSet.from_selection`, which
        partitions locked-in-group components into their group monolith.
        """
        from protoboard.gui.drag_set import DragSet

        group_ids = self._grabbed_group_ids(sel)
        return DragSet.from_selection(
            selected_ids=self._selected_comp_ids(),
            selected_wire_ids=frozenset(),
            selected_group_ids=group_ids,
            groups_map=self._session.layout.groups,
            is_locked_in_group=self._is_locked_in_group,
        )

    def _grabbed_group_ids(self, sel: Selectable) -> frozenset[uuid.UUID]:
        """Group ids to move as monoliths for this grab.

        * A group hit → that group.
        * A component hit that is locked in a group below the active context →
          the outermost such group (so a click inside a nested group drags the
          group you see, not its innermost leaf).
        """
        import uuid as _uuid

        hit = sel.to_hit()
        if hit.kind == "group" and isinstance(hit.id, _uuid.UUID):
            return frozenset({hit.id})
        if hit.kind == "component" and isinstance(hit.id, _uuid.UUID):
            gid = self._draggable_group_for(hit.id)
            if gid is not None:
                return frozenset({gid})
        return frozenset()

    def _draggable_group_for(self, comp_id: uuid.UUID) -> uuid.UUID | None:
        """Outermost group containing *comp_id* below the active context, or None.

        None when the component is free, or when the active context *is* the
        innermost containing group (the user entered it, so members drag freely).
        The "below the active context" groups are those whose recursive member
        set does NOT include the active group — the outermost of these is the
        monolith you see and grab.
        """
        if not self._is_locked_in_group(comp_id):
            return None
        tree = self._session.group_tree()
        active = self._current_group_id
        candidates = [
            gid
            for gid in tree.groups_containing_component(comp_id)
            if gid != active and not self._is_ancestor_group(gid, active, tree)
        ]
        if not candidates:
            return None
        # Outermost = the candidate enclosing the most components.
        return max(candidates, key=lambda g: len(tree.all_component_ids(g)))

    @staticmethod
    def _is_ancestor_group(
        gid: uuid.UUID, ancestor: uuid.UUID, tree: GroupTree
    ) -> bool:
        """True when *ancestor* is *gid* or one of its parent groups.

        Walks the parent chain so a group nested under the active context is not
        chosen as the drag monolith (the user entered the context, so groups
        above it move freely / not at all here).
        """
        if ancestor == ROOT_GROUP_ID:
            return False
        cur: uuid.UUID | None = gid
        while cur is not None:
            if cur == ancestor:
                return True
            cur = tree.parent_group_of(cur)
        return False

    def _is_locked_in_group(self, comp_id: uuid.UUID) -> bool:
        """True when *comp_id* belongs to a group other than the active context."""
        tree = self._session.group_tree()
        containing = tree.groups_containing_component(comp_id)
        if not containing:
            return False
        # Free to drag alone only when the active context is one of its groups.
        return self._current_group_id not in containing

    def _anchor_component(self, sel: Selectable) -> uuid.UUID | None:
        import uuid as _uuid

        hit = sel.to_hit()
        if hit.kind == "component" and isinstance(hit.id, _uuid.UUID):
            return hit.id
        # Group grab: anchor on any member component (delta is shared).
        if hit.kind == "group" and isinstance(hit.id, _uuid.UUID):
            members = self._session.group_tree().all_component_ids(hit.id)
            anchored = next(iter(members), None)
            if anchored is not None:
                return anchored
        ids = self._selected_comp_ids()
        return next(iter(ids), None)

    def _snap(self, raw: Point, exclude: frozenset[uuid.UUID]) -> Point:
        from PySide6.QtCore import QPointF as _QPointF

        snapped = self._view.snap(_QPointF(raw.x, raw.y), exclude_ids=exclude)
        return Point(snapped.x, snapped.y)

    def _execute_command(self, cmd: object) -> bool:
        from typing import cast

        from protoboard.domain.commands.base import (
            Command,
            CommandRejectedError,
            ComponentState,
        )

        try:
            self._session.stack.execute(
                cast("Command[ComponentState]", cmd),
                cast("ComponentState", self._session),
            )
        except CommandRejectedError:
            return False
        self._session.layout_changed.emit()
        return True

    # -- rubber-band Qt seams ----------------------------------------------

    def _draw_band(self, a: Point, b: Point, ltr: bool) -> None:
        from PySide6.QtCore import QRectF

        x0, y0 = min(a.x, b.x), min(a.y, b.y)
        rect = QRectF(x0, y0, abs(b.x - a.x), abs(b.y - a.y))
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

    def _band_select(self, comp_ids: list[uuid.UUID]) -> None:
        from protoboard.gui.selection.adapters import ComponentSelectable

        picked: list[Selectable] = []
        for cid in comp_ids:
            pl = self._session.layout.placements.get(cid)
            if pl is not None:
                picked.append(ComponentSelectable(component_id=cid, placement=pl))
        if picked:
            self._selection.set_many(picked)

    # -- floating drag commit / cancel -------------------------------------

    def _confirm_floating(self) -> None:
        gesture = self._gesture
        if not isinstance(gesture, DragController):
            return
        outcome = gesture.confirm_floating()
        if outcome.phase is not FloatingPhase.FLOATING:
            self._gesture = None
            self._refresh_highlight()
            if outcome.dropped_wire_count:
                self._status(
                    f"Removed {outcome.dropped_wire_count} wire(s) (Ctrl+Z to undo)",
                    4000,
                )
            if self._view is not None:
                self._view.refresh()

    def _cancel_gesture(self) -> None:
        gesture = self._gesture
        if gesture is None:
            return
        gesture.cancel()
        self._gesture = None
        self._refresh_highlight()
        if self._view is not None:
            self._view.refresh()

    def _clear_band(self) -> None:
        if self._band_item is not None:
            try:
                self._view.scene().removeItem(self._band_item)
            except RuntimeError:
                pass
            self._band_item = None

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

        # A floating drag awaiting confirmation: Enter commits, Esc cancels.
        if isinstance(self._gesture, DragController) and self._gesture.is_floating:
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._confirm_floating()
                event.accept()
                return
            if key == Qt.Key.Key_Escape:
                self._cancel_gesture()
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
