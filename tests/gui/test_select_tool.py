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
        from PySide6.QtWidgets import (
            QAbstractGraphicsShapeItem,
            QGraphicsItemGroup,
        )

        grp = board_view._comp_groups.get(comp_id)
        if not isinstance(grp, QGraphicsItemGroup):
            return []
        out = []
        for child in grp.childItems():
            if isinstance(child, QAbstractGraphicsShapeItem):
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

        Regression: the cyan selection overlays were kept across a scene clear,
        so the next recolour pass touched already-deleted C++ items
        (libshiboken "already deleted") — which aborted the highlight refresh
        and broke dragging.
        """
        inst = _place(board_view, session, 0.5, 0.5)
        board_view.refresh()
        tool = _v2(board_view)
        _click(tool, 0.5, 0.5)  # select → registers a cyan outline + dashed bbox
        assert inst.id in _comp_ids(tool)

        # Simulate the scene wipe + rebuild that a drag refresh performs.
        board_view._invalidate_all_groups()
        assert not board_view._selection_bbox_overlays  # refs dropped, not dangling
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

    @staticmethod
    def _release_mods(tool, x, y, mods):  # type: ignore[no-untyped-def]
        from PySide6.QtGui import QMouseEvent

        tool.on_release(
            QMouseEvent(
                QMouseEvent.Type.MouseButtonRelease,
                QPointF(x, y), QPointF(x, y),
                Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton, mods,
            ),
            QPointF(x, y),
        )

    def test_band_catches_wires(self, qtbot) -> None:
        """A rubber-band over a wire selects it (regression: band ignored wires)."""
        session, view = _wire_session(qtbot)
        view.refresh()
        tool = _v2(view)
        wire = next(iter(session.layout.wires))
        # The fixture wire runs (1.5,1.5)→(4.5,1.5); enclose it L→R.
        _press(tool, 1.0, 1.0)
        _move(tool, 5.0, 2.0)
        _release(tool, 5.0, 2.0)
        assert wire.id in tool._selected_wire_ids

    def test_band_catches_groups(self, qtbot) -> None:
        """A rubber-band over a group's members selects the group."""
        import uuid as _uuid

        from protoboard.domain.layout.group import ComponentGroup
        from protoboard.domain.layout.layout import ROOT_GROUP_ID

        session, view = _wire_session(qtbot)
        inst = next(iter(session.instances.values()))  # X1 at (1.5, 1.5)
        gid = _uuid.uuid4()
        session.layout.groups[gid] = ComponentGroup(
            id=gid, name="G1", component_ids=frozenset([inst.id]),
            child_group_ids=frozenset(), exported_pins=frozenset(),
        )
        root = session.layout.groups[ROOT_GROUP_ID]
        session.layout.groups[ROOT_GROUP_ID] = ComponentGroup(
            id=ROOT_GROUP_ID, name=root.name,
            component_ids=root.component_ids - frozenset([inst.id]),
            child_group_ids=root.child_group_ids | {gid},
            exported_pins=root.exported_pins,
        )
        session.invalidate_graph()
        view.refresh()
        tool = _v2(view)
        _press(tool, 0.5, 0.5)
        _move(tool, 3.0, 3.0)
        _release(tool, 3.0, 3.0)
        assert gid in tool._selected_group_ids

    def test_ctrl_band_adds_to_selection(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """Ctrl+band ADDs to the existing selection instead of replacing it."""
        a = _place(board_view, session, 1.0, 1.0)
        b = _place(board_view, session, 6.0, 6.0)
        tool = _v2(board_view)
        tool._select_primary(a.id)
        assert _comp_ids(tool) == {a.id}
        # Ctrl+band starting on EMPTY space, sweeping over b only → b joins a.
        bb = session.component_bbox(b.id)
        assert bb is not None
        x0, y0 = bb.x - 0.5, bb.y - 0.5   # empty corner near b (a is far away)
        x1, y1 = bb.x + bb.width + 0.5, bb.y + bb.height + 0.5
        _press(tool, x0, y0, Qt.KeyboardModifier.ControlModifier)
        _move(tool, x1, y1)
        self._release_mods(tool, x1, y1, Qt.KeyboardModifier.ControlModifier)
        ids = _comp_ids(tool)
        assert a.id in ids and b.id in ids  # union, not replace


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
        from typing import cast

        from PySide6.QtCore import QPoint
        from PySide6.QtGui import QWheelEvent

        inst, tool = self._armed(board_view, session)
        base = session.layout.placements[inst.id].rotation
        # While armed the wheel-probe ignores the event; a dummy stands in.
        assert tool.wants_wheel(cast(QWheelEvent, object()))
        ev = QWheelEvent(
            QPointF(0, 0), QPointF(0, 0), QPoint(0, 0), QPoint(0, 120),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase, False,
        )
        tool.on_wheel(ev)
        assert session.layout.placements[inst.id].rotation == (base + 90) % 360

    @staticmethod
    def _wheel(dy: int):
        from PySide6.QtCore import QPoint
        from PySide6.QtGui import QWheelEvent
        return QWheelEvent(
            QPointF(0, 0), QPointF(0, 0), QPoint(0, 0), QPoint(0, dy),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase, False,
        )

    def test_wheel_bounce_one_notch_is_one_step(self, board_view, session) -> None:
        # Bug: a mouse emitting several sub-detent events per physical notch
        # over-rotated (one flick → 180°/270°).  Three 40-unit events sum to one
        # 120 notch → exactly one 90° step.
        inst, tool = self._armed(board_view, session)
        base = session.layout.placements[inst.id].rotation
        for _ in range(3):
            tool.on_wheel(self._wheel(40))  # 3 × 40 = 120 = one notch
        assert session.layout.placements[inst.id].rotation == (base + 90) % 360

    def test_wheel_hard_spin_steps_proportionally(
        self, board_view, session
    ) -> None:
        # A deliberate hard spin (two notches in one event) still steps twice.
        inst, tool = self._armed(board_view, session)
        base = session.layout.placements[inst.id].rotation
        tool.on_wheel(self._wheel(240))  # two notches
        assert session.layout.placements[inst.id].rotation == (base + 180) % 360

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
        from protoboard.gui.selection.float_transform import FloatTransform

        inst, tool = self._armed(board_view, session)
        before = session.layout.placements[inst.id].position
        _press(tool, 3.0, 3.0)  # click a pad away from the part → shared pivot
        assert isinstance(tool._armed, FloatTransform) and tool._armed.pivot is not None
        _key(tool, Qt.Key.Key_R)  # step → orbit the pivot, position changes
        after = session.layout.placements[inst.id].position
        assert (after.x, after.y) != (before.x, before.y)

    def test_pivot_markers_shown_while_armed_cleared_on_disarm(
        self, board_view, session
    ) -> None:
        from protoboard.gui.selection.float_transform import FloatTransform

        # Visual feedback: arming draws pivot marker(s); disarm clears them.
        inst, tool = self._armed(board_view, session)
        assert tool._pivot_marker_items  # per-object marker drawn on arm
        _press(tool, 3.0, 3.0)  # shared pad pivot
        assert isinstance(tool._armed, FloatTransform) and tool._armed.pivot is not None
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
        assert tool._armed is not None
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


class TestFloatTransformSwitch:
    """The merge's reason for being: switch rotate<->move while floating,
    without committing, and commit the whole thing as one undo entry."""

    def _armed_rotate(self, board_view, session):
        inst = _place(board_view, session, 2.5, 2.5)
        tool = _v2(board_view)
        _press(tool, 2.5, 2.5)
        _key(tool, Qt.Key.Key_R)  # arm in rotate sub-mode
        assert tool._armed is not None
        return inst, tool

    def test_rotate_then_switch_to_move_preserves_float(
        self, board_view, session
    ) -> None:
        from protoboard.gui.selection.float_transform import FloatTransform

        inst, tool = self._armed_rotate(board_view, session)
        base_rot = session.layout.placements[inst.id].rotation
        _key(tool, Qt.Key.Key_R)  # rotate +90 (per-object)
        assert session.layout.placements[inst.id].rotation == (base_rot + 90) % 360
        # [G] switches sub-mode WITHOUT committing or re-arming.
        _key(tool, Qt.Key.Key_G)
        assert isinstance(tool._armed, FloatTransform)
        assert tool._armed.mode == "move"
        assert tool._armed.finished is False
        # Rotation survived the switch (float pose carried over).
        assert session.layout.placements[inst.id].rotation == (base_rot + 90) % 360

    def test_mixed_transform_commits_as_one_undo_entry(
        self, board_view, session
    ) -> None:
        inst, tool = self._armed_rotate(board_view, session)
        base = session.layout.placements[inst.id]
        base_pos, base_rot = base.position, base.rotation
        depth = len(session.stack._undo)  # type: ignore[attr-defined]

        _key(tool, Qt.Key.Key_R)        # rotate +90
        _key(tool, Qt.Key.Key_G)        # -> move sub-mode (no commit)
        _key(tool, Qt.Key.Key_Right)    # nudge +1x
        _key(tool, Qt.Key.Key_Return)   # accept the whole transform

        assert tool._armed is None
        # Exactly ONE undo entry for the combined rotate+move.
        assert len(session.stack._undo) == depth + 1  # type: ignore[attr-defined]
        after = session.layout.placements[inst.id]
        assert after.rotation == (base_rot + 90) % 360
        assert after.position.x == base_pos.x + 1.0

        # One undo restores the *original* pose (before any operation).
        session.stack.undo(session)  # type: ignore[arg-type]
        restored = session.layout.placements[inst.id]
        assert restored.rotation == base_rot
        assert (restored.position.x, restored.position.y) == (
            base_pos.x, base_pos.y,
        )

    def test_cancel_from_mixed_state_returns_to_original(
        self, board_view, session
    ) -> None:
        inst, tool = self._armed_rotate(board_view, session)
        base = session.layout.placements[inst.id]
        base_pos, base_rot = base.position, base.rotation

        _key(tool, Qt.Key.Key_R)        # rotate +90
        _key(tool, Qt.Key.Key_G)        # -> move
        _key(tool, Qt.Key.Key_Right)    # nudge
        _key(tool, Qt.Key.Key_Escape)   # cancel — back to the pristine pose

        assert tool._armed is None
        now = session.layout.placements[inst.id]
        assert now.rotation == base_rot
        assert (now.position.x, now.position.y) == (base_pos.x, base_pos.y)


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
    """Selecting on press, but deferring collapse to a click release.

    A press on an already-selected entity must NOT mutate the selection (so a
    drag moves the whole multi-selection); if the gesture is a click on a
    multi-selection, the release collapses onto the clicked entity.  A click on
    the *sole* selected entity is a no-op — clicking never toggles selection off
    (explicit deselect is Ctrl+Shift+A / clicking the empty board).
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

    def test_click_on_sole_selection_keeps_it(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 0.5, 0.5)
        tool = _v2(board_view)
        _click(tool, 0.5, 0.5)
        assert inst.id in _comp_ids(tool)
        # Clicking the only selected component again is a NO-OP — it stays
        # selected (clicking never toggles off; that read as a mis-select).
        _click(tool, 0.5, 0.5)
        assert _comp_ids(tool) == {inst.id}

    def test_click_empty_board_clears_selection(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 0.5, 0.5)
        tool = _v2(board_view)
        _click(tool, 0.5, 0.5)
        assert inst.id in _comp_ids(tool)
        # Clicking empty board clears the selection (the deselect gesture).
        _click(tool, 9.5, 9.5)
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
    from protoboard.geometry import Vector

    inst = session.instances[comp_id]
    defn = session.components[inst.definition_ref]
    # bendable_pins is a mutable dict on the (frozen) definition — inject a free
    # continuous spec reaching a few raster units in any direction.
    defn.bendable_pins[pin] = PinBendSpec(
        min_reach=0.0, max_reach=5.0, default_offset=Vector(0.0, 0.0)
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

    def _place_smd(self, session: EditorSession, x: float, y: float):
        """Inject a placed SMD chip (rigid body, discrete pin-2 mount)."""
        import uuid
        from protoboard.domain.components.definition import (
            ComponentClass, ComponentDefinition,
        )
        from protoboard.domain.components.instance import ComponentInstance
        from protoboard.domain.components.package.adapter import PackageFootprint
        from protoboard.domain.components.package.loader import build_package
        from protoboard.domain.layout.placement import ComponentPlacement
        from protoboard.geometry import Point
        pkg = build_package(
            {
                "shape": "prism", "dims": {"length": 2.0, "width": 1.25},
                "arrangement": "axial", "mount": "surface", "bend": 0,
                "leads": {
                    "direction": [[0, 0, -1], [0, 0, -1]],
                    "body_offset": {"unit": 2.5, "value": [[1, 0]]},
                    "board_offset": "default",
                },
            },
            "SMD_TEST",
        )
        defn = ComponentDefinition(
            name="R_SMD_T", component_class=ComponentClass.RESISTOR,
            footprint=PackageFootprint(pkg), pin_names={}, package=pkg,
        )
        cid = uuid.uuid4()
        session.components["R_SMD_T"] = defn
        session.instances[cid] = ComponentInstance(
            id=cid, definition_ref="R_SMD_T", refdes="R9", value="",
        ).bind(defn, session.package_families)
        session.layout.placements[cid] = ComponentPlacement(
            component_id=cid, position=Point(x, y), rotation=0, side="top",
        )
        return cid

    def test_redrag_arms_for_smd_grabs_clicked_terminal(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        # SMD pin 1 (1.5,1.5) is the rigid body anchor; pin 2 (2.5,1.5) is the
        # free terminal.  Clicking the free terminal grabs it; clicking the rigid
        # anchor grabs the anchor (a body-translate).  Either way a pick arms.
        cid = self._place_smd(session, 1.5, 1.5)
        tool = _v2(board_view)
        _press(tool, 2.5, 1.5)  # click the free terminal (pin 2)
        tool.begin_redrag(cid)
        assert tool._selection.active_pick is not None
        assert tool._redrag_pin == 2

    def test_redrag_smd_free_terminal_to_diagonal_pad(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        from protoboard.domain.layout.placement import pin_world_position
        cid = self._place_smd(session, 1.5, 1.5)
        tool = _v2(board_view)
        _press(tool, 2.5, 1.5)  # grab the free terminal (pin 2)
        tool.begin_redrag(cid)
        assert tool._redrag_pin == 2
        # Click the SE-of-pin1 diagonal pad (2.5, 2.5) → pin 2 bends onto it.
        _click(tool, 2.5, 2.5)
        pl = session.layout.placements[cid]
        defn = session.components["R_SMD_T"]
        p1 = pin_world_position(defn.footprint, pl, 1)
        p2 = pin_world_position(defn.footprint, pl, 2)
        assert (round(p2.x - p1.x), round(p2.y - p1.y)) == (1, 1)  # SE diagonal
        assert tool._selection.active_pick is None

    def test_redrag_targets_clicked_pin(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        # Both leads bendable; selecting via pin 2 grabs pin 2 (not auto pin 1).
        # _place snaps R to (0.5, 0.5): pin1 (0.5, 0.5), pin2 (0.5, 1.5).
        inst = _place(board_view, session, 1.0, 1.0)
        _make_bendable(session, inst.id, pin=1)
        _make_bendable(session, inst.id, pin=2)
        tool = _v2(board_view)
        _press(tool, 0.5, 1.5)  # click pin 2
        tool.begin_redrag(inst.id)
        assert tool._redrag_pin == 2
        # The drag moves pin 2's offset, leaving pin 1 untouched.
        before1 = session.layout.placements[inst.id].pin_offsets.get(1)
        _click(tool, 2.5, 1.5)  # a pad in pin 2's reach
        offs = session.layout.placements[inst.id].pin_offsets
        assert 2 in offs and offs.get(1) == before1

    def test_redrag_reclick_switches_grab(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        # Grab defaults to the clicked pin 1; re-clicking pin 2 switches it,
        # without completing the pad pick.  pin1 (0.5, 0.5), pin2 (0.5, 1.5).
        inst = _place(board_view, session, 1.0, 1.0)
        _make_bendable(session, inst.id, pin=1)
        _make_bendable(session, inst.id, pin=2)
        tool = _v2(board_view)
        _press(tool, 0.5, 0.5)  # click pin 1
        depth = len(session.stack._undo)  # type: ignore[attr-defined]
        tool.begin_redrag(inst.id)
        assert tool._redrag_pin == 1
        _click(tool, 0.5, 1.5)  # re-click pin 2 → switch grab, no commit
        assert tool._redrag_pin == 2
        assert tool._selection.active_pick is not None  # still armed
        assert len(session.stack._undo) == depth  # type: ignore[attr-defined]
        _click(tool, 2.5, 1.5)  # now pick a pad → commits pin 2
        assert 2 in session.layout.placements[inst.id].pin_offsets
        assert tool._selection.active_pick is None

    def test_redrag_no_clicked_pin_enters_pick_substep(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        # Body-selected (no pin) + more than one movable lead → pick-a-pin first.
        # pin1 (0.5, 0.5), pin2 (0.5, 1.5); midpoint (0.5, 1.0) is > PAD_HIT_RADIUS
        # (0.35) from either pin but inside the component bbox.
        inst = _place(board_view, session, 1.0, 1.0)
        _make_bendable(session, inst.id, pin=1)
        _make_bendable(session, inst.id, pin=2)
        tool = _v2(board_view)
        _press(tool, 0.5, 1.0)  # body click between the pins
        assert tool._selected_clicked_pin(inst.id) is None
        tool.begin_redrag(inst.id)
        # A component pick is armed to choose the pin; _redrag_pin not yet set.
        assert tool._selection.active_pick is not None
        assert tool._redrag_pin is None
        # Clicking pin 2 resolves the sub-step and arms the pad pick on pin 2.
        _click(tool, 0.5, 1.5)
        assert tool._redrag_pin == 2

    def test_redrag_rigid_grab_translates_body(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        # Grab the RIGID pin 1 of a part whose pin 2 is bendable: the body slides
        # so pin 1 reaches the target, and pin 2 absorbs the shift (stays put).
        from protoboard.domain.layout.placement import pin_world_position
        from protoboard.geometry import Point

        inst = _place(board_view, session, 1.0, 1.0)  # pin1 (0.5,0.5) rigid
        _make_bendable(session, inst.id, pin=2)       # only pin 2 bends
        tool = _v2(board_view)
        _press(tool, 0.5, 0.5)  # click the rigid pin 1
        assert tool._selected_clicked_pin(inst.id) == 1
        tool.begin_redrag(inst.id)
        assert tool._redrag_pin == 1
        defn = session.components[session.instances[inst.id].definition_ref]
        pin2_before = pin_world_position(
            defn.footprint, session.layout.placements[inst.id], 2
        )
        _click(tool, 1.5, 0.5)  # move pin 1 one hole east
        pl = session.layout.placements[inst.id]
        assert pl.position == Point(1.5, 0.5)  # body translated
        assert pin_world_position(defn.footprint, pl, 1) == Point(1.5, 0.5)
        # Pin 2 absorbed the shift — its world position is unchanged.
        assert pin_world_position(defn.footprint, pl, 2) == pin2_before

    def test_redrag_overlay_lights_only_feasible_pads(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        # The overlay's lit pads must equal the set redrag_feasibility accepts —
        # overlay legality == commit legality (no silent divergence).
        inst = _place(board_view, session, 1.0, 1.0)
        _make_bendable(session, inst.id, pin=2)  # pin 2 bendable, pin 1 rigid
        tool = _v2(board_view)
        _press(tool, 0.5, 1.5)  # click pin 2
        tool.begin_redrag(inst.id)
        assert tool._redrag_pin == 2
        # Independently compute the feasible pads via the shared predicate.
        feasible = [
            p for p in board_view.board_pad_positions()
            if (r := tool._redrag_feasibility(inst.id, 2, p)) is not None and r.ok
        ]
        # The overlay drew one green dot per feasible pad (plus the disc ring).
        assert len(feasible) > 0
        assert len(tool._redrag_items) == len(feasible) + 1  # dots + disc outline


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
    inst = inst.bind(defn, {})
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
    def test_arm_enriches_pad_vertices(self, qtbot) -> None:
        """Arming wire-edit materialises board pads the wire passes over.

        Regression: the v2 hit-test dropped the legacy pad-vertex enrichment, so
        intermediate pads on a shortcut/simplified wire were unselectable.
        """
        import uuid as _uuid

        from protoboard.domain.layout.wire import Wire
        from protoboard.geometry import Point
        from protoboard.gui.selection.adapters import WireSelectable

        session, view = _wire_session(qtbot)
        # Replace the fixture wire with an endpoints-only one spanning 4 pads.
        session.layout.wires.clear()
        w = Wire(id=_uuid.uuid4(), path=[Point(1.5, 1.5), Point(4.5, 1.5)],
                 insulated=False, side="top")
        session.layout.wires.append(w)
        session.invalidate_graph()
        view.refresh()
        tool = _v2(view)
        tool._selection.set(WireSelectable(wire=w))
        assert len(next(iter(session.layout.wires)).path) == 2  # endpoints only

        tool.arm_wire_edit()
        # The two intermediate pads (2.5,1.5)/(3.5,1.5) are now vertices.
        path = next(iter(session.layout.wires)).path
        xs = {round(p.x, 1) for p in path}
        assert {1.5, 2.5, 3.5, 4.5} <= xs

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

    def test_reroute_live_anchor_follows_last_waypoint(self, qtbot) -> None:
        """The live rubber-band starts from the last picked pad, not the start.

        Regression: ``_reroute_anchor`` read ``state.waypoints`` (only filled at
        commit) so every live segment was drawn from the fragment start.  It now
        reads the live ``PickSession``, like RouteTool's single ``_points``.
        """
        from protoboard.gui.selection.adapters import PointSelectable

        session, view = _wire_session(qtbot)
        tool = _v2(view)
        _select_wire(tool, session)

        _key(tool, Qt.Key.Key_E)
        _click(tool, 2.5, 1.5)
        _click(tool, 3.5, 1.5)
        state = tool._wire_edit
        assert state is not None
        wire = tool._wire_by_id(state.start_wire_id)
        assert wire is not None
        frag_start = wire.path[state.start_idx]

        _key(tool, Qt.Key.Key_W)  # arm reroute
        # Before any waypoint: anchor is the fragment start.
        assert tool._reroute_anchor() == frag_start

        _click(tool, 3.0, 2.5)  # one waypoint (snaps to nearest pad)
        # After picking: anchor follows the last picked pad, not the start.
        pick = tool._selection.active_pick
        assert pick is not None and len(pick.collected) == 1
        last = pick.collected[-1]
        assert isinstance(last, PointSelectable)
        picked_pt = last.point
        anchor = tool._reroute_anchor()
        assert anchor == picked_pt
        assert anchor != frag_start

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


class TestWireAndPadHighlight:
    """Selecting a wire draws a bright overlay; its net's pads get neon discs.

    Dimming alone can't tell the user *which* wire/pads they picked (everything
    else just fades).  The bright overlay (wire) + neon discs (net pads) are the
    positive "this is what you selected" signal — ported from the legacy tool.
    """

    @staticmethod
    def _select_wire_and_highlight(tool, view, session) -> None:  # type: ignore[no-untyped-def]
        """Select the wire and run the highlight pass (what a real click does)."""
        _select_wire(tool, session)
        tool._refresh_highlight()  # publish to the highlight stack
        view._apply_highlight_opacity_impl()  # draw overlays now (no debounce wait)

    def test_selected_wire_gets_bright_overlay(self, qtbot) -> None:
        session, view = _wire_session(qtbot)
        view.refresh()
        tool = _v2(view)
        assert not view._wire_select_overlays  # nothing selected yet

        self._select_wire_and_highlight(tool, view, session)
        wire = next(iter(session.layout.wires))
        # A bright overlay group now exists for the selected wire.
        assert wire.id in view._wire_select_overlays

    def test_deselect_removes_wire_overlay(self, qtbot) -> None:
        session, view = _wire_session(qtbot)
        view.refresh()
        tool = _v2(view)
        self._select_wire_and_highlight(tool, view, session)
        wire = next(iter(session.layout.wires))
        assert wire.id in view._wire_select_overlays

        tool._selection.clear()
        tool._refresh_highlight()
        view._apply_highlight_opacity_impl()
        assert wire.id not in view._wire_select_overlays

    def test_selected_wire_net_pads_get_discs(self, qtbot) -> None:
        session, view = _wire_session(qtbot)
        view.refresh()  # populate the pin-pad record cache
        tool = _v2(view)
        assert not view._net_pad_highlight_items

        self._select_wire_and_highlight(tool, view, session)
        # The wire's net has a 1-pin component (X1) → at least one pad disc.
        assert view._net_pad_highlight_items

    def test_discs_follow_selected_component_net_not_arbitrary(self, qtbot) -> None:
        """Selecting a component lights ITS net's pads — not one constant net.

        Regression: _anchor_net_id published next(iter(frozenset)) — a single
        arbitrary net by hash order — so the same pad lit regardless of what was
        selected, and other pads never reacted.  Discs now follow the actual
        selection's nets.
        """
        import uuid as _uuid
        from pathlib import Path

        from protoboard.domain.board.board import Board
        from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
        from protoboard.domain.board.pads import PadLayers
        from protoboard.domain.components.definition import ComponentDefinition
        from protoboard.domain.components.footprints import SIL
        from protoboard.domain.components.instance import ComponentInstance
        from protoboard.domain.layout.placement import ComponentPlacement
        from protoboard.domain.schematic.net import Net, PinRef
        from protoboard.geometry import Point
        from protoboard.services.library.loader import load_library

        grid = BoardGrid(
            width_policy=SizePolicy.HUG, height_policy=SizePolicy.HUG,
            cell=PadAtom(
                width_policy=SizePolicy.HUG, height_policy=SizePolicy.HUG,
                layers=PadLayers(top=True, bottom=True, plated_through=True),
            ),
            rows=10, cols=10, row_gap=0.0, col_gap=0.0,
        )
        lib = load_library([Path(__file__).parent.parent / "fixtures" / "library"])
        session = EditorSession(board=Board(root=grid), library=lib)
        defn = ComponentDefinition(name="D1", footprint=SIL(1), pin_names={1: "A"})
        session.components["D1"] = defn
        c1 = ComponentInstance.new(definition_ref="D1", refdes="U1", value="").bind(defn, {})
        c2 = ComponentInstance.new(definition_ref="D1", refdes="U2", value="").bind(defn, {})
        session.instances[c1.id] = c1
        session.instances[c2.id] = c2
        session.layout.placements[c1.id] = ComponentPlacement(
            component_id=c1.id, position=Point(1.5, 1.5), rotation=0, side="top")
        session.layout.placements[c2.id] = ComponentPlacement(
            component_id=c2.id, position=Point(5.5, 1.5), rotation=0, side="top")
        # U1 → netA only, U2 → netB only.
        session.schematic.nets[_uuid.uuid4()] = Net(
            id=_uuid.uuid4(), name="NA", pins={PinRef(c1.id, 1)})
        nb = Net(id=_uuid.uuid4(), name="NB", pins={PinRef(c2.id, 1)})
        session.schematic.nets[nb.id] = nb
        session.invalidate_graph()
        view = BoardView(session)
        qtbot.addWidget(view)
        view.refresh()
        tool = _v2(view)

        def _disc_centers() -> set:
            from typing import cast

            from PySide6.QtWidgets import QGraphicsEllipseItem

            discs = [
                cast(QGraphicsEllipseItem, i)
                for i in view._net_pad_highlight_items
            ]
            return {
                (round(i.rect().center().x(), 1), round(i.rect().center().y(), 1))
                for i in discs
            }

        net_a = next(n.id for n in session.schematic.nets.values() if n.name == "NA")
        net_b = nb.id
        # Discs follow the selection's *net context* (a pad click → that net),
        # not all nets of selected components.  Drive the context directly.
        tool._select_primary(c1.id)
        tool._context_net_id = net_a
        view._apply_highlight_opacity_impl()
        u1_discs = _disc_centers()
        tool._select_primary(c2.id)
        tool._context_net_id = net_b
        view._apply_highlight_opacity_impl()
        u2_discs = _disc_centers()

        # Each context lights its own net's pad — and the two sets differ.
        assert (1.5, 1.5) in u1_discs
        assert (5.5, 1.5) in u2_discs
        assert u1_discs != u2_discs  # context net drives it, not a constant pad

    def test_body_select_no_context_no_discs(self, qtbot) -> None:
        """A body click (no net context) lights no pads, even when netted."""
        from protoboard.gui.selection.adapters import ComponentSelectable

        session, view = _wire_session(qtbot)
        view.refresh()
        tool = _v2(view)
        inst = next(iter(session.instances.values()))  # netted X1
        # Select by body (pin_number=None) → context stays None → no discs.
        pl = session.layout.placements[inst.id]
        tool._selection.set(ComponentSelectable(component_id=inst.id, placement=pl))
        tool._context_net_id = None
        tool._refresh_highlight()
        view._apply_highlight_opacity_impl()
        assert view._net_pad_highlight_items == []

    def test_scene_rebuild_while_wire_selected_is_clean(self, qtbot) -> None:
        """Scene wipe must drop overlay refs, not leave dead C++ wrappers."""
        session, view = _wire_session(qtbot)
        view.refresh()
        tool = _v2(view)
        self._select_wire_and_highlight(tool, view, session)
        assert view._wire_select_overlays

        view._invalidate_all_groups()
        assert not view._wire_select_overlays  # refs dropped, not dangling
        assert not view._net_pad_highlight_items
        view.refresh()
        # The post-rebuild highlight pass (which would crash on dead refs) is clean.
        view._apply_highlight_opacity_impl()


class TestDragConflictOutline:
    """A live drag conflict recolours the selection outline (red HARD / amber
    SOFT), restoring cyan when clear — replaces the dropped dashed overlay.
    """

    def test_severity_drives_outline_override(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        from protoboard.gui.tools.drag_commit_decision import (
            DragCommitDecision,
            DragSeverity,
        )

        _place(board_view, session, 0.5, 0.5)
        board_view.refresh()
        tool = _v2(board_view)

        # HARD → red override.
        tool._show_conflict_corner(
            DragCommitDecision(severity=DragSeverity.HARD, description="x")
        )
        hard = board_view._select_outline_override
        assert hard is not None and hard.red() > hard.green()  # reddish

        # SOFT → amber override (more green than red's, still warm).
        tool._show_conflict_corner(
            DragCommitDecision(severity=DragSeverity.SOFT, description="y")
        )
        soft = board_view._select_outline_override
        assert soft is not None and soft.green() > hard.green()  # amber vs red

        # CLEAR → back to cyan (override removed).
        tool._show_conflict_corner(
            DragCommitDecision(severity=DragSeverity.CLEAR)
        )
        assert board_view._select_outline_override is None

    def test_wire_ghosting_dims_drop_wires(self, qtbot) -> None:
        """A drop decision ghost-dims its drop/disconnect wires; CLEAR restores."""
        from PySide6.QtWidgets import QGraphicsItemGroup

        from protoboard.gui.tools.drag_commit_decision import (
            DragCommitDecision,
            DragSeverity,
        )

        session, view = _wire_session(qtbot)
        view.refresh()
        tool = _v2(view)
        wire = next(iter(session.layout.wires))

        grp = view._wire_groups.get(wire.id)
        assert isinstance(grp, QGraphicsItemGroup) and grp.opacity() == 1.0

        # SOFT decision marking the wire as a free drop → dimmed.
        tool._update_wire_ghosting(
            DragCommitDecision(
                severity=DragSeverity.SOFT,
                free_drop_wire_ids=(wire.id,),
            )
        )
        assert grp.opacity() < 1.0
        assert wire.id in tool._ghosted_wire_ids

        # CLEAR decision → wire restored to full opacity.
        tool._update_wire_ghosting(
            DragCommitDecision(severity=DragSeverity.CLEAR)
        )
        assert grp.opacity() == 1.0
        assert wire.id not in tool._ghosted_wire_ids

    def test_clear_drag_corner_restores_ghosted_wires(self, qtbot) -> None:
        from PySide6.QtWidgets import QGraphicsItemGroup

        from protoboard.gui.tools.drag_commit_decision import (
            DragCommitDecision,
            DragSeverity,
        )

        session, view = _wire_session(qtbot)
        view.refresh()
        tool = _v2(view)
        wire = next(iter(session.layout.wires))
        tool._update_wire_ghosting(
            DragCommitDecision(
                severity=DragSeverity.SOFT, free_drop_wire_ids=(wire.id,)
            )
        )
        grp = view._wire_groups.get(wire.id)
        assert isinstance(grp, QGraphicsItemGroup) and grp.opacity() < 1.0
        # Any drag-end path routes through _clear_drag_corner → wires restored.
        tool._clear_drag_corner()
        assert grp.opacity() == 1.0
        assert not tool._ghosted_wire_ids

    def test_selection_bbox_follows_committed_rotate(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """The dashed selection bbox re-syncs to the component after a rotate.

        Regression: the standalone overlay rect lingered at the pre-rotate
        position/size because it doesn't ride the comp group's refresh.
        """
        inst = _place(board_view, session, 2.5, 2.5)
        board_view.refresh()
        tool = _v2(board_view)
        tool._select_primary(inst.id)
        board_view.refresh()
        rect = board_view._selection_bbox_overlays.get(inst.id)
        assert rect is not None  # dashed bbox drawn for the selection
        before = (round(rect.rect().width(), 2), round(rect.rect().height(), 2))

        tool._rotate_selected(delta=90)
        board_view.refresh()
        after = (round(rect.rect().width(), 2), round(rect.rect().height(), 2))
        # A 2-pin part's bbox swaps width/height on a 90° turn → the overlay must
        # follow (before this fix it stayed at the old extent).
        assert after == (before[1], before[0])
        assert after != before

    def test_selection_bbox_overlay_on_and_off(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        # A selected component gets a thin dashed bbox overlay when the toggle
        # is on; selecting with it off draws none; deselecting removes it.
        from PySide6.QtCore import Qt as _Qt
        board_view.set_show_selection_bbox(True)
        inst = _place(board_view, session, 2.5, 2.5)
        board_view.refresh()
        tool = _v2(board_view)
        tool._select_primary(inst.id)
        board_view.refresh()
        rect = board_view._selection_bbox_overlays.get(inst.id)
        assert rect is not None
        assert rect.pen().style() == _Qt.PenStyle.DashLine  # dashed, not solid

        # Deselect → overlay removed.
        tool._deselect()
        board_view.refresh()
        assert inst.id not in board_view._selection_bbox_overlays

        # Toggle off → no overlay even when selected.
        board_view.set_show_selection_bbox(False)
        tool._select_primary(inst.id)
        board_view.refresh()
        assert inst.id not in board_view._selection_bbox_overlays

    def test_clear_drag_corner_resets_outline(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        from protoboard.gui.tools.drag_commit_decision import (
            DragCommitDecision,
            DragSeverity,
        )

        _place(board_view, session, 0.5, 0.5)
        board_view.refresh()
        tool = _v2(board_view)
        tool._show_conflict_corner(
            DragCommitDecision(severity=DragSeverity.HARD, description="x")
        )
        assert board_view._select_outline_override is not None
        # Any drag-end path routes through _clear_drag_corner → outline resets.
        tool._clear_drag_corner()
        assert board_view._select_outline_override is None


class TestFreePinHint:
    """Clicking an unassigned pin shows a status-bar hint (legacy parity)."""

    def test_free_pin_click_emits_hint(self, qtbot) -> None:
        from PySide6.QtCore import QPointF

        from protoboard.gui.selection.adapters import ComponentSelectable

        session, view = _wire_session(qtbot)
        view.refresh()
        tool = _v2(view)
        # X1 (1-pin) is at (1.5,1.5) and IS netted in the fixture → no hint.
        inst = next(iter(session.instances.values()))
        msgs: list[str] = []
        tool._status = lambda m, t=3000: msgs.append(m)  # type: ignore[assignment]

        # A pin-pad hit carries pin_number (the new hit-test contract).
        sel = ComponentSelectable(
            component_id=inst.id,
            placement=session.layout.placements[inst.id],
            pin_number=1,
        )
        tool._hint_free_pin_under(sel, QPointF(1.5, 1.5))
        assert msgs == []  # netted pin → silent

        # Disconnect the pin → now free → clicking it hints.
        from typing import cast

        from protoboard.domain.commands.base import Command, ComponentState
        from protoboard.domain.commands.component_cmds import DisconnectPinCommand
        session.stack.execute(
            cast(
                "Command[ComponentState]",
                DisconnectPinCommand(component_id=inst.id, pin_number=1),
            ),
            cast(ComponentState, session),
        )
        tool._hint_free_pin_under(sel, QPointF(1.5, 1.5))
        assert len(msgs) == 1
        assert "no net" in msgs[0]
        assert "Assign to net" in msgs[0]

    def test_body_click_no_pin_no_hint(self, qtbot) -> None:
        from PySide6.QtCore import QPointF

        from protoboard.gui.selection.adapters import ComponentSelectable

        session, view = _wire_session(qtbot)
        view.refresh()
        tool = _v2(view)
        inst = next(iter(session.instances.values()))
        # Disconnect so the pin is free — but a *body* hit (pin_number=None)
        # must still stay silent: no pin context means no free-pin feedback.
        from typing import cast

        from protoboard.domain.commands.base import Command, ComponentState
        from protoboard.domain.commands.component_cmds import DisconnectPinCommand
        session.stack.execute(
            cast(
                "Command[ComponentState]",
                DisconnectPinCommand(component_id=inst.id, pin_number=1),
            ),
            cast(ComponentState, session),
        )
        msgs: list[str] = []
        tool._status = lambda m, t=3000: msgs.append(m)  # type: ignore[assignment]
        sel = ComponentSelectable(  # body hit: no pin_number
            component_id=inst.id, placement=session.layout.placements[inst.id]
        )
        tool._hint_free_pin_under(sel, QPointF(1.5, 1.5))
        assert msgs == []  # body click → no free-pin hint

    def test_no_hint_for_wire_or_empty(self, qtbot) -> None:
        from PySide6.QtCore import QPointF

        from protoboard.gui.selection.adapters import WireSelectable

        session, view = _wire_session(qtbot)
        view.refresh()
        tool = _v2(view)
        msgs: list[str] = []
        tool._status = lambda m, t=3000: msgs.append(m)  # type: ignore[assignment]
        wire = next(iter(session.layout.wires))
        tool._hint_free_pin_under(WireSelectable(wire=wire), QPointF(2.5, 1.5))
        assert msgs == []  # wire hit → never a free-pin hint


class TestNetContext:
    """Net context (drives bright pads) comes only from a pin-pad or wire click.

    Click a pad → context = that pin's net; click body → no context; re-click a
    different pin → context moves; click empty/Esc → cleared.  Pin number is
    metadata on the Hit (excluded from identity), so a pad click on a selected
    component doesn't read as a new object.
    """

    @staticmethod
    def _ctx_session(qtbot):  # type: ignore[no-untyped-def]
        import uuid as _uuid
        from pathlib import Path

        from protoboard.domain.board.board import Board
        from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
        from protoboard.domain.board.pads import PadLayers
        from protoboard.domain.components.definition import ComponentDefinition
        from protoboard.domain.components.footprints import SIL
        from protoboard.domain.components.instance import ComponentInstance
        from protoboard.domain.layout.placement import ComponentPlacement
        from protoboard.domain.schematic.net import Net, PinRef
        from protoboard.geometry import Point
        from protoboard.services.library.loader import load_library

        grid = BoardGrid(
            width_policy=SizePolicy.HUG, height_policy=SizePolicy.HUG,
            cell=PadAtom(
                width_policy=SizePolicy.HUG, height_policy=SizePolicy.HUG,
                layers=PadLayers(top=True, bottom=True, plated_through=True),
            ),
            rows=10, cols=10, row_gap=0.0, col_gap=0.0,
        )
        lib = load_library([Path(__file__).parent.parent / "fixtures" / "library"])
        sess = EditorSession(board=Board(root=grid), library=lib)
        # 2-pin component: pin1 on netA, pin2 on netB.
        sess.components["D2"] = ComponentDefinition(
            name="D2", footprint=SIL(2), pin_names={1: "A", 2: "B"})
        inst = ComponentInstance.new(definition_ref="D2", refdes="U1", value="")
        inst = inst.bind(sess.components["D2"], {})
        sess.instances[inst.id] = inst
        sess.layout.placements[inst.id] = ComponentPlacement(
            component_id=inst.id, position=Point(2.5, 2.5), rotation=0, side="top")
        na = Net(id=_uuid.uuid4(), name="NA", pins={PinRef(inst.id, 1)})
        nb = Net(id=_uuid.uuid4(), name="NB", pins={PinRef(inst.id, 2)})
        sess.schematic.nets[na.id] = na
        sess.schematic.nets[nb.id] = nb
        sess.invalidate_graph()
        view = BoardView(sess)
        qtbot.addWidget(view)
        view.refresh()
        return sess, view, inst, na, nb

    def test_pad_click_sets_pin_net_context(self, qtbot) -> None:
        session, view, inst, na, nb = self._ctx_session(qtbot)
        tool = _v2(view)
        _click(tool, 2.5, 2.5)  # pin1 pad → netA
        assert tool.context_net_id == na.id
        _click(tool, 2.5, 3.5)  # re-click pin2 pad → context moves to netB
        assert tool.context_net_id == nb.id
        assert inst.id in _comp_ids(tool)  # component stays selected

    def test_body_click_clears_context(self, qtbot) -> None:
        session, view, inst, na, nb = self._ctx_session(qtbot)
        tool = _v2(view)
        _click(tool, 2.5, 2.5)  # pin1 → netA context
        assert tool.context_net_id == na.id
        _click(tool, 2.5, 2.92)  # body band (0.42 off pad) → no context
        assert tool.context_net_id is None
        assert inst.id in _comp_ids(tool)  # still selected (body click)

    def test_context_persists_across_rotate(self, qtbot) -> None:
        session, view, inst, na, nb = self._ctx_session(qtbot)
        tool = _v2(view)
        _click(tool, 2.5, 2.5)  # netA context
        assert tool.context_net_id == na.id
        tool._rotate_selected(delta=90)
        assert tool.context_net_id == na.id  # context survives the rotate

    def test_empty_click_clears_context(self, qtbot) -> None:
        session, view, inst, na, nb = self._ctx_session(qtbot)
        tool = _v2(view)
        _click(tool, 2.5, 2.5)
        assert tool.context_net_id == na.id
        _click(tool, 8.5, 8.5)  # empty board
        assert tool.context_net_id is None


class TestContextMenu:
    """Right-click rebuilds the unified item-aware menu (bug #2 regression).

    The v2 swap left only BoardView's generic tool-action menu; the item-aware
    sections (wire ops, lock, assign/disconnect, select-by-net, highlight) were
    gone.  We inspect the built menu's action→data map directly — QMenu.exec is
    un-mockable in PySide6, so on_right_press is not driven headless.
    """

    @staticmethod
    def _menu_datas(tool, x: float, y: float):  # type: ignore[no-untyped-def]
        from PySide6.QtCore import QPointF

        built = tool._build_context_menu(QPointF(x, y))
        assert built is not None, "expected a menu over an item"
        _menu, items = built
        return list(items.values())

    def test_empty_canvas_returns_no_menu(self, qtbot) -> None:
        from PySide6.QtCore import QPointF

        _session, view = _wire_session(qtbot)
        tool = _v2(view)
        # Far from any wire/component → no item menu (BoardView falls back).
        assert tool._build_context_menu(QPointF(15.0, 15.0)) is None

    def test_wire_section_present_over_wire(self, qtbot) -> None:
        session, view = _wire_session(qtbot)
        tool = _v2(view)
        _select_wire(tool, session)
        actions = {d[0] for d in self._menu_datas(tool, 2.5, 1.5)}
        # Wire-mutation entries + universal view section are all present.
        assert "wire_insulated" in actions
        assert "wire_side" in actions
        assert "simplify_wire" in actions
        assert "view_center" in actions
        assert "view_escape" in actions

    def test_wire_side_action_executes(self, qtbot) -> None:
        from PySide6.QtCore import QPointF

        session, view = _wire_session(qtbot)
        tool = _v2(view)
        _select_wire(tool, session)
        wire = next(iter(session.layout.wires))
        side_before = wire.side
        depth = _depth(session)

        data = next(
            d for d in self._menu_datas(tool, 2.5, 1.5) if d[0] == "wire_side"
        )
        tool._menu_dispatch(data, QPointF(2.5, 1.5))

        assert _depth(session) == depth + 1  # a command ran
        wire_after = next(iter(session.layout.wires))
        assert wire_after.side != side_before

    def test_component_side_action_present_and_executes(
        self, board_view, session
    ) -> None:
        from PySide6.QtCore import QPointF

        inst = _place(board_view, session, 0.5, 0.5)
        tool = _v2(board_view)
        _press(tool, 0.5, 0.5)  # select the component
        side_before = session.layout.placements[inst.id].side
        depth = _depth(session)

        datas = self._menu_datas(tool, 0.5, 0.5)
        assert any(d[0] == "component_side" for d in datas)
        data = next(d for d in datas if d[0] == "component_side")
        tool._menu_dispatch(data, QPointF(0.5, 0.5))

        assert _depth(session) == depth + 1  # a command ran
        assert session.layout.placements[inst.id].side != side_before

    def test_tool_actions_added_to_menu(self, qtbot) -> None:
        """The menu augments — it carries the tool's own action shortcuts too."""
        from PySide6.QtCore import QPointF
        from PySide6.QtWidgets import QMenu

        session, view = _wire_session(qtbot)
        tool = _v2(view)
        _select_wire(tool, session)
        built = tool._build_context_menu(QPointF(2.5, 1.5))
        assert built is not None
        menu, items = built
        assert isinstance(menu, QMenu)
        # More QActions exist than the data-carrying ones we dispatch on —
        # the extra ones are the tool-action section (augmentation).
        all_actions = [a for a in menu.actions() if not a.isSeparator()]
        assert len(all_actions) > len(items)

    def test_component_lock_and_pin_entries(self, qtbot) -> None:
        """Over a component: lock + clicked-pin assign/disconnect appear."""
        session, view = _wire_session(qtbot)
        view.refresh()
        tool = _v2(view)
        # X1 is a 1-pin component at (1.5, 1.5), pin 1 bound to the wire's net.
        inst = next(iter(session.instances.values()))
        tool._select_primary(inst.id)
        actions = {d[0] for d in self._menu_datas(tool, 1.5, 1.5)}
        assert "lock" in actions
        assert "assign_pin" in actions  # pin under cursor → assign offered
        assert "disconnect_pin" in actions  # pin 1 is netted → disconnect offered

    def test_lock_action_executes(self, qtbot) -> None:
        from PySide6.QtCore import QPointF

        session, view = _wire_session(qtbot)
        view.refresh()
        tool = _v2(view)
        inst = next(iter(session.instances.values()))
        tool._select_primary(inst.id)
        assert not session.layout.placements[inst.id].locked
        depth = _depth(session)

        data = next(
            d for d in self._menu_datas(tool, 1.5, 1.5) if d[0] == "lock"
        )
        tool._menu_dispatch(data, QPointF(1.5, 1.5))

        assert _depth(session) == depth + 1
        assert session.layout.placements[inst.id].locked

    def test_disconnect_pin_executes(self, qtbot) -> None:
        from PySide6.QtCore import QPointF

        from protoboard.domain.schematic.net import PinRef

        session, view = _wire_session(qtbot)
        view.refresh()
        tool = _v2(view)
        inst = next(iter(session.instances.values()))
        tool._select_primary(inst.id)
        ref = PinRef(component_id=inst.id, pin_number=1)
        assert session.schematic.net_for_pin(ref) is not None  # netted first

        data = next(
            d for d in self._menu_datas(tool, 1.5, 1.5) if d[0] == "disconnect_pin"
        )
        tool._menu_dispatch(data, QPointF(1.5, 1.5))
        assert session.schematic.net_for_pin(ref) is None  # now disconnected

    def test_select_section_present_over_wire(self, qtbot) -> None:
        session, view = _wire_session(qtbot)
        tool = _v2(view)
        _select_wire(tool, session)
        actions = {d[0] for d in self._menu_datas(tool, 2.5, 1.5)}
        # The wire is on a net → select-by-net entries appear.
        assert "select_net_components" in actions
        assert "select_net_wires" in actions
        assert "select_net_all" in actions

    def test_select_net_wires_promotes_selection(self, qtbot) -> None:
        from PySide6.QtCore import QPointF

        session, view = _wire_session(qtbot)
        view.refresh()
        tool = _v2(view)
        # Start from a component selection, then "Wires of net" → wire selected.
        inst = next(iter(session.instances.values()))
        tool._select_primary(inst.id)
        wire = next(iter(session.layout.wires))

        data = next(
            d for d in self._menu_datas(tool, 1.5, 1.5)
            if d[0] == "select_net_wires"
        )
        tool._menu_dispatch(data, QPointF(1.5, 1.5))
        assert wire.id in tool._selected_wire_ids

    def test_highlight_section_and_push(self, qtbot) -> None:
        from PySide6.QtCore import QPointF

        session, view = _wire_session(qtbot)
        tool = _v2(view)
        _select_wire(tool, session)
        actions = {d[0] for d in self._menu_datas(tool, 2.5, 1.5)}
        assert "highlight_selection" in actions
        assert "clear_highlight" in actions

        assert not session.highlight_stack.has_persistent()
        data = next(
            d for d in self._menu_datas(tool, 2.5, 1.5)
            if d[0] == "highlight_selection"
        )
        tool._menu_dispatch(data, QPointF(2.5, 1.5))
        assert session.highlight_stack.has_persistent()  # selection spotlit

        # Clear removes it.
        tool._menu_dispatch(("clear_highlight",), QPointF(2.5, 1.5))
        assert not session.highlight_stack.has_persistent()


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

    def test_armed_move_claims_wasd_and_arrows(
        self, board_view, session
    ) -> None:
        # Bug: WASD switched tools / arrows scrolled because the armed mode
        # didn't veto the global shortcuts.  While armed-move is live the step
        # keys (arrow OR WASD, by action id) must be claimed so neither a global
        # QAction (S=Select…) nor the arrow-scroll handler steals them.
        _place(board_view, session, 0.5, 0.5)
        tool = _v2(board_view)
        _press(tool, 0.5, 0.5)
        # Idle: 'S' is the global Select-tool shortcut, not the tool's.
        assert tool.wants_key(_key_event(Qt.Key.Key_S)) is False
        _key(tool, Qt.Key.Key_G)  # arm move
        assert tool._armed is not None
        for k in (
            Qt.Key.Key_S, Qt.Key.Key_W, Qt.Key.Key_A, Qt.Key.Key_D,
            Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down,
        ):
            assert tool.wants_key(_key_event(k)) is True
        _key(tool, Qt.Key.Key_Escape)  # disarm
        assert tool.wants_key(_key_event(Qt.Key.Key_S)) is False  # released


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


class TestSelectionBroadcastOnClick:
    """Every click-driven selection change must emit ``selection_changed`` so the
    side panels (which subscribe to it) stay in sync — not just the ~6
    programmatic paths that called ``_notify_selection`` before.
    """

    @staticmethod
    def _spy(session: EditorSession) -> list[tuple]:
        events: list[tuple] = []
        session.selection_changed.connect(
            lambda c, w, n: events.append((c, w, n))
        )
        return events

    def test_plain_click_select_broadcasts(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 0.5, 0.5)
        tool = _v2(board_view)
        events = self._spy(session)
        _click(tool, 0.5, 0.5)
        assert events, "a plain component click must broadcast selection_changed"
        assert inst.id in events[-1][0]

    def test_empty_canvas_click_broadcasts_clear(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 0.5, 0.5)
        tool = _v2(board_view)
        tool._select_primary(inst.id)
        events = self._spy(session)
        # Click far from any component → rubber-band sub-threshold click → clear.
        _click(tool, 9.0, 9.0)
        assert events, "an empty-canvas click must broadcast the cleared selection"
        assert not events[-1][0]  # no components in the final broadcast

    def test_rubber_band_broadcasts(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        a = _place(board_view, session, 1.0, 1.0)
        b = _place(board_view, session, 6.0, 6.0)
        tool = _v2(board_view)
        events = self._spy(session)
        _band_select(tool, session, [a.id, b.id])  # band starts off-bbox
        assert events, "a rubber-band commit must broadcast selection_changed"
        assert {a.id, b.id} <= events[-1][0]
