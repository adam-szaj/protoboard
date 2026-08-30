# SPDX-License-Identifier: MIT
"""``QtHelpOverlay`` — Qt adapter of :class:`HelpOverlayPort` (qt-ports §2.5).

Owns the ``[?]`` ``QLabel`` parked over the board view's viewport (built
lazily on first show).  ToolHost renders the HTML and calls the port; this
class is the only code that knows the label exists.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtWidgets import QGraphicsView, QLabel

_MARGIN = 12


class QtHelpOverlay:
    """HelpOverlayPort over a QLabel floating on the view's viewport."""

    def __init__(self, view: QGraphicsView) -> None:
        self._view = view
        self._label: QLabel | None = None

    def show_help(self, html: str) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QLabel

        label = self._label
        if label is None:
            label = QLabel(self._view.viewport())
            label.setTextFormat(Qt.TextFormat.RichText)
            label.setStyleSheet(
                "QLabel { background: rgba(20,22,28,235); color: #E6E6E6; "
                "border: 1px solid #4A5060; border-radius: 6px; padding: 10px; }"
            )
            self._label = label
        label.setText(html)
        label.adjustSize()
        label.move(self._view.viewport().width() - label.width() - _MARGIN, _MARGIN)
        label.show()
        label.raise_()

    def hide_help(self) -> None:
        if self._label is not None:
            self._label.hide()

    @property
    def help_visible(self) -> bool:
        # Track shown-state via ``isHidden()`` (False once ``show()`` is
        # called) rather than ``isVisible()`` — the latter is False in a
        # headless test whose parent viewport is never mapped.
        return self._label is not None and not self._label.isHidden()
