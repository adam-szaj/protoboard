# SPDX-License-Identifier: MIT
"""Shared pytest fixtures for GUI tests."""

from __future__ import annotations

import os

# Run Qt headless by default so the suite needs no X server / display.
# Must be set before PySide6 is imported (pulled in transitively below via
# pytest-qt and the ``protoboard.gui`` modules). An explicit env override
# (e.g. ``QT_QPA_PLATFORM=xcb``) still wins thanks to ``setdefault``.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.gui.board_view import BoardView
from protoboard.gui.session import EditorSession
from protoboard.services.library.loader import load_library

_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "library"


def _make_perfboard(rows: int = 5, cols: int = 5) -> Board:
    grid = BoardGrid(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        ),
        rows=rows,
        cols=cols,
        row_gap=0.0,
        col_gap=0.0,
    )
    return Board(root=grid)


@pytest.fixture()
def session(qtbot: QtBot) -> EditorSession:
    library = load_library([_LIBRARY_DIR])
    sess = EditorSession(board=_make_perfboard(), library=library)
    return sess


@pytest.fixture()
def board_view(qtbot: QtBot, session: EditorSession) -> BoardView:
    view = BoardView(session)
    qtbot.addWidget(view)
    return view
