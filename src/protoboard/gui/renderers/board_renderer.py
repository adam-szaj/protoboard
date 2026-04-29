# SPDX-License-Identifier: MIT
"""QGraphicsScene renderer for the board (pads, strips, strip cuts, box frames)."""

from __future__ import annotations

from PySide6.QtCore import QRectF
from PySide6.QtGui import QBrush, QColor, QPen
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsLineItem,
    QGraphicsRectItem,
    QGraphicsScene,
)

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import PadAtom, StripAtom
from protoboard.domain.board.layout_engine import LayoutNode
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
_FRAME_PEN = QPen(QColor("#FFFFFF"), 0.05)


def render_board(
    scene: QGraphicsScene,
    board: Board,
    *,
    show_frames: bool = True,
) -> None:
    """Add board pad/strip items to the scene (does not clear first).

    When *show_frames* is True (default) the root box outline is drawn as a
    dashed white rectangle.
    """
    if show_frames:
        _render_frames(scene, board.layout, is_root=True)
    for atom, pos in board.atoms():
        x, y = pos.x, pos.y
        if isinstance(atom, PadAtom):
            _add_pad(scene, x, y, PAD_RADIUS, PAD_COLOR)
        elif isinstance(atom, StripAtom):
            _render_strip_pads(scene, atom, pos)


def render_cuts(
    scene: QGraphicsScene,
    cuts: list[StripCut],
    board: Board,
) -> None:
    """Render strip cuts as red X marks over the corresponding strip pads."""
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
        if atom.orientation == "horizontal":
            length = atom.length if isinstance(atom.length, int) else 1
            x = center.x - length / 2.0 + cut.position - 0.5
            y = center.y
        else:
            length = atom.length if isinstance(atom.length, int) else 1
            x = center.x
            y = center.y - length / 2.0 + cut.position - 0.5
        _add_cut_mark(scene, x, y, r)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _render_strip_pads(
    scene: QGraphicsScene, atom: StripAtom, center: Point
) -> None:
    """Draw one pad dot per hole of a strip atom."""
    length = atom.length if isinstance(atom.length, int) else 1
    if atom.orientation == "horizontal":
        # Strip spans horizontally: pads at (center.x - length/2 + 0.5 + i, center.y)
        start_x = center.x - length / 2.0 + 0.5
        for i in range(length):
            _add_pad(scene, start_x + i, center.y, STRIP_RADIUS, STRIP_COLOR)
    else:
        # Vertical strip
        start_y = center.y - length / 2.0 + 0.5
        for i in range(length):
            _add_pad(scene, center.x, start_y + i, STRIP_RADIUS, STRIP_COLOR)


def _render_frames(
    scene: QGraphicsScene, node: LayoutNode, *, is_root: bool
) -> None:
    """Recursively draw dashed outlines for container boxes.

    Only the root box gets a frame by default; inner containers are skipped
    to avoid visual noise.
    """
    from protoboard.domain.board.boxes import BoardGrid, HBox, VBox

    if is_root or isinstance(node.box, (HBox, VBox, BoardGrid)):
        half_w = node.width / 2.0
        half_h = node.height / 2.0
        rect = QGraphicsRectItem(
            QRectF(
                node.x - half_w,
                node.y - half_h,
                node.width,
                node.height,
            )
        )
        rect.setBrush(QBrush(QColor(0, 0, 0, 0)))  # transparent fill
        if is_root:
            pen = QPen(QColor("#FFFFFF"), 0.06)
        else:
            pen = QPen(QColor(255, 255, 255, 60), 0.04)
        rect.setPen(pen)
        scene.addItem(rect)

    # Only recurse into children of container nodes (not atoms).
    for child in node.children:
        _render_frames(scene, child, is_root=False)


def _add_pad(
    scene: QGraphicsScene, cx: float, cy: float, r: float, color: QColor
) -> None:
    ring = QGraphicsEllipseItem(QRectF(cx - r, cy - r, 2 * r, 2 * r))
    ring.setBrush(QBrush(color))
    ring.setPen(QPen(QColor("#000000"), 0.04))
    scene.addItem(ring)
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
