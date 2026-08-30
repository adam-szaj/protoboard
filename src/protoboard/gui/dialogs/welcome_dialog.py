# SPDX-License-Identifier: MIT
"""Welcome screen shown at startup — New Project, Open Project, Recent Projects."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from protoboard.domain.board.board import Board
from protoboard.gui import recent_projects
from protoboard.gui.dialogs.new_project_dialog import NewProjectDialog


class WelcomeDialog(QDialog):
    """Startup screen.

    After ``exec()``, inspect ``result_action`` to see what the user chose:
      - ``"new"``  → ``board()`` and ``project_path()`` are valid
      - ``"open"`` → ``project_path()`` is valid, ``board()`` is None
      - ``""``     → user closed / cancelled (quit)
    """

    def __init__(self, parent: object = None, default_dir: Path | None = None) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        self._default_dir = default_dir
        self.setWindowTitle("Protoboard Designer")
        self.setModal(True)
        self.setMinimumWidth(420)
        self.setWindowFlags(
            self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint
        )

        self._result_action: str = ""
        self._board: Board | None = None
        self._project_path: Path | None = None

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
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

        # ── Recent Projects ──────────────────────────────────────────────
        recents = recent_projects.load()
        if recents:
            sep = QFrame()
            sep.setFrameShape(QFrame.Shape.HLine)
            sep.setStyleSheet("color: #444;")
            layout.addWidget(sep)

            recent_label = QLabel("Recent Projects")
            recent_label.setStyleSheet("color: #888; font-size: 11px;")
            layout.addWidget(recent_label)

            self._recent_list = QListWidget()
            self._recent_list.setMaximumHeight(min(len(recents), 6) * 28 + 4)
            self._recent_list.setAlternatingRowColors(True)
            for path in recents:
                item = QListWidgetItem(path.name)
                item.setToolTip(str(path))
                item.setData(Qt.ItemDataRole.UserRole, path)
                self._recent_list.addItem(item)
            self._recent_list.itemDoubleClicked.connect(self._open_recent)
            self._recent_list.itemClicked.connect(self._on_recent_selected)
            layout.addWidget(self._recent_list)

            self._open_recent_btn = QPushButton("Open Selected")
            self._open_recent_btn.setEnabled(False)
            self._open_recent_btn.clicked.connect(self._open_recent_selected)
            layout.addWidget(self._open_recent_btn)
        else:
            self._recent_list = None  # type: ignore[assignment]
            self._open_recent_btn = None  # type: ignore[assignment]

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
        dlg = NewProjectDialog(self, default_dir=self._default_dir)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._result_action = "new"
            self._board = dlg.board()
            self._project_path = dlg.project_path()
            self.accept()

    def _open_project(self) -> None:
        start = str(self._default_dir) if self._default_dir else str(Path.home())
        folder = QFileDialog.getExistingDirectory(
            self,
            "Open project folder",
            start,
        )
        if folder:
            self._result_action = "open"
            self._project_path = Path(folder)
            self.accept()

    def _on_recent_selected(self, item: QListWidgetItem) -> None:
        if self._open_recent_btn is not None:
            self._open_recent_btn.setEnabled(True)

    def _open_recent(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(path, Path) and path.exists():
            self._result_action = "open"
            self._project_path = path
            self.accept()

    def _open_recent_selected(self) -> None:
        if self._recent_list is None:
            return
        item = self._recent_list.currentItem()
        if item is not None:
            self._open_recent(item)
