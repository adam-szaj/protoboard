# SPDX-License-Identifier: MIT
"""Dialog for creating a new project — choose board type and dimensions."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from protoboard.domain.board.board import Board
from protoboard.domain.board.templates import (
    make_breadboard,
    make_perfboard,
    make_stripboard,
)

_BOARD_TYPES = ["Perfboard", "Stripboard", "Breadboard"]

_DESCRIPTIONS = {
    "Perfboard": (
        "Regular grid of plated-through holes."
        " Connections made with wire on top or bottom."
    ),
    "Stripboard": (
        "Horizontal copper strips on bottom layer."
        " Cut strips to isolate sections of a net."
    ),
    "Breadboard": (
        "Solderless breadboard layout."
        " 2 horizontal power rails (top + bottom),"
        " 2 terminal blocks of 5-row × N-column vertical strips"
        " with a centre DIP gap."
    ),
}

# (rows, cols) or (None, cols) for breadboard
_DEFAULTS: dict[str, tuple[int | None, int]] = {
    "Perfboard": (20, 30),
    "Stripboard": (20, 30),
    "Breadboard": (None, 30),
}

# Which board types use rows (vs just cols)
_HAS_ROWS = {"Perfboard", "Stripboard"}


class NewProjectDialog(QDialog):
    """Modal dialog that collects board type, size, and project folder path."""

    def __init__(self, parent: object = None) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        self.setWindowTitle("New Project")
        self.setMinimumWidth(440)
        self.setModal(True)

        outer = QVBoxLayout(self)
        outer.setSpacing(12)

        # --- Board type ---
        board_group = QGroupBox("Board type")
        board_form = QFormLayout(board_group)

        self._type_combo = QComboBox()
        self._type_combo.addItems(_BOARD_TYPES)
        board_form.addRow("Type:", self._type_combo)

        self._desc_label = QLabel()
        self._desc_label.setWordWrap(True)
        self._desc_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._desc_label.setStyleSheet("color: #666;")
        board_form.addRow("", self._desc_label)

        outer.addWidget(board_group)

        # --- Dimensions ---
        size_group = QGroupBox("Dimensions")
        size_form = QFormLayout(size_group)

        self._rows_spin = QSpinBox()
        self._rows_spin.setRange(2, 200)
        self._rows_label = QLabel("Rows:")
        size_form.addRow(self._rows_label, self._rows_spin)

        self._cols_spin = QSpinBox()
        self._cols_spin.setRange(2, 200)
        size_form.addRow("Columns:", self._cols_spin)

        outer.addWidget(size_group)

        # --- Project folder ---
        folder_group = QGroupBox("Project folder")
        folder_layout = QVBoxLayout(folder_group)

        path_row = QHBoxLayout()
        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText("Choose an empty folder…")
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse)
        path_row.addWidget(self._path_edit)
        path_row.addWidget(browse_btn)
        folder_layout.addLayout(path_row)

        self._path_warning = QLabel("")
        self._path_warning.setStyleSheet("color: #c00;")
        folder_layout.addWidget(self._path_warning)

        outer.addWidget(folder_group)

        # --- Buttons ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        # Wire up signals.
        self._type_combo.currentTextChanged.connect(self._on_type_changed)

        # Initialise state.
        self._on_type_changed(self._type_combo.currentText())

    # ------------------------------------------------------------------
    # Result accessors (valid only after Accepted)
    # ------------------------------------------------------------------

    def board(self) -> Board:
        board_type = self._type_combo.currentText()
        rows = self._rows_spin.value()
        cols = self._cols_spin.value()
        if board_type == "Perfboard":
            return make_perfboard(rows, cols)
        if board_type == "Breadboard":
            return make_breadboard(cols)
        return make_stripboard(rows, cols)

    def project_path(self) -> Path:
        return Path(self._path_edit.text().strip())

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _on_type_changed(self, text: str) -> None:
        self._desc_label.setText(_DESCRIPTIONS.get(text, ""))
        rows, cols = _DEFAULTS.get(text, (20, 30))
        show_rows = text in _HAS_ROWS
        self._rows_spin.setVisible(show_rows)
        self._rows_label.setVisible(show_rows)
        if rows is not None:
            self._rows_spin.setValue(rows)
        self._cols_spin.setValue(cols)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            "Choose project folder",
            str(Path.home()),
        )
        if folder:
            self._path_edit.setText(folder)

    def _on_accept(self) -> None:
        path_str = self._path_edit.text().strip()
        if not path_str:
            self._path_warning.setText("Please choose a project folder.")
            return
        path = Path(path_str)
        if path.exists() and any(path.glob("board.yaml")):
            self._path_warning.setText(
                "This folder already contains a project. Choose an empty folder."
            )
            return
        self._path_warning.setText("")
        self.accept()
