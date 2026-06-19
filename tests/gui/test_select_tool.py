# SPDX-License-Identifier: MIT
"""Integration tests for SelectTool — the rewritten selection tool (Phase 2).

Drives the new tool through a real BoardView/session, asserting it wires the
headless SelectionService / HitTester / HighlightController correctly: click to
select, rubber-band, rotate, ESC-pop, and routing a click into an active pick.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent

from protoboard.domain.components.instance import ComponentInstance
from protoboard.gui.board_view import BoardView
from protoboard.gui.selection import Stop
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.place_tool import PlaceTool
from protoboard.gui.tools.select_tool import SelectTool


def _place(
    board_view: BoardView, session: EditorSession, x: float, y: float
) -> ComponentInstance:
    board_view.set_tool(PlaceTool("R"))
    tool = board_view._active_tool
    assert isinstance(tool, PlaceTool)
    ev = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(x, y), QPointF(x, y),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    tool.on_press(ev, QPointF(x, y))
    while tool._phase == "drag_pin":
        tool.on_press(ev, QPointF(x, y))
    return list(session.instances.values())[-1]


def _press(
    tool: SelectTool, x: float, y: float,
    mods: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
) -> None:
    tool.on_press(
        QMouseEvent(
            QMouseEvent.Type.MouseButtonPress,
            QPointF(x, y), QPointF(x, y),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, mods,
        ),
        QPointF(x, y),
    )


def _move(tool: SelectTool, x: float, y: float) -> None:
    tool.on_move(
        QMouseEvent(
            QMouseEvent.Type.MouseMove,
            QPointF(x, y), QPointF(x, y),
            Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        ),
        QPointF(x, y),
    )


def _release(tool: SelectTool, x: float, y: float) -> None:
    tool.on_release(
        QMouseEvent(
            QMouseEvent.Type.MouseButtonRelease,
            QPointF(x, y), QPointF(x, y),
            Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        ),
        QPointF(x, y),
    )


def _click(
    tool: SelectTool, x: float, y: float,
    mods: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
) -> None:
    """Press + release at the same point — a click (no drag)."""
    _press(tool, x, y, mods)
    _release(tool, x, y)


def _key(
    tool: SelectTool, key: Qt.Key,
    mods: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
) -> None:
    tool.on_key(QKeyEvent(QKeyEvent.Type.KeyPress, key, mods))


def _v2(board_view: BoardView) -> SelectTool:
    board_view.set_tool(SelectTool())
    tool = board_view._active_tool
    assert isinstance(tool, SelectTool)
    return tool


def _comp_ids(tool: SelectTool) -> set:
    return {h.id for h in (s.to_hit() for s in tool._selection.selected())
            if h.kind == "component"}


class TestSelectionBasics:
    def test_click_selects_component(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 0.5, 0.5)
        tool = _v2(board_view)
        _press(tool, 0.5, 0.5)
        assert inst.id in _comp_ids(tool)

    def test_click_empty_clears_via_band_noop(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 0.5, 0.5)
        tool = _v2(board_view)
        _press(tool, 0.5, 0.5)
        assert inst.id in _comp_ids(tool)
        # Press on empty space arms a band; releasing in place (no drag) is a
        # no-op click — selection persists (no accidental clear).
        _press(tool, 8.0, 8.0)
        _release(tool, 8.0, 8.0)
        assert inst.id in _comp_ids(tool)


class TestCyanSelectionOutline:
    """A selected component's body outline is recoloured to the select colour."""

    @staticmethod
    def _outline_colours(board_view: BoardView, comp_id):  # type: ignore[no-untyped-def]
        from PySide6.QtWidgets import QGraphicsItemGroup

        grp = board_view._comp_groups.get(comp_id)
        if not isinstance(grp, QGraphicsItemGroup):
            return []
        out = []
        for child in grp.childItems():
            if hasattr(child, "pen"):
                out.append(child.pen().color().rgb() & 0x00FFFFFF)
        return out

    def test_selected_component_outline_turns_cyan(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        from protoboard.gui.theme import DEFAULT_THEME

        inst = _place(board_view, session, 0.5, 0.5)
        board_view.refresh()
        border_rgb = QColor(DEFAULT_THEME.comp_border).rgb() & 0x00FFFFFF
        select_rgb = QColor(DEFAULT_THEME.select_color).rgb() & 0x00FFFFFF

        before = self._outline_colours(board_view, inst.id)
        assert border_rgb in before  # the dark outline exists pre-selection

        tool = _v2(board_view)
        _press(tool, 0.5, 0.5)
        after = self._outline_colours(board_view, inst.id)
        assert select_rgb in after  # outline recoloured to cyan
        assert border_rgb not in after  # the dark outline is gone

    def test_deselect_restores_outline(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        from protoboard.gui.theme import DEFAULT_THEME

        inst = _place(board_view, session, 0.5, 0.5)
        board_view.refresh()
        border_rgb = QColor(DEFAULT_THEME.comp_border).rgb() & 0x00FFFFFF
        select_rgb = QColor(DEFAULT_THEME.select_color).rgb() & 0x00FFFFFF

        tool = _v2(board_view)
        _click(tool, 0.5, 0.5)  # press+release — a completed click (no armed drag)
        assert select_rgb in self._outline_colours(board_view, inst.id)

        # ESC clears the selection → outline returns to the neutral border.
        _key(tool, Qt.Key.Key_Escape)
        restored = self._outline_colours(board_view, inst.id)
        assert border_rgb in restored
        assert select_rgb not in restored

    def test_scene_rebuild_while_selected_does_not_crash(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """A scene rebuild (as during drag) must not leave dead overlay refs.

        Regression: the cyan SVG-overlay rects were kept across a scene clear,
        so the next recolour pass touched already-deleted C++ items
        (libshiboken "already deleted") — which aborted the highlight refresh
        and broke dragging.
        """
        inst = _place(board_view, session, 0.5, 0.5)
        board_view.refresh()
        tool = _v2(board_view)
        _click(tool, 0.5, 0.5)  # select → may register a cyan / SVG overlay
        assert inst.id in _comp_ids(tool)

        # Simulate the scene wipe + rebuild that a drag refresh performs.
        board_view._invalidate_all_groups()
        assert not board_view._svg_select_overlays  # refs dropped, not dangling
        assert not board_view._cyan_outlined
        board_view.refresh()

        # A subsequent highlight pass (the one that crashed) must be clean.
        board_view._apply_highlight_opacity_impl()
        tool._refresh_highlight()


class TestGroupSelectAndEdit:
    """A group selects, shows a cyan outline, and [E] enters its edit context."""

    @staticmethod
    def _make_group(board_view, session):  # type: ignore[no-untyped-def]
        import uuid as _uuid

        from protoboard.domain.layout.group import ComponentGroup

        a = _place(board_view, session, 1.0, 1.0)
        b = _place(board_view, session, 5.0, 5.0)
        gid = _uuid.uuid4()
        session.layout.groups[gid] = ComponentGroup(
            id=gid, name="G", component_ids=frozenset({a.id, b.id})
        )
        session.invalidate_graph()
        board_view.refresh()
        return gid

    @staticmethod
    def _group_outline_rgb(board_view, gid):  # type: ignore[no-untyped-def]
        from PySide6.QtWidgets import QGraphicsRectItem

        for it in board_view._scene.items():
            if (
                isinstance(it, QGraphicsRectItem)
                and it.data(1) == "group_outline"
                and it.data(0) == str(gid)
            ):
                return it.pen().color().rgb() & 0x00FFFFFF
        return None

    def test_group_interior_click_selects_group(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        gid = self._make_group(board_view, session)
        tool = _v2(board_view)
        _click(tool, 3.0, 3.0)  # interior, between the two members
        assert gid in set(tool._selected_group_ids)

    def test_selected_group_outline_turns_cyan(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        from protoboard.gui.theme import DEFAULT_THEME

        gid = self._make_group(board_view, session)
        select_rgb = QColor(DEFAULT_THEME.select_color).rgb() & 0x00FFFFFF
        tool = _v2(board_view)
        _click(tool, 3.0, 3.0)
        assert self._group_outline_rgb(board_view, gid) == select_rgb

    def test_e_enters_selected_group(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        gid = self._make_group(board_view, session)
        tool = _v2(board_view)
        _click(tool, 3.0, 3.0)
        assert tool._current_group_id != gid  # at root before entering
        _key(tool, Qt.Key.Key_E)
        assert tool._current_group_id == gid  # entered the group's context
        assert board_view._active_group_id == gid  # view re-rendered in-context
        _key(tool, Qt.Key.Key_Escape)
        assert tool._current_group_id != gid  # ESC leaves the group


class TestSelectionBasicsExtra:
    def test_highlight_published_on_select(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 0.5, 0.5)
        tool = _v2(board_view)
        _press(tool, 0.5, 0.5)
        entry = session.highlight_stack.active()
        assert entry is not None
        assert inst.id in entry.comp_ids


class TestRubberBand:
    def test_ltr_selects_components_inside(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        a = _place(board_view, session, 1.0, 1.0)
        b = _place(board_view, session, 4.0, 4.0)
        tool = _v2(board_view)
        # Build a left-to-right band that provably encloses both bboxes.
        bxs = [session.component_bbox(cid) for cid in (a.id, b.id)]
        assert all(bb is not None for bb in bxs)
        x0 = min(bb.x for bb in bxs) - 0.5  # type: ignore[union-attr]
        y0 = min(bb.y for bb in bxs) - 0.5  # type: ignore[union-attr]
        x1 = max(bb.x + bb.width for bb in bxs) + 0.5  # type: ignore[union-attr]
        y1 = max(bb.y + bb.height for bb in bxs) + 0.5  # type: ignore[union-attr]
        _press(tool, x0, y0)
        _move(tool, x1, y1)
        _release(tool, x1, y1)
        ids = _comp_ids(tool)
        assert a.id in ids
        assert b.id in ids

    def test_rtl_selects_intersecting(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        a = _place(board_view, session, 1.0, 1.0)
        tool = _v2(board_view)
        bb = session.component_bbox(a.id)
        assert bb is not None
        # Right-to-left band (end.x < start.x) that merely clips the bbox.
        cx, cy = bb.x + bb.width / 2, bb.y + bb.height / 2
        _press(tool, cx + 5.0, cy + 5.0)
        _move(tool, cx, cy)
        _release(tool, cx, cy)
        assert a.id in _comp_ids(tool)


class TestRotate:
    def test_r_arms_then_steps_rotation(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        # [R] now ARMS float-rotate; a second [R] (or wheel/arrow) steps 90°.
        inst = _place(board_view, session, 0.5, 0.5)
        tool = _v2(board_view)
        _press(tool, 0.5, 0.5)
        before = session.layout.placements[inst.id].rotation
        _key(tool, Qt.Key.Key_R)  # arm
        assert tool._armed is not None
        assert session.layout.placements[inst.id].rotation == before  # not yet
        _key(tool, Qt.Key.Key_R)  # step +90
        assert session.layout.placements[inst.id].rotation == (before + 90) % 360

    def test_rotate_around_point_moves_component(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        # Legacy pivot-capture seeds the armed session's shared pivot; the part
        # then orbits it when stepped.
        inst = _place(board_view, session, 0.5, 0.5)
        tool = _v2(board_view)
        _press(tool, 0.5, 0.5)
        before = session.layout.placements[inst.id].position
        tool.begin_rotate_around_point()
        assert tool._pivot_capture_pending is True
        _press(tool, 3.0, 3.0)  # capture pivot away from the part
        assert tool._rotate_pivot is not None
        _key(tool, Qt.Key.Key_R)  # arm (seeds pivot from the captured point)
        _key(tool, Qt.Key.Key_R)  # step
        after = session.layout.placements[inst.id].position
        assert (after.x, after.y) != (before.x, before.y)

    def test_esc_clears_pivot(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        _place(board_view, session, 0.5, 0.5)
        tool = _v2(board_view)
        _press(tool, 0.5, 0.5)
        tool.begin_rotate_around_point()
        _press(tool, 3.0, 3.0)
        assert tool._rotate_pivot is not None
        _key(tool, Qt.Key.Key_Escape)
        assert tool._rotate_pivot is None


class TestArmedRotate:
    """[R] arms a float-rotate the selection stays live in."""

    def _armed(self, board_view, session):
        inst = _place(board_view, session, 0.5, 0.5)
        tool = _v2(board_view)
        _press(tool, 0.5, 0.5)
        _key(tool, Qt.Key.Key_R)  # arm
        assert tool._armed is not None
        return inst, tool

    def test_up_down_step_cw_ccw(self, board_view, session) -> None:
        inst, tool = self._armed(board_view, session)
        base = session.layout.placements[inst.id].rotation
        _key(tool, Qt.Key.Key_Up)
        assert session.layout.placements[inst.id].rotation == (base + 90) % 360
        _key(tool, Qt.Key.Key_Down)
        assert session.layout.placements[inst.id].rotation == base

    def test_wheel_steps_rotation(self, board_view, session) -> None:
        from PySide6.QtCore import QPoint
        from PySide6.QtGui import QWheelEvent

        inst, tool = self._armed(board_view, session)
        base = session.layout.placements[inst.id].rotation
        assert tool.wants_wheel(object())  # claims the wheel while armed
        ev = QWheelEvent(
            QPointF(0, 0), QPointF(0, 0), QPoint(0, 0), QPoint(0, 120),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase, False,
        )
        tool.on_wheel(ev)
        assert session.layout.placements[inst.id].rotation == (base + 90) % 360

    def test_esc_cancels_whole_session(self, board_view, session) -> None:
        inst, tool = self._armed(board_view, session)
        base = session.layout.placements[inst.id].rotation
        _key(tool, Qt.Key.Key_R)  # +90
        _key(tool, Qt.Key.Key_R)  # +90  (now base+180)
        assert session.layout.placements[inst.id].rotation == (base + 180) % 360
        _key(tool, Qt.Key.Key_Escape)  # cancel the whole session
        assert tool._armed is None
        assert session.layout.placements[inst.id].rotation == base

    def test_enter_accepts_keeps_rotation(self, board_view, session) -> None:
        inst, tool = self._armed(board_view, session)
        base = session.layout.placements[inst.id].rotation
        _key(tool, Qt.Key.Key_R)  # +90
        _key(tool, Qt.Key.Key_Return)  # accept
        assert tool._armed is None
        assert session.layout.placements[inst.id].rotation == (base + 90) % 360

    def test_pad_click_sets_shared_pivot(self, board_view, session) -> None:
        inst, tool = self._armed(board_view, session)
        before = session.layout.placements[inst.id].position
        _press(tool, 3.0, 3.0)  # click a pad away from the part → shared pivot
        assert tool._armed is not None and tool._armed.pivot is not None
        _key(tool, Qt.Key.Key_R)  # step → orbit the pivot, position changes
        after = session.layout.placements[inst.id].position
        assert (after.x, after.y) != (before.x, before.y)

    def test_pivot_markers_shown_while_armed_cleared_on_disarm(
        self, board_view, session
    ) -> None:
        # Visual feedback: arming draws pivot marker(s); disarm clears them.
        inst, tool = self._armed(board_view, session)
        assert tool._pivot_marker_items  # per-object marker drawn on arm
        _press(tool, 3.0, 3.0)  # shared pad pivot
        assert tool._armed is not None and tool._armed.pivot is not None
        assert tool._pivot_marker_items  # re-drawn on the shared pad
        _key(tool, Qt.Key.Key_Escape)  # cancel → disarm
        assert tool._armed is None
        assert tool._pivot_marker_items == []  # markers cleared

    def test_accept_parks_armed_on_conflict(self, board_view, session) -> None:
        # Conditional-disarm contract: an accept that cannot commit (HARD
        # conflict) leaves the sub-tool armed, waiting — it does NOT disarm.
        from protoboard.gui.tools.drag_commit_decision import (
            DragCommitDecision,
            DragSeverity,
        )

        inst, tool = self._armed(board_view, session)
        _key(tool, Qt.Key.Key_R)  # step once so there is a non-zero rotation
        armed = tool._armed
        assert armed is not None
        # Force the classifier to report a HARD (un-committable) state.
        armed._classify = lambda: DragCommitDecision(severity=DragSeverity.HARD)  # type: ignore[attr-defined]
        assert armed.can_commit() is False
        _key(tool, Qt.Key.Key_Return)  # accept → must stay armed
        assert tool._armed is armed  # not disarmed
        assert tool._armed.finished is False


class TestArmedMove:
    """[G] arms a keyboard-driven float-move (arrows / WASD)."""

    def _armed(self, board_view, session):
        inst = _place(board_view, session, 0.5, 0.5)
        tool = _v2(board_view)
        _press(tool, 0.5, 0.5)
        assert not tool._selection.is_empty()
        _key(tool, Qt.Key.Key_G)  # arm
        assert tool._armed is not None
        return inst, tool

    def test_arrows_then_enter_commits_move(self, board_view, session) -> None:
        inst, tool = self._armed(board_view, session)
        before = session.layout.placements[inst.id].position
        _key(tool, Qt.Key.Key_Right)
        _key(tool, Qt.Key.Key_Down)
        _key(tool, Qt.Key.Key_Return)  # drop
        assert tool._armed is None
        after = session.layout.placements[inst.id].position
        assert (after.x, after.y) == (before.x + 1.0, before.y + 1.0)

    def test_wasd_steps(self, board_view, session) -> None:
        inst, tool = self._armed(board_view, session)
        before = session.layout.placements[inst.id].position
        _key(tool, Qt.Key.Key_D)  # right
        _key(tool, Qt.Key.Key_S)  # down
        _key(tool, Qt.Key.Key_Return)
        after = session.layout.placements[inst.id].position
        assert (after.x, after.y) == (before.x + 1.0, before.y + 1.0)

    def test_esc_cancels_move(self, board_view, session) -> None:
        inst, tool = self._armed(board_view, session)
        before = session.layout.placements[inst.id].position
        _key(tool, Qt.Key.Key_Right)
        _key(tool, Qt.Key.Key_Escape)  # cancel — no commit
        assert tool._armed is None
        after = session.layout.placements[inst.id].position
        assert (after.x, after.y) == (before.x, before.y)


class TestArmedAtomic:
    """_invoke_armed_atomic: arm -> one action -> accept, conditional disarm."""

    def _step_cw(self, armed) -> None:
        # The Up arrow is one CW step on the armed sub-tool's public input.
        armed.on_key(QKeyEvent(
            QKeyEvent.Type.KeyPress, Qt.Key.Key_Up, Qt.KeyboardModifier.NoModifier,
        ))

    def test_atomic_rotate_commits_and_disarms(self, board_view, session) -> None:
        inst = _place(board_view, session, 0.5, 0.5)
        tool = _v2(board_view)
        _press(tool, 0.5, 0.5)
        base = session.layout.placements[inst.id].rotation
        depth = len(session.stack._undo)  # type: ignore[attr-defined]

        ok = tool._invoke_armed_atomic(tool.arm_rotate, self._step_cw)

        assert ok is True
        assert tool._armed is None  # disarmed
        assert session.layout.placements[inst.id].rotation == (base + 90) % 360
        # One rotation = one undo entry (preview-then-commit).
        assert len(session.stack._undo) == depth + 1  # type: ignore[attr-defined]

    def test_atomic_noop_on_empty_selection(self, board_view, session) -> None:
        tool = _v2(board_view)  # nothing selected
        ok = tool._invoke_armed_atomic(tool.arm_rotate, self._step_cw)
        assert ok is False
        assert tool._armed is None  # never armed

    def test_atomic_parks_armed_on_conflict(self, board_view, session) -> None:
        from protoboard.gui.tools.drag_commit_decision import (
            DragCommitDecision,
            DragSeverity,
        )

        inst = _place(board_view, session, 0.5, 0.5)
        tool = _v2(board_view)
        _press(tool, 0.5, 0.5)

        def step_then_force_conflict(armed) -> None:
            self._step_cw(armed)
            # Make the post-step state report a HARD (un-committable) conflict.
            armed._classify = lambda: DragCommitDecision(  # type: ignore[attr-defined]
                severity=DragSeverity.HARD
            )

        ok = tool._invoke_armed_atomic(tool.arm_rotate, step_then_force_conflict)

        assert ok is False
        assert tool._armed is not None  # parked, waiting for the user
        assert tool._armed.finished is False


class TestDragGesture:
    def test_drag_component_moves_and_commits(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 0.5, 0.5)
        tool = _v2(board_view)
        _press(tool, 0.5, 0.5)  # select + arm drag (press on the pin)
        before = session.layout.placements[inst.id].position
        depth = len(session.stack._undo)  # type: ignore[attr-defined]
        _move(tool, 2.5, 0.5)  # past the 0.5 threshold
        _release(tool, 2.5, 0.5)
        after = session.layout.placements[inst.id].position
        assert (after.x, after.y) != (before.x, before.y)
        # CLEAR drag auto-commits → exactly one command on the stack.
        assert len(session.stack._undo) == depth + 1  # type: ignore[attr-defined]
        assert tool._mouse is None

    def test_subthreshold_press_release_is_click(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 0.5, 0.5)
        tool = _v2(board_view)
        _press(tool, 0.5, 0.5)
        before = session.layout.placements[inst.id].position
        depth = len(session.stack._undo)  # type: ignore[attr-defined]
        _move(tool, 0.6, 0.5)  # below threshold
        _release(tool, 0.6, 0.5)
        after = session.layout.placements[inst.id].position
        assert (after.x, after.y) == (before.x, before.y)
        assert len(session.stack._undo) == depth  # type: ignore[attr-defined]
        # Selection survives a click that didn't become a drag.
        assert inst.id in _comp_ids(tool)

    def test_drag_keeps_component_selected(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 0.5, 0.5)
        tool = _v2(board_view)
        _press(tool, 0.5, 0.5)
        _move(tool, 2.5, 0.5)
        _release(tool, 2.5, 0.5)
        assert inst.id in _comp_ids(tool)


def _band_select(tool: SelectTool, session: EditorSession, ids: list) -> None:
    """Rubber-band a set of components into the selection (relation-agnostic)."""
    bxs = [session.component_bbox(cid) for cid in ids]
    assert all(bb is not None for bb in bxs)
    x0 = min(bb.x for bb in bxs) - 0.5  # type: ignore[union-attr]
    y0 = min(bb.y for bb in bxs) - 0.5  # type: ignore[union-attr]
    x1 = max(bb.x + bb.width for bb in bxs) + 0.5  # type: ignore[union-attr]
    y1 = max(bb.y + bb.height for bb in bxs) + 0.5  # type: ignore[union-attr]
    _press(tool, x0, y0)
    _move(tool, x1, y1)
    _release(tool, x1, y1)


class TestPressReleaseSelection:
    """Selecting on press, but deferring collapse / deselect to a click release.

    A press on an already-selected entity must NOT mutate the selection (so a
    drag moves the whole multi-selection); if the gesture is a click, the
    release collapses onto / deselects the entity.
    """

    def test_press_on_selected_keeps_multiselection_for_drag(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        a = _place(board_view, session, 0.5, 0.5)
        b = _place(board_view, session, 4.5, 0.5)
        tool = _v2(board_view)
        _band_select(tool, session, [a.id, b.id])
        assert {a.id, b.id} <= _comp_ids(tool)
        # Press on one of them, then drag — the WHOLE selection must move, so
        # the press must not have collapsed to the grabbed component.
        _press(tool, 0.5, 0.5)
        assert {a.id, b.id} <= _comp_ids(tool)  # still both at press time
        before_b = session.layout.placements[b.id].position
        _move(tool, 2.5, 0.5)  # past threshold
        _release(tool, 2.5, 0.5)
        after_b = session.layout.placements[b.id].position
        assert (after_b.x, after_b.y) != (before_b.x, before_b.y)

    def test_click_on_selected_in_multiselection_collapses(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        a = _place(board_view, session, 0.5, 0.5)
        b = _place(board_view, session, 4.5, 0.5)
        tool = _v2(board_view)
        _band_select(tool, session, [a.id, b.id])
        assert {a.id, b.id} <= _comp_ids(tool)
        # A click (press+release, no drag) on one collapses to just that one.
        _click(tool, 0.5, 0.5)
        assert _comp_ids(tool) == {a.id}

    def test_click_on_sole_selection_deselects(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 0.5, 0.5)
        tool = _v2(board_view)
        _click(tool, 0.5, 0.5)
        assert inst.id in _comp_ids(tool)
        # Clicking the only selected component again deselects it.
        _click(tool, 0.5, 0.5)
        assert _comp_ids(tool) == set()

    def test_click_on_unselected_selects_on_press(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        a = _place(board_view, session, 0.5, 0.5)
        b = _place(board_view, session, 4.5, 0.5)
        tool = _v2(board_view)
        _press(tool, 0.5, 0.5)
        assert _comp_ids(tool) == {a.id}  # selected on press, no release yet
        _release(tool, 0.5, 0.5)
        # Pressing an unselected component selects it immediately (on press),
        # replacing the previous selection.
        _press(tool, 4.5, 0.5)
        assert _comp_ids(tool) == {b.id}


def _make_group(
    session: EditorSession, component_ids: list, name: str = "G1"
):
    import uuid

    from protoboard.domain.layout.group import ComponentGroup

    gid = uuid.uuid4()
    session.layout.groups[gid] = ComponentGroup(
        id=gid, name=name, component_ids=frozenset(component_ids)
    )
    return gid


class TestGroupDrag:
    def test_press_grouped_component_drags_whole_group(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        a = _place(board_view, session, 0.5, 0.5)
        b = _place(board_view, session, 1.5, 0.5)
        _make_group(session, [a.id, b.id])
        tool = _v2(board_view)
        a0 = session.layout.placements[a.id].position
        b0 = session.layout.placements[b.id].position
        # Press on A (a grouped component) and drag — both members move by the
        # same delta (monolith), not just A.
        _press(tool, 0.5, 0.5)
        _move(tool, 2.5, 0.5)
        _release(tool, 2.5, 0.5)
        a1 = session.layout.placements[a.id].position
        b1 = session.layout.placements[b.id].position
        assert (a1.x - a0.x, a1.y - a0.y) == (b1.x - b0.x, b1.y - b0.y)
        assert (a1.x, a1.y) != (a0.x, a0.y)

    def test_group_drag_is_one_atomic_command(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        a = _place(board_view, session, 0.5, 0.5)
        b = _place(board_view, session, 1.5, 0.5)
        _make_group(session, [a.id, b.id])
        tool = _v2(board_view)
        a0 = session.layout.placements[a.id].position
        b0 = session.layout.placements[b.id].position
        depth = len(session.stack._undo)  # type: ignore[attr-defined]
        _press(tool, 0.5, 0.5)
        _move(tool, 2.5, 0.5)
        _release(tool, 2.5, 0.5)
        # One command moves the whole group; undo restores both members.
        assert len(session.stack._undo) == depth + 1  # type: ignore[attr-defined]
        session.stack.undo(session)  # type: ignore[arg-type]
        assert session.layout.placements[a.id].position == a0
        assert session.layout.placements[b.id].position == b0

    def test_free_component_still_drags_alone(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        _place(board_view, session, 0.5, 0.5)  # A at the press point
        b = _place(board_view, session, 1.5, 0.5)
        # No group — A is free; dragging A must not move B.
        tool = _v2(board_view)
        b0 = session.layout.placements[b.id].position
        _press(tool, 0.5, 0.5)
        _move(tool, 2.5, 0.5)
        _release(tool, 2.5, 0.5)
        assert session.layout.placements[b.id].position == b0


def _make_bendable(session: EditorSession, comp_id, pin: int = 1) -> None:
    """Give *comp_id*'s definition a free bendable lead on *pin* (in-place)."""
    from protoboard.domain.components.definition import PinBendSpec
    from protoboard.geometry import Point

    inst = session.instances[comp_id]
    defn = session.components[inst.definition_ref]
    # bendable_pins is a mutable dict on the (frozen) definition — inject a free
    # continuous spec reaching a few raster units in any direction.
    defn.bendable_pins[pin] = PinBendSpec(
        min_reach=0.0, max_reach=5.0, default_offset=Point(0.0, 0.0)
    )


class TestRedrag:
    def test_redrag_moves_pin_offset_and_commits(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 1.0, 1.0)
        _make_bendable(session, inst.id, pin=1)
        tool = _v2(board_view)
        _press(tool, 1.0, 1.0)  # select it
        depth = len(session.stack._undo)  # type: ignore[attr-defined]
        before = dict(session.layout.placements[inst.id].pin_offsets)
        tool.begin_redrag(inst.id)
        assert tool._selection.active_pick is not None
        # Click a reachable pad → pick completes → command lands.
        _click(tool, 2.0, 1.0)
        after = dict(session.layout.placements[inst.id].pin_offsets)
        assert after != before  # pin 1 offset changed
        assert len(session.stack._undo) == depth + 1  # type: ignore[attr-defined]
        assert tool._selection.active_pick is None

    def test_redrag_undo_restores_offset(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 1.0, 1.0)
        _make_bendable(session, inst.id, pin=1)
        tool = _v2(board_view)
        _press(tool, 1.0, 1.0)
        before = dict(session.layout.placements[inst.id].pin_offsets)
        tool.begin_redrag(inst.id)
        _click(tool, 2.0, 1.0)
        session.stack.undo(session)  # type: ignore[arg-type]
        assert dict(session.layout.placements[inst.id].pin_offsets) == before

    def test_redrag_esc_cancels_no_command(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 1.0, 1.0)
        _make_bendable(session, inst.id, pin=1)
        tool = _v2(board_view)
        _press(tool, 1.0, 1.0)
        depth = len(session.stack._undo)  # type: ignore[attr-defined]
        tool.begin_redrag(inst.id)
        assert tool._selection.active_pick is not None
        _key(tool, Qt.Key.Key_Escape)  # cancel the pick
        assert tool._selection.active_pick is None
        assert len(session.stack._undo) == depth  # type: ignore[attr-defined]
        assert tool._redrag_comp_id is None  # overlay state cleared

    def test_redrag_no_bendable_pin_is_noop(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 1.0, 1.0)  # R has no bendable pins
        tool = _v2(board_view)
        _press(tool, 1.0, 1.0)
        tool.begin_redrag(inst.id)
        # No free lead → no pick armed.
        assert tool._selection.active_pick is None


class TestPickRouting:
    def test_click_feeds_active_pick(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 0.5, 0.5)
        tool = _v2(board_view)
        done: list[list] = []
        # Arm the pick through the tool so it routes via a PointPickController.
        tool._arm_pick(
            "component", Stop.count(1), on_done=lambda xs: done.append(xs)
        )
        # A click during an active pick feeds it, not the normal selection.
        _click(tool, 0.5, 0.5)
        assert len(done) == 1
        assert done[0][0].to_hit().id == inst.id
        # Pick completed → no interaction / pick left active.
        assert tool._selection.active_pick is None
        assert tool._mouse is None

    def test_esc_cancels_active_pick(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        _place(board_view, session, 0.5, 0.5)
        tool = _v2(board_view)
        cancelled: list[bool] = []
        tool._arm_pick(
            "component", Stop.confirm(),
            on_done=lambda xs: None, on_cancel=lambda: cancelled.append(True),
        )
        _key(tool, Qt.Key.Key_Escape)
        assert cancelled == [True]
        assert tool._selection.active_pick is None
        assert tool._mouse is None


# -- wire-edit ----------------------------------------------------------------


def _wire_session(qtbot) -> tuple[EditorSession, BoardView]:
    """A 16×16 perfboard session with a single net-bound straight wire selected.

    wire: (1.5,1.5) → (2.5,1.5) → (3.5,1.5) → (4.5,1.5), bottom side, bound to
    one net via a 1-pin component at the first pad, so `_build_wire_net_map`
    resolves it.
    """
    import uuid
    from pathlib import Path

    from protoboard.domain.board.board import Board
    from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
    from protoboard.domain.board.pads import PadLayers
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.footprints import SIL
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.layout.placement import ComponentPlacement
    from protoboard.domain.layout.wire import Wire
    from protoboard.domain.schematic.net import Net, PinRef
    from protoboard.geometry import Point
    from protoboard.services.library.loader import load_library

    grid = BoardGrid(
        width_policy=SizePolicy.HUG, height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG, height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        ),
        rows=16, cols=16, row_gap=0.0, col_gap=0.0,
    )
    library = load_library([Path(__file__).parent.parent / "fixtures" / "library"])
    sess = EditorSession(board=Board(root=grid), library=library)

    defn = ComponentDefinition(name="STUB1", footprint=SIL(1), pin_names={1: "P1"})
    sess.components["STUB1"] = defn
    inst = ComponentInstance.new(definition_ref="STUB1", refdes="X1", value="")
    sess.instances[inst.id] = inst
    sess.layout.placements[inst.id] = ComponentPlacement(
        component_id=inst.id, position=Point(1.5, 1.5), rotation=0, side="top",
    )
    net_id = uuid.uuid4()
    sess.schematic.nets[net_id] = Net(
        id=net_id, name="N1",
        pins={PinRef(component_id=inst.id, pin_number=1)},
    )
    wire = Wire(
        id=uuid.uuid4(),
        path=[Point(1.5, 1.5), Point(2.5, 1.5), Point(3.5, 1.5), Point(4.5, 1.5)],
        insulated=False, side="bottom",
    )
    sess.layout.wires.append(wire)
    sess.invalidate_graph()

    view = BoardView(sess)
    qtbot.addWidget(view)
    return sess, view


def _select_wire(tool: SelectTool, session: EditorSession) -> None:
    from protoboard.gui.selection.adapters import WireSelectable

    wire = next(iter(session.layout.wires))
    tool._selection.set(WireSelectable(wire=wire))


def _depth(session: EditorSession) -> int:
    return len(session.stack._undo)  # type: ignore[attr-defined]


class TestWireEdit:
    def test_e_arms_then_two_clicks_select_fragment(self, qtbot) -> None:
        session, view = _wire_session(qtbot)
        tool = _v2(view)
        _select_wire(tool, session)
        depth = _depth(session)

        _key(tool, Qt.Key.Key_E)  # arm wire-edit
        assert tool._selection.active_pick is not None  # vertex pick armed
        _click(tool, 2.5, 1.5)  # first vertex (index 1)
        assert tool._wire_edit is None  # not complete yet
        _click(tool, 3.5, 1.5)  # second vertex (index 2)

        assert tool._wire_edit is not None
        assert (tool._wire_edit.start_idx, tool._wire_edit.end_idx) == (1, 2)
        assert tool._mouse is None  # range pick finished
        assert _depth(session) == depth  # no command yet

    def test_f_flips_fragment_to_other_layer(self, qtbot) -> None:
        from protoboard.domain.commands.component_cmds import (
            FlipWireFragmentCommand,
        )

        session, view = _wire_session(qtbot)
        tool = _v2(view)
        _select_wire(tool, session)
        wire = next(iter(session.layout.wires))
        assert wire.side == "bottom"
        # The flip goes through replace_wire_fragments, which needs both endpoint
        # knots — backfill them (the fixture appends a bare wire; runtime wires
        # get knots from AddWireCommand / load migration).
        session.layout.ensure_wire_knots(wire)
        depth = _depth(session)

        _key(tool, Qt.Key.Key_E)
        _click(tool, 2.5, 1.5)  # idx 1
        _click(tool, 3.5, 1.5)  # idx 2
        assert tool._wire_edit is not None
        _key(tool, Qt.Key.Key_F)  # flip the fragment

        assert _depth(session) == depth + 1
        cmd = session.stack._undo[-1]  # type: ignore[attr-defined]
        assert isinstance(cmd, FlipWireFragmentCommand)
        assert cmd.target_side == "top"
        assert tool._wire_edit is None  # op leaves wire-edit
        # The conductor stays one wire, now multi-fragment (a via boundary).
        flipped = next(iter(session.layout.wires))
        assert flipped.id == wire.id
        assert any(f.side == "top" for f in flipped.fragments)

    def test_w_reroute_commits_replace_fragments(self, qtbot) -> None:
        from protoboard.domain.commands.component_cmds import (
            ReplaceWireFragmentsCommand,
        )

        session, view = _wire_session(qtbot)
        tool = _v2(view)
        _select_wire(tool, session)
        depth = _depth(session)

        _key(tool, Qt.Key.Key_E)
        _click(tool, 2.5, 1.5)
        _click(tool, 3.5, 1.5)
        # [W] arms the waypoint pick; click a detour waypoint, Enter commits.
        _key(tool, Qt.Key.Key_W)
        assert tool._selection.active_pick is not None
        _click(tool, 3.0, 2.5)  # waypoint off the straight line
        _key(tool, Qt.Key.Key_Return)  # commit

        assert _depth(session) == depth + 1
        cmd = session.stack._undo[-1]  # type: ignore[attr-defined]
        assert isinstance(cmd, ReplaceWireFragmentsCommand)
        assert tool._wire_edit is None

    def test_w_reroute_undo_restores(self, qtbot) -> None:
        session, view = _wire_session(qtbot)
        tool = _v2(view)
        _select_wire(tool, session)
        before = [
            list(w.path) for w in session.layout.wires
        ]

        _key(tool, Qt.Key.Key_E)
        _click(tool, 2.5, 1.5)
        _click(tool, 3.5, 1.5)
        _key(tool, Qt.Key.Key_W)
        _click(tool, 3.0, 2.5)
        _key(tool, Qt.Key.Key_Return)
        session.stack.undo(session)  # type: ignore[arg-type]

        after = [list(w.path) for w in session.layout.wires]
        assert after == before

    def test_del_deletes_whole_wire(self, qtbot) -> None:
        from protoboard.domain.commands.component_cmds import RemoveWireCommand

        session, view = _wire_session(qtbot)
        tool = _v2(view)
        _select_wire(tool, session)

        _key(tool, Qt.Key.Key_E)
        _click(tool, 1.5, 1.5)  # first vertex (index 0)
        _click(tool, 4.5, 1.5)  # last vertex (index 3) → whole wire
        depth = _depth(session)
        _key(tool, Qt.Key.Key_Delete)

        assert _depth(session) == depth + 1
        assert isinstance(
            session.stack._undo[-1], RemoveWireCommand  # type: ignore[attr-defined]
        )
        assert tool._wire_edit is None

    def test_del_partial_cuts_into_halves(self, qtbot) -> None:
        session, view = _wire_session(qtbot)
        tool = _v2(view)
        _select_wire(tool, session)

        _key(tool, Qt.Key.Key_E)
        _click(tool, 2.5, 1.5)  # index 1
        _click(tool, 3.5, 1.5)  # index 2 — interior fragment
        n_before = len(list(session.layout.wires))
        _key(tool, Qt.Key.Key_Delete)
        # The single wire is cut into two surviving halves.
        assert len(list(session.layout.wires)) == n_before + 1

    def test_esc_cancels_fragment_no_command(self, qtbot) -> None:
        session, view = _wire_session(qtbot)
        tool = _v2(view)
        _select_wire(tool, session)

        _key(tool, Qt.Key.Key_E)
        _click(tool, 2.5, 1.5)
        _click(tool, 3.5, 1.5)
        depth = _depth(session)
        assert tool._wire_edit is not None
        _key(tool, Qt.Key.Key_Escape)

        assert tool._wire_edit is None
        assert _depth(session) == depth

    def test_esc_during_reroute_returns_to_fragment(self, qtbot) -> None:
        session, view = _wire_session(qtbot)
        tool = _v2(view)
        _select_wire(tool, session)

        _key(tool, Qt.Key.Key_E)
        _click(tool, 2.5, 1.5)
        _click(tool, 3.5, 1.5)
        _key(tool, Qt.Key.Key_W)  # arm reroute
        _click(tool, 3.0, 2.5)  # one waypoint
        _key(tool, Qt.Key.Key_Escape)  # cancel the reroute pick

        # Reroute aborted, but the fragment selection survives.
        assert tool._selection.active_pick is None
        assert tool._wire_edit is not None

    def test_backspace_during_reroute_drops_last_waypoint(self, qtbot) -> None:
        session, view = _wire_session(qtbot)
        tool = _v2(view)
        _select_wire(tool, session)

        _key(tool, Qt.Key.Key_E)
        _click(tool, 2.5, 1.5)
        _click(tool, 3.5, 1.5)
        _key(tool, Qt.Key.Key_W)
        _click(tool, 3.0, 2.5)
        pick = tool._selection.active_pick
        assert pick is not None and len(pick.collected) == 1
        _key(tool, Qt.Key.Key_Backspace)  # UNDO drops the waypoint
        assert len(pick.collected) == 0

    def test_press_elsewhere_cancels_fragment(self, qtbot) -> None:
        session, view = _wire_session(qtbot)
        tool = _v2(view)
        _select_wire(tool, session)

        _key(tool, Qt.Key.Key_E)
        _click(tool, 2.5, 1.5)
        _click(tool, 3.5, 1.5)
        assert tool._wire_edit is not None
        # A plain press on empty board space leaves wire-edit (and falls
        # through to normal selection — here an empty area arms a band).
        _press(tool, 10.0, 10.0)
        assert tool._wire_edit is None

    def test_e_without_selected_wire_is_noop(self, qtbot) -> None:
        session, view = _wire_session(qtbot)
        tool = _v2(view)
        # No wire selected.
        _key(tool, Qt.Key.Key_E)
        assert tool._selection.active_pick is None
        assert tool._wire_edit is None

    def test_menu_action_arms_wire_edit(self, qtbot) -> None:
        # The Edit ► Design ► Wire ► Edit Path menu entry delegates to the same
        # public arm_wire_edit() the [E] key uses — one behaviour, two surfaces.
        session, view = _wire_session(qtbot)
        tool = _v2(view)
        _select_wire(tool, session)
        tool.arm_wire_edit()
        assert tool._selection.active_pick is not None  # vertex pick armed

    def test_menu_action_has_no_shortcut(self) -> None:
        # The menu surface must carry no QShortcut: the live [E] keystroke is
        # owned by the namespaced select-tool.action.edit, so a non-empty
        # default here would mint a second, competing binding for one op.
        from protoboard.gui import keybindings as KB

        assert KB.DEFAULTS[KB.ACTION_WIRE_EDIT] == ""


def _key_event(key: Qt.Key) -> QKeyEvent:
    return QKeyEvent(QKeyEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)


class TestDeleteSelectedWire:
    """Delete on a selected (whole) wire removes it — without entering edit."""

    def test_del_on_selected_wire_removes_it(self, qtbot) -> None:
        from protoboard.domain.commands.component_cmds import RemoveWireCommand

        session, view = _wire_session(qtbot)
        tool = _v2(view)
        _select_wire(tool, session)
        depth = _depth(session)
        n_before = len(list(session.layout.wires))

        _key(tool, Qt.Key.Key_Delete)

        assert len(list(session.layout.wires)) == n_before - 1
        assert _depth(session) == depth + 1
        assert isinstance(
            session.stack._undo[-1], RemoveWireCommand  # type: ignore[attr-defined]
        )

    def test_del_with_empty_selection_is_noop(self, qtbot) -> None:
        session, view = _wire_session(qtbot)
        tool = _v2(view)
        depth = _depth(session)
        _key(tool, Qt.Key.Key_Delete)
        assert _depth(session) == depth  # nothing selected → nothing deleted


class TestWantsKeyShortcutVeto:
    """The tool vetoes global shortcuts only for keys it needs contextually."""

    def test_wants_delete_when_wire_selected(self, qtbot) -> None:
        session, view = _wire_session(qtbot)
        tool = _v2(view)
        _select_wire(tool, session)
        assert tool.wants_key(_key_event(Qt.Key.Key_Delete)) is True

    def test_does_not_want_delete_when_nothing_selected(self, qtbot) -> None:
        session, view = _wire_session(qtbot)
        tool = _v2(view)
        assert tool.wants_key(_key_event(Qt.Key.Key_Delete)) is False

    def test_wants_w_and_a_only_in_wire_edit(self, qtbot) -> None:
        session, view = _wire_session(qtbot)
        tool = _v2(view)
        _select_wire(tool, session)
        # No fragment yet → [W] belongs to the global Route-tool shortcut.
        assert tool.wants_key(_key_event(Qt.Key.Key_W)) is False
        assert tool.wants_key(_key_event(Qt.Key.Key_A)) is False
        # Enter wire-edit and pick a fragment.
        _key(tool, Qt.Key.Key_E)
        _click(tool, 2.5, 1.5)
        _click(tool, 3.5, 1.5)
        assert tool._wire_edit is not None
        # Now the fragment claims [W] (reroute) and [A] (autoroute).
        assert tool.wants_key(_key_event(Qt.Key.Key_W)) is True
        assert tool.wants_key(_key_event(Qt.Key.Key_A)) is True


class TestDynamicRebind:
    """Veto + dispatch follow the *current* binding, never a hard-coded key."""

    def test_reroute_follows_rebound_key(self, qtbot) -> None:
        session, view = _wire_session(qtbot)
        tool = _v2(view)
        # User rebinds reroute from "W" to "Q".
        view.keymap.set_override("select-tool.action.reroute", "Q")
        _select_wire(tool, session)
        _key(tool, Qt.Key.Key_E)
        _click(tool, 2.5, 1.5)
        _click(tool, 3.5, 1.5)
        assert tool._wire_edit is not None
        # Old key no longer claimed; the new key is.
        assert tool.wants_key(_key_event(Qt.Key.Key_W)) is False
        assert tool.wants_key(_key_event(Qt.Key.Key_Q)) is True
        # And [Q] now drives reroute (arms the waypoint pick).
        _key(tool, Qt.Key.Key_Q)
        assert tool._selection.active_pick is not None

    def test_delete_follows_rebound_global(self, qtbot) -> None:
        session, view = _wire_session(qtbot)
        tool = _v2(view)
        # Rebind the *global* delete from "Del" to "X"; the tool inherits it.
        view.keymap.set_override("global.action.delete", "X")
        _select_wire(tool, session)
        assert tool.wants_key(_key_event(Qt.Key.Key_Delete)) is False
        assert tool.wants_key(_key_event(Qt.Key.Key_X)) is True
        # [X] now deletes the selected wire.
        n_before = len(list(session.layout.wires))
        _key(tool, Qt.Key.Key_X)
        assert len(list(session.layout.wires)) == n_before - 1


# -- net / connectivity relation ----------------------------------------------


def _two_comp_net(board_view: BoardView, session: EditorSession):
    """Place two 1-pin components bound to one net; return (i1, i2)."""
    import uuid as _uuid

    from protoboard.domain.schematic.net import Net, PinRef

    i1 = _place(board_view, session, 0.5, 0.5)
    i2 = _place(board_view, session, 3.5, 0.5)
    net = Net(id=_uuid.uuid4(), name="N1", pins={PinRef(i1.id, 1), PinRef(i2.id, 1)})
    session.schematic.nets[net.id] = net
    session.invalidate_graph()
    return i1, i2


class TestNetRelation:
    def test_alt_click_selects_whole_net(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        i1, i2 = _two_comp_net(board_view, session)
        tool = _v2(board_view)
        # Alt = RELATION basis → SET expands to the net members.
        _press(tool, 0.5, 0.5, Qt.KeyboardModifier.AltModifier)
        ids = _comp_ids(tool)
        assert i1.id in ids
        assert i2.id in ids

    def test_plain_click_selects_only_hit(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        i1, i2 = _two_comp_net(board_view, session)
        tool = _v2(board_view)
        _press(tool, 0.5, 0.5)  # no Alt → SINGLE basis
        ids = _comp_ids(tool)
        assert ids == {i1.id}  # only the clicked component is selected

    def test_plain_click_auto_highlights_net_context(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        i1, i2 = _two_comp_net(board_view, session)
        tool = _v2(board_view)
        _press(tool, 0.5, 0.5)  # plain SET
        entry = session.highlight_stack.active()
        assert entry is not None
        # Auto-highlight shows the hit's net context — both members dimmed in.
        assert i1.id in entry.comp_ids
        assert i2.id in entry.comp_ids

    def test_relation_none_disables_context_highlight(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        from protoboard.gui.selection_axes import RelationClass

        i1, i2 = _two_comp_net(board_view, session)
        tool = _v2(board_view)
        tool.set_relation(RelationClass.NONE)
        _press(tool, 0.5, 0.5)
        entry = session.highlight_stack.active()
        assert entry is not None
        # NONE correlates nothing — only the clicked component is highlighted.
        assert i2.id not in entry.comp_ids

    def test_set_relation_changes_axis(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        from protoboard.gui.selection_axes import RelationClass

        tool = _v2(board_view)
        assert tool.relation == RelationClass.SAME_LOGICAL_NET
        tool.set_relation(RelationClass.NEIGHBORS)
        assert tool.relation == RelationClass.NEIGHBORS

    def test_a_autoroutes_fragment(self, qtbot) -> None:
        session, view = _wire_session(qtbot)
        tool = _v2(view)
        _select_wire(tool, session)

        _key(tool, Qt.Key.Key_E)
        _click(tool, 2.5, 1.5)  # index 1
        _click(tool, 3.5, 1.5)  # index 2
        assert tool._wire_edit is not None
        # [A] runs A* between the two boundary pads; whether it commits a new
        # path or finds none, it must leave wire-edit cleanly and not crash.
        _key(tool, Qt.Key.Key_A)
        assert tool._wire_edit is None
        assert tool._selection.active_pick is None
