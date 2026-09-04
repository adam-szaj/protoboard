# SPDX-License-Identifier: MIT
"""MainWindow seeds the manual-wire default from AppSettings and persists a
change back to ``app_settings.yaml``.

Regression: the manual-routing wire defaults were hardcoded to
insulated/top and never saved, so the user's choice (and the correct
bare/bottom default) was lost on every restart.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from protoboard.controller import app_settings as APP
from protoboard.controller.app_settings import AppSettings, load_app_settings
from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.gui import keybindings as KB
from protoboard.gui.main_window import MainWindow
from protoboard.gui.session import EditorSession
from protoboard.services.library.loader import load_library

_LIBRARY_DIR = Path(__file__).parent.parent / "fixtures" / "library"


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


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    """Redirect both app-settings and keybindings persistence to throwaway
    files so the test never touches the developer's real ~/.config."""
    monkeypatch.setattr(APP, "_CONFIG_PATH", tmp_path / "app_settings.yaml")
    monkeypatch.setattr(KB, "_KEYBINDINGS_PATH", tmp_path / "keybindings.yaml")


def _window(qtbot, settings: AppSettings) -> MainWindow:
    library = load_library([_LIBRARY_DIR])
    session = EditorSession(board=_make_perfboard(), library=library)
    mw = MainWindow(session, settings)
    qtbot.addWidget(mw)
    return mw


class TestSeedFromSettings:
    def test_default_settings_seed_bare_bottom(self, qtbot) -> None:
        mw = _window(qtbot, AppSettings())
        assert mw._wire_insulated is False
        assert mw._wire_side == "bottom"

    def test_persisted_choice_is_restored(self, qtbot) -> None:
        mw = _window(
            qtbot,
            AppSettings(wire_default_insulated=True, wire_default_side="top"),
        )
        assert mw._wire_insulated is True
        assert mw._wire_side == "top"


class TestPersistOnChange:
    def test_set_side_saves_to_disk(self, qtbot) -> None:
        mw = _window(qtbot, AppSettings())
        mw.set_wire_side("top")
        assert load_app_settings().wire_default_side == "top"

    def test_set_insulated_saves_to_disk(self, qtbot) -> None:
        mw = _window(qtbot, AppSettings())
        mw.set_wire_insulated(True)
        assert load_app_settings().wire_default_insulated is True
