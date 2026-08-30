# SPDX-License-Identifier: MIT
"""Tests for RouteTool — wire placement, commit/cancel, preview cleanup."""

from __future__ import annotations

from typing import cast

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent

from protoboard.domain.commands.base import ComponentState
from protoboard.gui.board_view import BoardView
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.gui_tools import RouteTool
from protoboard.gui.tools.route_preview import QtRoutePreview
from tests.gui.input_helpers import key_inp, pointer


def _press(board_view: BoardView, x: float, y: float) -> None:
    """Simulate a left-button press at scene coordinates (x, y)."""
    tool = board_view._active_tool
    assert tool is not None
    tool.on_press(pointer(x, y))


def _enter(board_view: BoardView) -> None:
    tool = board_view._active_tool
    assert tool is not None
    tool.on_key(_key_event(Qt.Key.Key_Return))


def _escape(board_view: BoardView) -> None:
    tool = board_view._active_tool
    assert tool is not None
    tool.on_key(_key_event(Qt.Key.Key_Escape))


def _mouse_event(button: Qt.MouseButton) -> QMouseEvent:
    return QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(0, 0),
        QPointF(0, 0),
        button,
        button,
        Qt.KeyboardModifier.NoModifier,
    )


def _key_event(key: Qt.Key):
    return key_inp(key)


class TestRouteToolCommit:
    def test_commit_adds_wire_to_layout(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(RouteTool())
        _press(board_view, 0.5, 0.5)
        _press(board_view, 1.5, 0.5)
        _enter(board_view)
        assert len(session.layout.wires) == 1

    def test_wire_has_correct_path(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(RouteTool())
        _press(board_view, 0.5, 0.5)
        _press(board_view, 1.5, 0.5)
        _enter(board_view)
        wire = session.layout.wires[0]
        from protoboard.geometry import Point

        assert wire.path[0] == Point(0.5, 0.5)
        assert wire.path[-1] == Point(1.5, 0.5)

    def test_committed_wire_has_pad_vertices_inserted(
        self,
        board_view: BoardView,
        session: EditorSession,
    ) -> None:
        """Manual-routed wire must include every pad it crosses as a vertex
        so simplify/unweave/edit have handles there."""

        board_view.set_tool(RouteTool())
        # Long horizontal — crosses 4 intermediate pads.
        _press(board_view, 0.5, 0.5)
        _press(board_view, 4.5, 0.5)
        _enter(board_view)
        wire = session.layout.wires[0]
        coords = {(p.x, p.y) for p in wire.path}
        assert (0.5, 0.5) in coords
        assert (1.5, 0.5) in coords
        assert (2.5, 0.5) in coords
        assert (3.5, 0.5) in coords
        assert (4.5, 0.5) in coords

    def test_wire_persists_after_commit(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """BUG 1 regression: wire must still be in layout after refresh."""
        board_view.set_tool(RouteTool())
        _press(board_view, 0.5, 0.5)
        _press(board_view, 1.5, 0.5)
        _enter(board_view)
        # Force another refresh to verify wire survives.
        board_view.refresh()
        assert len(session.layout.wires) == 1

    def test_points_cleared_after_commit(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(RouteTool())
        tool = board_view._active_tool
        assert isinstance(tool, RouteTool)
        _press(board_view, 0.5, 0.5)
        _press(board_view, 1.5, 0.5)
        _enter(board_view)
        assert tool._points == []

    def test_preview_cleared_after_commit(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """BUG 1 regression: no stale preview items after commit."""
        board_view.set_tool(RouteTool())
        tool = board_view._active_tool
        assert isinstance(tool, RouteTool)
        _press(board_view, 0.5, 0.5)
        _press(board_view, 1.5, 0.5)
        _enter(board_view)
        pv = tool.preview
        assert isinstance(pv, QtRoutePreview)
        assert pv._segments == [] and pv._live is None
        assert pv._hints == []

    def test_fewer_than_two_points_does_not_commit(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(RouteTool())
        _press(board_view, 0.5, 0.5)
        _enter(board_view)
        assert len(session.layout.wires) == 0

    def test_multiple_wires(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(RouteTool())
        _press(board_view, 0.5, 0.5)
        _press(board_view, 1.5, 0.5)
        _enter(board_view)
        board_view.set_tool(RouteTool())
        _press(board_view, 2.5, 0.5)
        _press(board_view, 3.5, 0.5)
        _enter(board_view)
        assert len(session.layout.wires) == 2

    def test_wire_commit_is_undoable(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(RouteTool())
        _press(board_view, 0.5, 0.5)
        _press(board_view, 1.5, 0.5)
        _enter(board_view)
        assert session.stack.can_undo()
        session.stack.undo(cast(ComponentState, session))
        assert len(session.layout.wires) == 0

    def test_wire_undo_redo(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(RouteTool())
        _press(board_view, 0.5, 0.5)
        _press(board_view, 1.5, 0.5)
        _enter(board_view)
        session.stack.undo(cast(ComponentState, session))
        assert len(session.layout.wires) == 0
        session.stack.redo(cast(ComponentState, session))
        assert len(session.layout.wires) == 1


class TestRouteToolCancel:
    def test_cancel_does_not_add_wire(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(RouteTool())
        _press(board_view, 0.5, 0.5)
        _press(board_view, 1.5, 0.5)
        _escape(board_view)
        assert len(session.layout.wires) == 0

    def test_preview_cleared_after_cancel(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(RouteTool())
        tool = board_view._active_tool
        assert isinstance(tool, RouteTool)
        _press(board_view, 0.5, 0.5)
        _press(board_view, 1.5, 0.5)
        _escape(board_view)
        pv = tool.preview
        assert isinstance(pv, QtRoutePreview)
        assert pv._segments == [] and pv._live is None

    def test_points_cleared_after_cancel(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(RouteTool())
        tool = board_view._active_tool
        assert isinstance(tool, RouteTool)
        _press(board_view, 0.5, 0.5)
        _escape(board_view)
        assert tool._points == []

    def test_deactivate_behaves_like_cancel(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(RouteTool())
        _press(board_view, 0.5, 0.5)
        board_view.set_tool(None)  # triggers deactivate
        assert len(session.layout.wires) == 0
