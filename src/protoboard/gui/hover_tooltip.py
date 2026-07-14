# SPDX-License-Identifier: MIT
"""Cursor-anchored tooltip for a component's hidden label (hover-reveal 2A)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QWidget


class HoverTooltip(QLabel):
    """A small floating label shown near the cursor while ``labels`` is hidden."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setStyleSheet(
            "background: rgba(30,30,30,220); color: #f0f0f0;"
            " padding: 2px 5px; border-radius: 3px;"
        )
        self.hide()

    def show_at(self, text: str, view_pos: tuple[int, int]) -> None:
        self.setText(text)
        self.adjustSize()
        x, y = view_pos
        self.move(x + 12, y + 12)
        self.show()
        self.raise_()
