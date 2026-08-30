# SPDX-License-Identifier: MIT
"""Dialog shown when a wire would connect two pins that belong to different nets."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
)

if TYPE_CHECKING:
    from protoboard.domain.schematic.net import Net


class NetMergeDialog(QDialog):
    """Asks the user to confirm and name a net merge.

    Returns via exec():
      - QDialog.DialogCode.Accepted → user confirmed; use chosen_name
      - QDialog.DialogCode.Rejected → user cancelled; do not commit the wire
    """

    def __init__(
        self,
        net_a: Net,
        net_b: Net,
        parent: object = None,
    ) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        self.setWindowTitle("Merge Nets")
        self.setMinimumWidth(380)
        self.setModal(True)

        self._chosen_name: str = net_a.name

        layout = QVBoxLayout(self)

        msg = QLabel(
            f"This wire connects net <b>{net_a.name}</b> "
            f"with net <b>{net_b.name}</b>.<br>"
            f"Merge them into one net?"
        )
        msg.setWordWrap(True)
        msg.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(msg)

        layout.addWidget(QLabel("Merged net name:"))
        self._combo = QComboBox()
        self._combo.addItems([net_a.name, net_b.name])
        self._combo.setEditable(True)
        self._combo.currentTextChanged.connect(self._on_name_changed)
        layout.addWidget(self._combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_name_changed(self, text: str) -> None:
        self._chosen_name = text

    @property
    def chosen_name(self) -> str:
        return self._chosen_name
