# SPDX-License-Identifier: MIT
"""Select tool — click to select component or wire."""

from __future__ import annotations

from PySide6.QtCore import QPointF
from PySide6.QtGui import QMouseEvent

from protoboard.gui.board_view import _Tool


class SelectTool(_Tool):
    """No-op for MVP — just shows cursor."""

    def on_press(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        pass
