# SPDX-License-Identifier: MIT
"""DragAskRemoveDialog — Yes/Cancel confirmation for soft-conflict wire removal.

Shown at drag-commit for ``ask-remove`` preference categories:

* ``drag.free_wire_conflict`` — a free wire (no owning group) lies on a
  drag-set pin's new pad position, or a drag-set wire crosses an alien pad.
* ``drag.stationary_group_wire_conflict`` — a wire owned by a stationary
  group is in conflict; modifying it crosses group boundaries.

Two actions:

* **Yes** — drop the wires and commit the drag.  Default action (Enter).
* **Cancel** — refuse the commit, return to floating state.  Esc.

Plus session-level opt-out checkbox: the chosen action becomes the default
for the same category for the rest of the app run (cached in
``DragSessionOverrides``).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

__all__ = ["DragAskRemoveDialog"]


class DragAskRemoveDialog(QDialog):
    """Modal Yes/Cancel + 'Don't ask again for this session' checkbox.

    `accept()` (Yes) and `reject()` (Cancel) follow the standard
    ``QDialog`` exit codes — callers can use ``exec() == Accepted``.
    """

    def __init__(
        self,
        title: str,
        message: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(380)
        self.setModal(True)
        self._opt_out: bool = False

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        msg_label = QLabel(message)
        msg_label.setWordWrap(True)
        layout.addWidget(msg_label)

        self._opt_out_checkbox = QCheckBox("Don't ask again for this session")
        layout.addWidget(self._opt_out_checkbox)

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        yes_btn = QPushButton("Yes")
        yes_btn.setDefault(True)
        yes_btn.setShortcut(Qt.Key.Key_Return)
        yes_btn.clicked.connect(self._on_yes)
        btn_row.addWidget(yes_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setShortcut(Qt.Key.Key_Escape)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        layout.addLayout(btn_row)

    def session_opt_out(self) -> bool:
        """True iff the user ticked 'Don't ask again for this session'."""
        return self._opt_out

    def _on_yes(self) -> None:
        self._opt_out = self._opt_out_checkbox.isChecked()
        self.accept()
