# SPDX-License-Identifier: MIT
"""HighlightModeWidget — status-bar dropdown to select the interaction mode."""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QToolButton, QWidget


class HighlightModeWidget(QWidget):
    """Compact status-bar widget showing current interaction mode with a dropdown."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 0, 2, 0)
        layout.setSpacing(3)
        layout.addWidget(QLabel("HL:"))

        self._btn = QToolButton(self)
        self._btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._btn.setFixedHeight(20)
        self._btn.setMinimumWidth(80)
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
        self._rebuild_menu()

    def on_highlight_mode_changed(self, label: str) -> None:
        self._btn.setText(label + " ▾")

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _rebuild_menu(self) -> None:
        from protoboard.gui.interaction_mode import INTERACTION_MODES
        self._menu.clear()
        for mode in INTERACTION_MODES.values():
            action = self._menu.addAction(mode.label)
            if mode.description:
                action.setToolTip(mode.description)
            action.triggered.connect(lambda checked=False, m=mode: self._select_mode(m))
        # Set initial label.
        from protoboard.gui.interaction_mode import INTERACTION_MODES
        first = next(iter(INTERACTION_MODES.values()))
        self._btn.setText(first.label + " ▾")

    def _select_mode(self, mode: object) -> None:
        from protoboard.gui.tools.select_tool import SelectTool
        if isinstance(self._tool, SelectTool):
            self._tool.set_interaction_mode(mode)
        from protoboard.gui.interaction_mode import InteractionMode
        if isinstance(mode, InteractionMode):
            self._btn.setText(mode.label + " ▾")
