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
