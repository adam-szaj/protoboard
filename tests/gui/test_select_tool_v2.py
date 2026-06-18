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


def _click(
    tool: SelectToolV2, x: float, y: float,
    mods: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
) -> None:
    """Press + release at the same point — a click (no drag)."""
    _press(tool, x, y, mods)
    _release(tool, x, y)


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


def _select_wire(tool: SelectToolV2, session: EditorSession) -> None:
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
