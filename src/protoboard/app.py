# SPDX-License-Identifier: MIT
"""Entry point for the Protoboard Designer application."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.gui.main_window import MainWindow
from protoboard.gui.session import EditorSession
from protoboard.services.library.loader import load_library

_LIBRARY_DIR = Path(__file__).parent.parent.parent.parent / "library"


def _default_board() -> Board:
    grid = BoardGrid(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        ),
        rows=20,
        cols=30,
        row_gap=0.0,
        col_gap=0.0,
    )
    return Board(root=grid)


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Protoboard Designer")

    library_dirs = [_LIBRARY_DIR] if _LIBRARY_DIR.exists() else []
    library = load_library(library_dirs)
    board = _default_board()
    session = EditorSession(board=board, library=library)

    window = MainWindow(session)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
