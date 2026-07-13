# SPDX-License-Identifier: MIT
"""HighlightModeWidget — status-bar dropdown to select the relation class."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QToolButton, QWidget

from protoboard.controller.selection_axes import RELATION_LABELS, RelationClass


class HighlightModeWidget(QWidget):
    """Compact status-bar widget showing the current relation class with a dropdown."""

    # Emitted after a user-driven menu pick.  Payload is the new RelationClass
    # — MainWindow listens to persist it.
    relation_changed = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 0, 2, 0)
        layout.setSpacing(3)
        layout.addWidget(QLabel("HL:"))

        self._btn = QToolButton(self)
        self._btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._btn.setFixedHeight(20)
        self._btn.setMinimumWidth(120)
        self._btn.setStyleSheet(
            "QToolButton { font-size: 10px; border-radius: 3px; padding: 0 4px;"
            " background: #2e5fa3; color: white; border: none; }"
            "QToolButton::menu-indicator { image: none; }"
        )
        layout.addWidget(self._btn)

        self._tool: object = None
        self._menu = QMenu(self)
        self._btn.setMenu(self._menu)
        self._rebuild_menu()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def set_tool(self, tool: object) -> None:
        self._tool = tool
        # Reflect the tool's current relation in the label.
        from protoboard.gui.tools.gui_tools import SelectTool
        if isinstance(tool, SelectTool):
            self._btn.setText(RELATION_LABELS.get(tool.relation, tool.relation.name) + " ▾")
        self._rebuild_menu()

    def on_highlight_mode_changed(self, label: str) -> None:
        self._btn.setText(label + " ▾")

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _rebuild_menu(self) -> None:
        self._menu.clear()
        for relation in RelationClass:
            label = RELATION_LABELS.get(relation, relation.name)
            action = self._menu.addAction(label)
            action.triggered.connect(
                lambda checked=False, r=relation: self._select_relation(r)
            )
        if not self._btn.text():
            self._btn.setText(
                RELATION_LABELS[RelationClass.SAME_LOGICAL_NET] + " ▾"
            )

    def _select_relation(self, relation: RelationClass) -> None:
        from protoboard.gui.tools.gui_tools import SelectTool
        if isinstance(self._tool, SelectTool):
            self._tool.set_relation(relation)
        self._btn.setText(RELATION_LABELS.get(relation, relation.name) + " ▾")
        self.relation_changed.emit(relation)
