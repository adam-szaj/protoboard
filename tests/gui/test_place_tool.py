# SPDX-License-Identifier: MIT
"""Tests for PlaceTool — placement, undo, rotation, refdes generation."""

from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent

from protoboard.gui.board_view import BoardView
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.place_tool import PlaceTool


def _left_click(tool: PlaceTool, x: float, y: float) -> None:
    """Click once (anchor). If the tool enters drag_pin phase, click again to confirm."""
    ev = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(x, y),
        QPointF(x, y),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    tool.on_press(ev, QPointF(x, y))
    # If the component has bendable pins, confirm each drag pin with a click at
    # the same position (accepts the default offset).
    while tool._phase == "drag_pin":
        tool.on_press(ev, QPointF(x, y))


def _key_r(tool: PlaceTool) -> None:
    tool.on_key(
        QKeyEvent(
            QKeyEvent.Type.KeyPress,
            Qt.Key.Key_R,
            Qt.KeyboardModifier.NoModifier,
        )
    )


class TestPlaceToolPlacement:
    def test_place_adds_to_layout(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(PlaceTool("R"))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        _left_click(tool, 0.5, 0.5)
        assert len(session.layout.placements) == 1

    def test_place_adds_instance(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(PlaceTool("R"))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        _left_click(tool, 0.5, 0.5)
        assert len(session.instances) == 1

    def test_place_snaps_to_pad(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(PlaceTool("R"))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        _left_click(tool, 0.4, 0.4)
        placement = list(session.layout.placements.values())[0]
        from protoboard.geometry import Point

        assert placement.position == Point(0.5, 0.5)

    def test_place_default_rotation_zero(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(PlaceTool("R"))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        _left_click(tool, 0.5, 0.5)
        placement = list(session.layout.placements.values())[0]
        assert placement.rotation == 0


class TestPlaceToolUndo:
    def test_undo_removes_placement(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(PlaceTool("R"))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        _left_click(tool, 0.5, 0.5)
        assert len(session.layout.placements) == 1
        session.stack.undo(session)
        assert len(session.layout.placements) == 0

    def test_undo_removes_instance(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """BUG 6 regression: undo must also remove instance from session.instances."""
        board_view.set_tool(PlaceTool("R"))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        _left_click(tool, 0.5, 0.5)
        assert len(session.instances) == 1
        session.stack.undo(session)
        assert len(session.instances) == 0

    def test_redo_restores_placement(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(PlaceTool("R"))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        _left_click(tool, 0.5, 0.5)
        session.stack.undo(session)
        session.stack.redo(session)
        assert len(session.layout.placements) == 1


class TestPlaceToolRotation:
    def test_rotate_increments_by_90(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(PlaceTool("R"))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        _key_r(tool)
        assert tool._rotation == 90

    def test_rotate_wraps_at_360(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(PlaceTool("R"))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        for _ in range(4):
            _key_r(tool)
        assert tool._rotation == 0

    def test_placed_component_has_rotated_placement(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(PlaceTool("R"))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        _key_r(tool)
        _left_click(tool, 0.5, 0.5)
        placement = list(session.layout.placements.values())[0]
        assert placement.rotation == 90


def _key_event(
    key: Qt.Key, mods: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier
) -> QKeyEvent:
    return QKeyEvent(QKeyEvent.Type.KeyPress, key, mods)


class TestPlaceToolKeymap:
    """PlaceTool claims the transform keys (so the no-op-while-placing global
    rotate/flip actions don't shadow them), dynamically via the KeyMap."""

    def test_wants_rotate_and_flip_keys(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(PlaceTool("R"))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        assert tool.wants_key(_key_event(Qt.Key.Key_R)) is True  # rotate CW
        assert tool.wants_key(
            _key_event(Qt.Key.Key_R, Qt.KeyboardModifier.AltModifier)
        ) is True  # flip
        # An unrelated key is not claimed.
        assert tool.wants_key(_key_event(Qt.Key.Key_K)) is False

    def test_rotate_follows_rebound_global(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        # Rebind the global rotate from "R" to "K"; PlaceTool inherits it.
        board_view.keymap.set_override("global.action.rotate_cw", "K")
        board_view.set_tool(PlaceTool("R"))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        assert tool.wants_key(_key_event(Qt.Key.Key_R)) is False
        assert tool.wants_key(_key_event(Qt.Key.Key_K)) is True
        # [K] now rotates the ghost.
        tool.on_key(_key_event(Qt.Key.Key_K))
        assert tool._rotation == 90


class TestPlaceToolRefdes:
    def test_first_component_gets_r1(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(PlaceTool("R"))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        _left_click(tool, 0.5, 0.5)
        inst = list(session.instances.values())[0]
        assert inst.refdes == "R1"

    def test_second_component_gets_r2(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(PlaceTool("R"))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        _left_click(tool, 0.5, 0.5)
        _left_click(tool, 2.5, 0.5)
        refdeses = {i.refdes for i in session.instances.values()}
        assert refdeses == {"R1", "R2"}


class TestPlaceToolInitialValue:
    """``ComponentDefinition.default_value`` controls what gets written
    into ``ComponentInstance.value`` on a fresh placement."""

    def test_concrete_part_value_equals_name(
        self, board_view: BoardView, session: EditorSession,
    ) -> None:
        # The default fixture's R has no default_value field → falls
        # back to defn.name="R".
        board_view.set_tool(PlaceTool("R"))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        _left_click(tool, 0.5, 0.5)
        inst = list(session.instances.values())[0]
        assert inst.value == "R"

    def test_generic_template_value_is_question_mark(
        self, board_view: BoardView, session: EditorSession,
    ) -> None:
        # Override the in-memory definition with default_value="?" to
        # simulate a "generic" template entry.
        import dataclasses as _dc
        defn = session.components["R"]
        session.components["R"] = _dc.replace(defn, default_value="?")
        board_view.set_tool(PlaceTool("R"))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        _left_click(tool, 0.5, 0.5)
        inst = list(session.instances.values())[0]
        assert inst.value == "?"
