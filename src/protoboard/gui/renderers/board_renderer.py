# SPDX-License-Identifier: MIT
"""QGraphicsScene renderer for the board (pads, strips, strip cuts)."""

from __future__ import annotations

from PySide6.QtCore import QRectF
from PySide6.QtGui import QBrush, QColor, QPen
from PySide6.QtWidgets import QGraphicsEllipseItem, QGraphicsLineItem, QGraphicsScene

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import PadAtom, StripAtom
from protoboard.domain.layout.cut import StripCut
from protoboard.geometry import Point

# Visual constants (scene units = raster units)
PAD_RADIUS = 0.35
STRIP_RADIUS = 0.30
PAD_COLOR = QColor("#C8A030")  # gold
STRIP_COLOR = QColor("#C8A030")
HOLE_COLOR = QColor("#1A1A1A")
BOARD_BG = QColor("#2B6B3A")  # PCB green
_CUT_COLOR = QColor("#FF3030")
_CUT_PEN = QPen(_CUT_COLOR, 0.10)


def render_board(scene: QGraphicsScene, board: Board) -> None:
    """Add board pad/strip items to the scene (does not clear first)."""
    for atom, pos in board.atoms():
        x, y = pos.x, pos.y
        if isinstance(atom, PadAtom):
            _add_pad(scene, x, y, PAD_RADIUS, PAD_COLOR)
        elif isinstance(atom, StripAtom):
            _add_pad(scene, x, y, STRIP_RADIUS, STRIP_COLOR)


def render_cuts(
    scene: QGraphicsScene,
    cuts: list[StripCut],
    board: Board,
) -> None:
    """Render strip cuts as red X marks over the corresponding strip pads."""
    # Build a map from strip_atom_ref → list of (atom, pos) for lookup.
    strip_map: dict[str, tuple[StripAtom, Point]] = {}
    for atom, pos in board.atoms():
        if isinstance(atom, StripAtom):
            strip_map[str(id(atom))] = (atom, pos)

    r = STRIP_RADIUS * 0.7
    for cut in cuts:
        entry = strip_map.get(cut.strip_atom_ref)
        if entry is None:
            continue
        atom, center = entry
        # Compute world position of the cut pad.
        if atom.orientation == "horizontal":
            length = atom.length if isinstance(atom.length, int) else 1
            x = center.x - length / 2 + cut.position - 0.5
            y = center.y
        else:
            length = atom.length if isinstance(atom.length, int) else 1
            x = center.x
            y = center.y - length / 2 + cut.position - 0.5
        _add_cut_mark(scene, x, y, r)


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


def _add_cut_mark(scene: QGraphicsScene, cx: float, cy: float, r: float) -> None:
    """Draw a red X at (cx, cy) to indicate a strip cut."""
    for dx, dy in ((r, r), (-r, r)):
        line = QGraphicsLineItem(cx - dx, cy - dy, cx + dx, cy + dy)
        line.setPen(_CUT_PEN)
        scene.addItem(line)
