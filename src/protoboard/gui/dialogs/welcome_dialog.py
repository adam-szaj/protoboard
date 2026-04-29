# SPDX-License-Identifier: MIT
"""Welcome screen shown at startup — New Project or Open Project."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from protoboard.domain.board.board import Board
from protoboard.gui.dialogs.new_project_dialog import NewProjectDialog


class WelcomeDialog(QDialog):
    """Startup screen.

    After ``exec()``, inspect ``result_action`` to see what the user chose:
      - ``"new"``  → ``board()`` and ``project_path()`` are valid
      - ``"open"`` → ``project_path()`` is valid, ``board()`` is None
      - ``""``     → user closed / cancelled (quit)
    """

    def __init__(self, parent: object = None) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        self.setWindowTitle("Protoboard Designer")
        self.setModal(True)
        self.setMinimumWidth(340)
        self.setWindowFlags(
            self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint
        )

        self._result_action: str = ""
        self._board: Board | None = None
        self._project_path: Path | None = None

        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(32, 32, 32, 32)

        title = QLabel("Protoboard Designer")
        font = QFont()
        font.setPointSize(16)
        font.setBold(True)
        title.setFont(font)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        subtitle = QLabel("Connectivity-aware perfboard & stripboard layout tool")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setStyleSheet("color: #666;")
        layout.addWidget(subtitle)

        layout.addSpacing(8)

        new_btn = QPushButton("New Project…")
        new_btn.setFixedHeight(40)
        new_btn.setDefault(True)
        new_btn.clicked.connect(self._new_project)
        layout.addWidget(new_btn)

        open_btn = QPushButton("Open Project…")
        open_btn.setFixedHeight(40)
        open_btn.clicked.connect(self._open_project)
        layout.addWidget(open_btn)

        layout.addSpacing(4)

        quit_btn = QPushButton("Quit")
        quit_btn.clicked.connect(self.reject)
        layout.addWidget(quit_btn)

    # ------------------------------------------------------------------
    # Result accessors
    # ------------------------------------------------------------------

    @property
    def result_action(self) -> str:
        return self._result_action

    def board(self) -> Board | None:
        return self._board

    def project_path(self) -> Path | None:
        return self._project_path

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _new_project(self) -> None:
        dlg = NewProjectDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._result_action = "new"
            self._board = dlg.board()
            self._project_path = dlg.project_path()
            self.accept()

    def _open_project(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            "Open project folder",
            str(Path.home()),
        )
        if folder:
            self._result_action = "open"
            self._project_path = Path(folder)
            self.accept()
