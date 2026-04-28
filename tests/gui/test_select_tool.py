# SPDX-License-Identifier: MIT
"""Tests for SelectTool — selection, drag-move, rotate, delete, state management."""

from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent

from protoboard.domain.components.instance import ComponentInstance
from protoboard.geometry import Point
from protoboard.gui.board_view import BoardView
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.place_tool import PlaceTool
from protoboard.gui.tools.select_tool import SelectTool


def _place_component(
    board_view: BoardView, session: EditorSession, x: float, y: float
) -> ComponentInstance:
    """Place an R component and return the instance."""
    board_view.set_tool(PlaceTool("R"))
    tool = board_view._active_tool
    assert isinstance(tool, PlaceTool)
    tool.on_press(
        QMouseEvent(
            QMouseEvent.Type.MouseButtonPress,
            QPointF(x, y),
            QPointF(x, y),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        ),
        QPointF(x, y),
    )
    return list(session.instances.values())[-1]


def _press(tool: SelectTool, x: float, y: float) -> None:
    tool.on_press(
        QMouseEvent(
            QMouseEvent.Type.MouseButtonPress,
            QPointF(x, y),
            QPointF(x, y),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        ),
        QPointF(x, y),
    )


def _move(tool: SelectTool, x: float, y: float) -> None:
    tool.on_move(
        QMouseEvent(
            QMouseEvent.Type.MouseMove,
            QPointF(x, y),
            QPointF(x, y),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        ),
        QPointF(x, y),
    )


def _release(tool: SelectTool, x: float, y: float) -> None:
    tool.on_release(
        QMouseEvent(
            QMouseEvent.Type.MouseButtonRelease,
            QPointF(x, y),
            QPointF(x, y),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        ),
        QPointF(x, y),
    )


def _key(tool: SelectTool, key: Qt.Key) -> None:
    tool.on_key(QKeyEvent(QKeyEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier))


class TestSelectToolSelection:
    def test_click_near_component_selects_it(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place_component(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 0.5, 0.5)
        assert tool._selected_id == inst.id

    def test_click_empty_area_deselects(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        _place_component(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 0.5, 0.5)
        assert tool._selected_id is not None
        _press(tool, 10.0, 10.0)  # far from any component
        assert tool._selected_id is None

    def test_highlight_added_to_scene_on_select(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        _place_component(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 0.5, 0.5)
        assert tool._highlight is not None


class TestSelectToolDrag:
    def test_drag_moves_component(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place_component(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 0.5, 0.5)
        _move(tool, 2.5, 0.5)
        _release(tool, 2.5, 0.5)
        placement = session.layout.placements[inst.id]
        assert placement.position == Point(2.5, 0.5)

    def test_drag_creates_undo_entry(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        _place_component(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 0.5, 0.5)
        _move(tool, 2.5, 0.5)
        _release(tool, 2.5, 0.5)
        assert session.stack.can_undo()

    def test_drag_undo_restores_position(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place_component(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 0.5, 0.5)
        _move(tool, 2.5, 0.5)
        _release(tool, 2.5, 0.5)
        session.stack.undo(session)
        placement = session.layout.placements[inst.id]
        assert placement.position == Point(0.5, 0.5)

    def test_highlight_restored_after_drag(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """BUG 5 regression: component must remain selected after drag+release."""
        inst = _place_component(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 0.5, 0.5)
        _move(tool, 2.5, 0.5)
        _release(tool, 2.5, 0.5)
        assert tool._selected_id == inst.id

    def test_drag_state_reset_on_deactivate(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """BUG 3 regression: _dragging must be False after deactivate."""
        _place_component(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 0.5, 0.5)
        tool._dragging = True  # simulate mid-drag
        tool.deactivate()
        assert tool._dragging is False
        assert tool._drag_orig_pos is None


class TestSelectToolRotate:
    def test_rotate_key_changes_rotation(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place_component(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 0.5, 0.5)
        _key(tool, Qt.Key.Key_R)
        placement = session.layout.placements[inst.id]
        assert placement.rotation == 90

    def test_rotate_is_undoable(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place_component(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 0.5, 0.5)
        _key(tool, Qt.Key.Key_R)
        session.stack.undo(session)
        placement = session.layout.placements[inst.id]
        assert placement.rotation == 0


class TestSelectToolDelete:
    def test_delete_removes_placement(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        _place_component(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 0.5, 0.5)
        _key(tool, Qt.Key.Key_Delete)
        assert len(session.layout.placements) == 0

    def test_delete_clears_selection(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        _place_component(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 0.5, 0.5)
        _key(tool, Qt.Key.Key_Delete)
        assert tool._selected_id is None


class TestSelectToolSceneCleared:
    def test_scene_cleared_drops_highlight_ref(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """on_scene_cleared must drop the stale graphics item reference."""
        _place_component(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 0.5, 0.5)
        assert tool._highlight is not None
        tool.on_scene_cleared()
        assert tool._highlight is None

    def test_scene_cleared_preserves_selected_id_for_drag(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """_selected_id must survive on_scene_cleared so drag can commit."""
        inst = _place_component(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 0.5, 0.5)
        assert tool._selected_id == inst.id
        tool.on_scene_cleared()
        assert tool._selected_id == inst.id  # must survive for drag commit
