# SPDX-License-Identifier: MIT
"""Integration tests for SelectToolV2 — the rewritten selection tool (Phase 2).

Drives the new tool through a real BoardView/session, asserting it wires the
headless SelectionService / HitTester / HighlightController correctly: click to
select, rubber-band, rotate, ESC-pop, and routing a click into an active pick.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent

from protoboard.domain.components.instance import ComponentInstance
from protoboard.gui.board_view import BoardView
from protoboard.gui.selection import Stop
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.place_tool import PlaceTool
from protoboard.gui.tools.select_tool_v2 import SelectToolV2


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
    tool: SelectToolV2, x: float, y: float,
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


def _move(tool: SelectToolV2, x: float, y: float) -> None:
    tool.on_move(
        QMouseEvent(
            QMouseEvent.Type.MouseMove,
            QPointF(x, y), QPointF(x, y),
            Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        ),
        QPointF(x, y),
    )


def _release(tool: SelectToolV2, x: float, y: float) -> None:
    tool.on_release(
        QMouseEvent(
            QMouseEvent.Type.MouseButtonRelease,
            QPointF(x, y), QPointF(x, y),
            Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        ),
        QPointF(x, y),
    )


def _key(
    tool: SelectToolV2, key: Qt.Key,
    mods: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
) -> None:
    tool.on_key(QKeyEvent(QKeyEvent.Type.KeyPress, key, mods))


def _v2(board_view: BoardView) -> SelectToolV2:
    board_view.set_tool(SelectToolV2())
    tool = board_view._active_tool
    assert isinstance(tool, SelectToolV2)
    return tool


def _comp_ids(tool: SelectToolV2) -> set:
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
    def test_r_rotates_selected(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 0.5, 0.5)
        tool = _v2(board_view)
        _press(tool, 0.5, 0.5)
        before = session.layout.placements[inst.id].rotation
        _key(tool, Qt.Key.Key_R)
        after = session.layout.placements[inst.id].rotation
        assert after == (before + 90) % 360

    def test_rotate_around_point_moves_component(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 0.5, 0.5)
        tool = _v2(board_view)
        _press(tool, 0.5, 0.5)
        before = session.layout.placements[inst.id].position
        tool.begin_rotate_around_point()
        assert tool._pivot_capture_pending is True
        _press(tool, 3.0, 3.0)  # capture pivot away from the part
        assert tool._rotate_pivot is not None
        _key(tool, Qt.Key.Key_R)
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
        assert tool._gesture is None

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
        _press(tool, 2.0, 1.0)
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
        _press(tool, 2.0, 1.0)
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
        tool._selection.request_pick(
            "component", Stop.count(1), on_done=lambda xs: done.append(xs)
        )
        # A click during an active pick feeds it, not the normal selection.
        _press(tool, 0.5, 0.5)
        assert len(done) == 1
        assert done[0][0].to_hit().id == inst.id
        # Pick completed → selection unchanged by the pick click.
        assert tool._selection.active_pick is None

    def test_esc_cancels_active_pick(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        _place(board_view, session, 0.5, 0.5)
        tool = _v2(board_view)
        cancelled: list[bool] = []
        tool._selection.request_pick(
            "component", Stop.confirm(),
            on_done=lambda xs: None, on_cancel=lambda: cancelled.append(True),
        )
        _key(tool, Qt.Key.Key_Escape)
        assert cancelled == [True]
        assert tool._selection.active_pick is None
