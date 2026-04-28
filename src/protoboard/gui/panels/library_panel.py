# SPDX-License-Identifier: MIT
"""Library panel — browse and select components for placement."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from protoboard.services.library.loader import ComponentLibrary


class LibraryPanel(QWidget):
    component_selected = Signal(str)  # emits definition_ref name

    def __init__(self, library: ComponentLibrary, parent: object = None) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        self._library = library
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(QLabel("Components"))
        self._list = QListWidget()
        for name in sorted(library.names()):
            item = QListWidgetItem(name)
            self._list.addItem(item)
        self._list.itemDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self._list)

    def _on_double_click(self, item: QListWidgetItem) -> None:
        self.component_selected.emit(item.text())
