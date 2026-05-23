# SPDX-License-Identifier: MIT
"""Tests for BoardView — snap, refresh, tool management."""

from __future__ import annotations

from PySide6.QtCore import QPointF

from protoboard.geometry import Point
from protoboard.gui.board_view import BoardView, _Tool
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.select_tool import SelectTool


class TestSnap:
    def test_snap_returns_nearest_pad(self, board_view: BoardView) -> None:
        # Board is 5×5, gap=0, so pads at (0.5,0.5), (1.5,0.5) etc.
        result = board_view.snap(QPointF(0.4, 0.4))
        assert result == Point(0.5, 0.5)

    def test_snap_far_from_origin(self, board_view: BoardView) -> None:
        result = board_view.snap(QPointF(3.6, 3.6))
        assert result == Point(3.5, 3.5)

    def test_snap_exact_pad_center(self, board_view: BoardView) -> None:
        result = board_view.snap(QPointF(2.5, 2.5))
        assert result == Point(2.5, 2.5)


class TestRefresh:
    def test_refresh_builds_bg_cache_display_list(self, board_view: BoardView) -> None:
        # Board layer no longer lives in the scene — it's rasterised
        # into the background cache by drawBackground.  Verify that the
        # display list has the expected primitives after a refresh.
        board_view.refresh()
        board_view._ensure_bg_cache()
        from protoboard.gui.renderers.board_renderer import BoardDisplayList
        assert isinstance(board_view._bg_display_list, BoardDisplayList)
        # 5×5 = 25 pads expected for the test fixture board.
        assert len(board_view._bg_display_list.pads) >= 25

    def test_refresh_is_idempotent(self, board_view: BoardView) -> None:
        board_view.refresh()
        count1 = len(board_view.scene().items())
        board_view.refresh()
        count2 = len(board_view.scene().items())
        assert count1 == count2


class TestToolManagement:
    def test_set_tool_activates_new_tool(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        tool = SelectTool()
        board_view.set_tool(tool)
        assert board_view._active_tool is tool

    def test_set_tool_deactivates_previous(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        deactivated: list[bool] = []

        class TrackingTool(_Tool):
            def deactivate(self) -> None:
                deactivated.append(True)

        board_view.set_tool(TrackingTool())
        board_view.set_tool(SelectTool())
        assert deactivated == [True]

    def test_set_tool_none_clears_active(self, board_view: BoardView) -> None:
        board_view.set_tool(SelectTool())
        board_view.set_tool(None)
        assert board_view._active_tool is None

    def test_layout_changed_triggers_refresh(
        self, board_view: BoardView, session: EditorSession, qtbot: object
    ) -> None:
        # Manually count items before and after adding a wire via layout_changed.
        import uuid

        from protoboard.domain.layout.wire import WireSegment
        from protoboard.geometry import Point

        wire = WireSegment(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(1.5, 0.5)],
            insulated=True,
            side="top",
        )
        session.layout.wires.append(wire)
        session.layout_changed.emit()
        # layout_changed is debounced via a 16ms timer — wait for it to fire.
        from pytestqt.qtbot import QtBot
        assert isinstance(qtbot, QtBot)
        qtbot.wait(50)
        # After refresh the wire's QGraphicsItemGroup must be registered.
        # Pad/board items no longer live in the scene (they're rasterised
        # into the background cache), so counting scene items is no
        # longer a useful proxy for "refresh happened".
        assert wire.id in board_view._wire_groups
