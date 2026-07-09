# SPDX-License-Identifier: MIT
"""Tests for CutTool — strip splitting, on-pad vs between-pad cuts."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent
from pytestqt.qtbot import QtBot

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import HBox, SizePolicy, StripAtom, VBox
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.commands.base import ComponentState
from protoboard.gui.board_view import BoardView
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.cut_tool import CutTool
from protoboard.services.library.loader import load_library
from tests.gui.input_helpers import pointer

_LIBRARY_DIR = Path(__file__).parent.parent.parent / "library"
_HUG = SizePolicy.HUG
_LAYERS = PadLayers(top=False, bottom=True, plated_through=False)


def _strip_row(length: int) -> HBox:
    """One strip row: HBox(gap=0)[StripAtom(length)]."""
    seg = StripAtom(
        width_policy=_HUG, height_policy=_HUG,
        length=length, orientation="horizontal", layers=_LAYERS,
    )
    return HBox(width_policy=_HUG, height_policy=_HUG,
                children=[seg], gap=0.0)

def _stripboard(rows: int = 3, cols: int = 5) -> Board:
    rows_list = [_strip_row(cols) for _ in range(rows)]
    return Board(root=VBox(
        width_policy=_HUG, height_policy=_HUG,
        children=list(rows_list), gap=1.0,      ))


def _session(rows: int = 3, cols: int = 5) -> EditorSession:
    return EditorSession(
        board=_stripboard(rows, cols), library=load_library([_LIBRARY_DIR])
    )


def _click(view: BoardView, x: float, y: float) -> None:
    tool = view._active_tool
    assert tool is not None
    tool.on_press(pointer(x, y))


def _strip_atoms(board: Board) -> list[StripAtom]:
    return [a for a, _ in board.atoms() if isinstance(a, StripAtom)]


class TestCutToolSplitsBetween:
    """Clicking between two pads splits the segment in two."""

    def test_click_between_pads_splits(self, qtbot: QtBot) -> None:
        session = _session(rows=1, cols=5)
        view = BoardView(session)
        qtbot.addWidget(view)
        view.set_tool(CutTool())
        # Gap between pad 2 (x=1.5) and pad 3 (x=2.5) → click at x=2.0
        _click(view, 2.0, 0.5)
        atoms = _strip_atoms(session.board)
        lengths = sorted(a.length for a in atoms)
        assert lengths == [2, 3]
        assert all(not a.isolated for a in atoms)

    def test_click_far_away_no_change(self, qtbot: QtBot) -> None:
        session = _session(rows=1, cols=5)
        view = BoardView(session)
        qtbot.addWidget(view)
        view.set_tool(CutTool())
        _click(view, 99.0, 99.0)
        atoms = _strip_atoms(session.board)
        assert len(atoms) == 1
        assert atoms[0].length == 5

    def test_emit_once_per_click(self, qtbot: QtBot) -> None:
        session = _session(rows=1, cols=5)
        view = BoardView(session)
        qtbot.addWidget(view)
        view.set_tool(CutTool())
        count: list[None] = []
        session.layout_changed.connect(lambda: count.append(None))
        _click(view, 2.0, 0.5)
        assert len(count) == 1


class TestCutToolOnPad:
    """Clicking on a pad isolates that pad."""

    def test_click_on_pad_isolates(self, qtbot: QtBot) -> None:
        session = _session(rows=1, cols=5)
        view = BoardView(session)
        qtbot.addWidget(view)
        view.set_tool(CutTool())
        # Pad 3 is at x=2.5 — click directly on it
        _click(view, 2.5, 0.5)
        atoms = _strip_atoms(session.board)
        isolated = [a for a in atoms if a.isolated]
        active = [a for a in atoms if not a.isolated]
        assert len(isolated) == 1
        assert isolated[0].length == 1
        assert sorted(a.length for a in active) == [2, 2]

    def test_click_on_first_pad_isolates(self, qtbot: QtBot) -> None:
        session = _session(rows=1, cols=4)
        view = BoardView(session)
        qtbot.addWidget(view)
        view.set_tool(CutTool())
        # Pad 1 at x=0.5
        _click(view, 0.5, 0.5)
        atoms = _strip_atoms(session.board)
        isolated = [a for a in atoms if a.isolated]
        assert len(isolated) == 1
        remainder = [a for a in atoms if not a.isolated]
        assert len(remainder) == 1
        assert remainder[0].length == 3


class TestCutToolUndo:
    def test_undo_restores_original(self, qtbot: QtBot) -> None:
        session = _session(rows=1, cols=5)
        view = BoardView(session)
        qtbot.addWidget(view)
        view.set_tool(CutTool())
        _click(view, 2.0, 0.5)
        assert len(_strip_atoms(session.board)) == 2
        session.stack.undo(cast(ComponentState, session))
        atoms = _strip_atoms(session.board)
        assert len(atoms) == 1
        assert atoms[0].length == 5
