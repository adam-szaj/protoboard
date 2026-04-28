# SPDX-License-Identifier: MIT
"""QGraphicsScene renderer for the board (pads and strips)."""

from __future__ import annotations

from PySide6.QtCore import QRectF
from PySide6.QtGui import QBrush, QColor, QPen
from PySide6.QtWidgets import QGraphicsEllipseItem, QGraphicsScene

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import PadAtom, StripAtom

# Visual constants (scene units = raster units)
PAD_RADIUS = 0.35
STRIP_RADIUS = 0.30
PAD_COLOR = QColor("#C8A030")  # gold
STRIP_COLOR = QColor("#C8A030")
HOLE_COLOR = QColor("#1A1A1A")
BOARD_BG = QColor("#2B6B3A")  # PCB green


def render_board(scene: QGraphicsScene, board: Board) -> None:
    """Add board pad/strip items to the scene (does not clear first)."""
    for atom, pos in board.atoms():
        x, y = pos.x, pos.y
        if isinstance(atom, PadAtom):
            _add_pad(scene, x, y, PAD_RADIUS, PAD_COLOR)
        elif isinstance(atom, StripAtom):
            _add_pad(scene, x, y, STRIP_RADIUS, STRIP_COLOR)


def _add_pad(
    scene: QGraphicsScene, cx: float, cy: float, r: float, color: QColor
) -> None:
    ring = QGraphicsEllipseItem(QRectF(cx - r, cy - r, 2 * r, 2 * r))
    ring.setBrush(QBrush(color))
    ring.setPen(QPen(QColor("#000000"), 0.04))
    scene.addItem(ring)
    # Drill hole
    hr = r * 0.45
    hole = QGraphicsEllipseItem(QRectF(cx - hr, cy - hr, 2 * hr, 2 * hr))
    hole.setBrush(QBrush(HOLE_COLOR))
    hole.setPen(QPen(QColor("#000000"), 0))
    scene.addItem(hole)
