# SPDX-License-Identifier: MIT
"""Hover-reveal: label tooltip (2A) and pin-name reveal (2B)."""

from __future__ import annotations

from protoboard.gui.hover_tooltip import HoverTooltip


def test_tooltip_shows_and_hides(qtbot) -> None:
    from PySide6.QtWidgets import QWidget

    parent = QWidget()
    qtbot.addWidget(parent)
    parent.show()
    tip = HoverTooltip(parent)
    tip.show_at("R5 4.7k", (10, 20))
    assert tip.isVisible()
    assert "R5 4.7k" in tip.text()
    tip.hide()
    assert not tip.isVisible()
