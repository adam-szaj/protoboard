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


@pytest.fixture(autouse=True)
def _isolate_user_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect per-user config stores to a throwaway tmp dir.

    ``recent_footprints`` / ``recent_projects`` persist to
    ``~/.config/protoboard/`` and read it back. Tests must never see a real
    developer's history: a stale "recent footprint" for a fixture component
    (e.g. ``R`` → ``''``) defeats the library picker's single-kind fast-path
    and opens a modal ``FootprintPickerDialog.exec()`` that blocks forever in
    a headless run (diagnosed 2026-06-26).

    These modules bind ``CONFIG_DIR`` / their file path as module-level
    aliases at import time, so patch the bound names directly rather than
    ``protoboard.paths``.
    """
    from protoboard.gui import (
        recent_footprints,
        recent_kind_variants,
        recent_projects,
    )

    cfg = tmp_path / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(recent_footprints, "_CONFIG_DIR", cfg, raising=False)
    monkeypatch.setattr(
        recent_footprints,
        "_RECENT_FILE",
        cfg / "recent_footprints.json",
        raising=False,
    )
    monkeypatch.setattr(recent_kind_variants, "_CONFIG_DIR", cfg, raising=False)
    monkeypatch.setattr(
        recent_kind_variants,
        "_RECENT_FILE",
        cfg / "recent_kind_variants.json",
        raising=False,
    )
    monkeypatch.setattr(recent_projects, "_CONFIG_DIR", cfg, raising=False)
    monkeypatch.setattr(
        recent_projects,
        "_RECENT_FILE",
        cfg / "recent.json",
        raising=False,
    )


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
