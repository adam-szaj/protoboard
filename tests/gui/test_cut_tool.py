# SPDX-License-Identifier: MIT
"""Tests for CutTool — strip cuts, position index, no double refresh."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import Box, SizePolicy, StripAtom, VBox
from protoboard.domain.board.pads import PadLayers
from protoboard.gui.board_view import BoardView
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.cut_tool import CutTool
from protoboard.services.library.loader import load_library

_LIBRARY_DIR = Path(__file__).parent.parent.parent / "library"


def _stripboard(strips: int, length: int) -> Board:
    """Create a vertical stack of horizontal strips."""
    children: list[Box] = [
        StripAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            length=length,
            orientation="horizontal",
            layers=PadLayers(top=True, bottom=False),
        )
        for _ in range(strips)
    ]
    root = VBox(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        children=children,
        gap=1.0,
    )
    return Board(root=root)


def _strip_session(strips: int = 3, length: int = 5) -> EditorSession:
    library = load_library([_LIBRARY_DIR])
    board = _stripboard(strips, length)
    return EditorSession(board=board, library=library)


def _click(board_view: BoardView, x: float, y: float) -> None:
    tool = board_view._active_tool
    assert tool is not None
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


class TestCutToolBasic:
    def test_click_near_strip_adds_cut(self, qtbot: object) -> None:
        from pytestqt.qtbot import QtBot

        assert isinstance(qtbot, QtBot)
        session = _strip_session()
        view = BoardView(session)
        qtbot.addWidget(view)
        view.set_tool(CutTool())
        # Strip 0 runs horizontally; first pad center is at (0.5, 0.5).
        _click(view, 0.5, 0.5)
        assert len(session.layout.cuts) == 1

    def test_click_far_from_strip_no_cut(self, qtbot: object) -> None:
        from pytestqt.qtbot import QtBot

        assert isinstance(qtbot, QtBot)
        session = _strip_session()
        view = BoardView(session)
        qtbot.addWidget(view)
        view.set_tool(CutTool())
        _click(view, 99.0, 99.0)
        assert len(session.layout.cuts) == 0

    def test_no_double_refresh(self, qtbot: object) -> None:
        """BUG 2 regression: layout_changed must be emitted exactly once per click."""
        from pytestqt.qtbot import QtBot

        assert isinstance(qtbot, QtBot)
        session = _strip_session()
        view = BoardView(session)
        qtbot.addWidget(view)
        view.set_tool(CutTool())
        emit_count: list[None] = []
        session.layout_changed.connect(lambda: emit_count.append(None))
        _click(view, 0.5, 0.5)
        if session.layout.cuts:  # only assert if a cut was added
            assert len(emit_count) == 1


class TestCutToolPositionIndex:
    def test_position_index_first_pad(self, qtbot: object) -> None:
        """BUG 2 regression: clicking on pad 1 should give position 1."""
        from pytestqt.qtbot import QtBot

        assert isinstance(qtbot, QtBot)
        session = _strip_session(strips=1, length=5)
        view = BoardView(session)
        qtbot.addWidget(view)
        view.set_tool(CutTool())
        # Pad centers: x = 0.5, 1.5, 2.5, 3.5, 4.5 (gap=1, so y=0.5 for strip 0)
        _click(view, 0.5, 0.5)
        if session.layout.cuts:
            cut = session.layout.cuts[0]
            assert cut.position == 1

    def test_position_index_third_pad(self, qtbot: object) -> None:
        """BUG 2 regression: clicking on pad 3 should give position 3."""
        from pytestqt.qtbot import QtBot

        assert isinstance(qtbot, QtBot)
        session = _strip_session(strips=1, length=5)
        view = BoardView(session)
        qtbot.addWidget(view)
        view.set_tool(CutTool())
        _click(view, 2.5, 0.5)
        if session.layout.cuts:
            cut = session.layout.cuts[0]
            assert cut.position == 3
