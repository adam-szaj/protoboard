# SPDX-License-Identifier: MIT
"""Dialog for selecting a group target: new group or add to existing."""

from __future__ import annotations

import uuid
from typing import Literal

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from protoboard.domain.layout.group import ComponentGroup


class GroupTargetDialog(QDialog):
    """Ask the user whether to create a new group or add to an existing one.

    Call ``exec()`` and then read ``mode``, ``new_name``, ``target_group_id``.

    ``mode == "new"``      → create a new group named ``new_name``.
    ``mode == "existing"`` → add to the group with id ``target_group_id``.
    """

    def __init__(
        self,
        existing_groups: list[ComponentGroup],
        default_name: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Group selection")

        self.mode: Literal["new", "existing"] = "new"
        self.new_name: str = default_name
        self.target_group_id: uuid.UUID | None = None

        layout = QVBoxLayout(self)

        # --- Radio: new group ---
        self._radio_new = QRadioButton("Create new group")
        self._radio_new.setChecked(True)
        layout.addWidget(self._radio_new)

        form_new = QFormLayout()
        form_new.setContentsMargins(24, 0, 0, 0)
        self._name_edit = QLineEdit(default_name)
        form_new.addRow("Name:", self._name_edit)
        layout.addLayout(form_new)

        # --- Radio: existing group ---
        self._radio_existing = QRadioButton("Add to existing group")
        layout.addWidget(self._radio_existing)

        form_existing = QFormLayout()
        form_existing.setContentsMargins(24, 0, 0, 0)
        self._group_combo = QComboBox()
        for g in existing_groups:
            self._group_combo.addItem(g.name or f"(unnamed {str(g.id)[:8]})", userData=g.id)
        if not existing_groups:
            self._group_combo.setEnabled(False)
            self._radio_existing.setEnabled(False)
        form_existing.addRow("Group:", self._group_combo)
        layout.addLayout(form_existing)

        # Enable/disable widgets on radio change.
        self._radio_new.toggled.connect(self._update_state)
        self._update_state()

        # --- Buttons ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _update_state(self) -> None:
        is_new = self._radio_new.isChecked()
        self._name_edit.setEnabled(is_new)
        self._group_combo.setEnabled(not is_new and self._radio_existing.isEnabled())

    def _on_accept(self) -> None:
        if self._radio_new.isChecked():
            self.mode = "new"
            self.new_name = self._name_edit.text().strip() or self._name_edit.placeholderText()
        else:
            self.mode = "existing"
            self.target_group_id = self._group_combo.currentData()
        self.accept()
