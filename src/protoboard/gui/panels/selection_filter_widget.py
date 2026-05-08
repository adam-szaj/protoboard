# SPDX-License-Identifier: MIT
"""SelectionFilterWidget — compact status-bar widget to filter what SelectTool selects."""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget


class SelectionFilterWidget(QWidget):
    """Three checkable buttons (Comps / Wires / Groups) embedded in the status bar."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 0, 2, 0)
        layout.setSpacing(3)
        layout.addWidget(QLabel("Filter:"))

        self._btn_comps = self._make_btn("Comps")
        self._btn_wires = self._make_btn("Wires")
        self._btn_groups = self._make_btn("Groups")
        layout.addWidget(self._btn_comps)
        layout.addWidget(self._btn_wires)
        layout.addWidget(self._btn_groups)

        self._tool: object = None

        self._btn_comps.clicked.connect(lambda: self._toggle("comps"))
        self._btn_wires.clicked.connect(lambda: self._toggle("wires"))
        self._btn_groups.clicked.connect(lambda: self._toggle("groups"))

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def set_tool(self, tool: object) -> None:
        """Bind to a SelectTool instance (called after session swap)."""
        self._tool = tool

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    @staticmethod
    def _make_btn(label: str) -> QPushButton:
        btn = QPushButton(label)
        btn.setCheckable(True)
        btn.setChecked(True)
        btn.setFixedHeight(20)
        btn.setMaximumWidth(58)
        btn.setStyleSheet(
            "QPushButton { font-size: 10px; border-radius: 3px; padding: 0 4px; }"
            "QPushButton:checked { background: #2e7d32; color: white; border: none; }"
            "QPushButton:!checked { background: #555; color: #aaa; border: none; }"
        )
        return btn

    def _toggle(self, which: str) -> None:
        from protoboard.gui.tools.select_tool import SelectTool
        if not isinstance(self._tool, SelectTool):
            return
        if which == "comps":
            self._tool.set_filter_components(self._btn_comps.isChecked())
        elif which == "wires":
            self._tool.set_filter_wires(self._btn_wires.isChecked())
        elif which == "groups":
            self._tool.set_filter_groups(self._btn_groups.isChecked())
