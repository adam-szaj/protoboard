# SPDX-License-Identifier: MIT
"""SelectionFilterWidget — compact status-bar widget to filter what SelectTool selects."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from protoboard.controller.selection_axes import SelectionFilter
from protoboard.services.debug_trace import traced


class SelectionFilterWidget(QWidget):
    """Three checkable buttons (Comps / Wires / Groups) embedded in the status bar."""

    # Emitted whenever the user toggles a button.  Payload is the new
    # SelectionFilter — listeners (e.g. MainWindow) use it to persist to
    # app_settings.
    filter_changed = Signal(object)

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

        self._btn_comps.clicked.connect(self._sync_to_tool)
        self._btn_wires.clicked.connect(self._sync_to_tool)
        self._btn_groups.clicked.connect(self._sync_to_tool)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    @traced()
    def set_tool(self, tool: object) -> None:
        """Bind to a SelectTool instance and mirror its current filter state."""
        self._tool = tool
        from protoboard.gui.tools.gui_tools import SelectTool
        if isinstance(tool, SelectTool):
            f = tool.filter
            self._btn_comps.setChecked(bool(f & SelectionFilter.COMPONENTS))
            self._btn_wires.setChecked(bool(f & SelectionFilter.WIRES))
            self._btn_groups.setChecked(bool(f & SelectionFilter.GROUPS))

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    @staticmethod
    @traced()
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

    @traced()
    def _sync_to_tool(self) -> None:
        f = SelectionFilter.NONE
        if self._btn_comps.isChecked():
            f |= SelectionFilter.COMPONENTS
        if self._btn_wires.isChecked():
            f |= SelectionFilter.WIRES
        if self._btn_groups.isChecked():
            f |= SelectionFilter.GROUPS
        from protoboard.gui.tools.gui_tools import SelectTool
        if isinstance(self._tool, SelectTool):
            self._tool.set_filter(f)
        self.filter_changed.emit(f)
