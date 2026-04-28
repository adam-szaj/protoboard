# SPDX-License-Identifier: MIT
"""Properties panel — selected component editor + DRC list."""

from __future__ import annotations

import uuid

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from protoboard.domain.components.instance import ComponentInstance
from protoboard.services.drc.engine import DRCIssue

_SEVERITY_COLORS = {
    "error": QColor("#CC2222"),
    "warning": QColor("#CC8800"),
    "info": QColor("#2255AA"),
}


class PropertiesPanel(QWidget):
    """Shows editable fields for the selected component and DRC issues."""

    component_changed = Signal(uuid.UUID, str, str, int)  # id, refdes, value, rotation

    def __init__(self, parent: object = None) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        # Component properties group.
        grp = QGroupBox("Component")
        form = QFormLayout(grp)
        self._refdes_edit = QLineEdit()
        self._value_edit = QLineEdit()
        self._rotation_combo = QComboBox()
        self._rotation_combo.addItems(["0°", "90°", "180°", "270°"])
        self._apply_btn = QPushButton("Apply")
        form.addRow("RefDes:", self._refdes_edit)
        form.addRow("Value:", self._value_edit)
        form.addRow("Rotation:", self._rotation_combo)
        form.addRow(self._apply_btn)
        layout.addWidget(grp)

        self._apply_btn.clicked.connect(self._on_apply)
        self._current_id: uuid.UUID | None = None

        # DRC group.
        drc_grp = QGroupBox("DRC")
        drc_layout = QVBoxLayout(drc_grp)
        self._drc_list = QListWidget()
        drc_layout.addWidget(self._drc_list)
        layout.addWidget(drc_grp)
        layout.addStretch()

    def show_component(self, inst: ComponentInstance) -> None:
        self._current_id = inst.id
        self._refdes_edit.setText(inst.refdes)
        self._value_edit.setText(inst.value)

    def clear_component(self) -> None:
        self._current_id = None
        self._refdes_edit.clear()
        self._value_edit.clear()

    def update_issues(self, issues: list[DRCIssue]) -> None:
        self._drc_list.clear()
        if not issues:
            item = QListWidgetItem("✓ No issues")
            item.setForeground(QColor("#228822"))
            self._drc_list.addItem(item)
            return
        for issue in issues:
            item = QListWidgetItem(f"[{issue.code}] {issue.message}")
            item.setForeground(_SEVERITY_COLORS.get(issue.severity, QColor()))
            self._drc_list.addItem(item)

    def _on_apply(self) -> None:
        if self._current_id is None:
            return
        rotation = self._rotation_combo.currentIndex() * 90
        self.component_changed.emit(
            self._current_id,
            self._refdes_edit.text(),
            self._value_edit.text(),
            rotation,
        )


class DRCPanel(QWidget):
    """Standalone DRC panel (kept for backward compat with session.drc_changed)."""

    def __init__(self, parent: object = None) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(QLabel("DRC"))
        self._list = QListWidget()
        layout.addWidget(self._list)

    def update_issues(self, issues: list[DRCIssue]) -> None:
        self._list.clear()
        if not issues:
            item = QListWidgetItem("✓ No issues")
            item.setForeground(QColor("#228822"))
            self._list.addItem(item)
            return
        for issue in issues:
            item = QListWidgetItem(f"[{issue.code}] {issue.message}")
            item.setForeground(_SEVERITY_COLORS.get(issue.severity, QColor()))
            self._list.addItem(item)
