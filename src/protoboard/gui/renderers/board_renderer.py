# SPDX-License-Identifier: MIT
"""QGraphicsScene renderer for the board (pads, strips, box frames)."""

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
from protoboard.geometry import Point

# Visual constants (scene units = raster units)
PAD_RADIUS = 0.35
STRIP_RADIUS = 0.30
PAD_COLOR = QColor("#C8A030")        # gold — active pad
STRIP_COLOR = QColor("#C8A030")      # gold — active strip hole
STRIP_TRACK_COLOR = QColor("#A07020")  # darker gold — copper track between holes
ISOLATED_COLOR = QColor("#444444")   # dark grey — drilled/isolated pad
HOLE_COLOR = QColor("#1A1A1A")
BOARD_BG = QColor("#2B6B3A")         # PCB green


def render_board(
    scene: QGraphicsScene,
    board: Board,
    *,
    show_frames: bool = True,
) -> None:
    """Add board pad/strip items to the scene (does not clear first).

    When *show_frames* is True (default) the root box outline is drawn as a
    white rectangle.
    """
    if show_frames:
        _render_frames(scene, board.layout, is_root=True)
    for atom, pos in board.atoms():
        if isinstance(atom, PadAtom):
            _add_pad(scene, pos.x, pos.y, PAD_RADIUS, PAD_COLOR)
        elif isinstance(atom, StripAtom):
            _render_strip_pads(scene, atom, pos)


# ---------------------------------------------------------------------------
# Strip rendering
# ---------------------------------------------------------------------------


def _render_strip_pads(
    scene: QGraphicsScene, atom: StripAtom, center: Point
) -> None:
    """Draw a copper track + one pad per hole for a strip segment.

    Active segments get a filled copper-coloured rectangle connecting all
    holes, matching the look of a real stripboard. Isolated pads get a
    dark crossed-out dot with no track.
    """
    length = atom.length if isinstance(atom.length, int) else 1

    if atom.isolated:
        # Single drilled-out pad: grey dot with an X.
        cx, cy = center.x, center.y
        _add_pad(scene, cx, cy, STRIP_RADIUS, ISOLATED_COLOR)
        r = STRIP_RADIUS * 0.7
        pen = QPen(QColor("#FF4040"), 0.08)
        for dx, dy in ((r, r), (-r, r)):
            line = QGraphicsLineItem(cx - dx, cy - dy, cx + dx, cy + dy)
            line.setPen(pen)
            scene.addItem(line)
        return

    # Draw the copper track rectangle behind the pads.
    track_h = STRIP_RADIUS * 1.1  # track height (half = track_h)
    if atom.orientation == "horizontal":
        start_x = center.x - length / 2.0 + 0.5
        end_x = start_x + length - 1
        track = QGraphicsRectItem(
            QRectF(start_x - STRIP_RADIUS, center.y - track_h,
                   (end_x - start_x) + 2 * STRIP_RADIUS, 2 * track_h)
        )
        track.setBrush(QBrush(STRIP_TRACK_COLOR))
        track.setPen(QPen(QColor("#7A5010"), 0.03))
        scene.addItem(track)
        for i in range(length):
            _add_pad(scene, start_x + i, center.y, STRIP_RADIUS, STRIP_COLOR)
    else:
        start_y = center.y - length / 2.0 + 0.5
        end_y = start_y + length - 1
        track_w = track_h
        track = QGraphicsRectItem(
            QRectF(center.x - track_w, start_y - STRIP_RADIUS,
                   2 * track_w, (end_y - start_y) + 2 * STRIP_RADIUS)
        )
        track.setBrush(QBrush(STRIP_TRACK_COLOR))
        track.setPen(QPen(QColor("#7A5010"), 0.03))
        scene.addItem(track)
        for i in range(length):
            _add_pad(scene, center.x, start_y + i, STRIP_RADIUS, STRIP_COLOR)


# ---------------------------------------------------------------------------
# Box frame outlines
# ---------------------------------------------------------------------------


def _render_frames(
    scene: QGraphicsScene, node: LayoutNode, *, is_root: bool
) -> None:
    """Draw dashed outlines for container boxes; root gets a solid outline."""
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
        rect.setBrush(QBrush(QColor(0, 0, 0, 0)))
        if is_root:
            pen = QPen(QColor("#FFFFFF"), 0.06)
        else:
            pen = QPen(QColor(255, 255, 255, 60), 0.04)
        rect.setPen(pen)
        scene.addItem(rect)

    for child in node.children:
        _render_frames(scene, child, is_root=False)


# ---------------------------------------------------------------------------
# Pad helper
# ---------------------------------------------------------------------------


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
