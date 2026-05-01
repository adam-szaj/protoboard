# SPDX-License-Identifier: MIT
"""QGraphicsScene renderer for the board (pads, strips, box frames)."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
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
from protoboard.gui.theme import DEFAULT_THEME, Theme

# Re-export BOARD_BG so board_view.py can import it without knowing about theme.
BOARD_BG = QColor(DEFAULT_THEME.board_bg)


def render_board(
    scene: QGraphicsScene,
    board: Board,
    *,
    show_frames: bool = True,
    theme: Theme = DEFAULT_THEME,
) -> None:
    """Add board pad/strip items to the scene (does not clear first)."""
    if show_frames:
        _render_frames(scene, board.layout, is_root=True, theme=theme)
    pad_color = QColor(theme.pad_color)
    strip_color = QColor(theme.strip_color)
    isolated_color = QColor(theme.isolated_color)
    for atom, pos in board.atoms():
        if isinstance(atom, PadAtom):
            _add_pad(scene, pos.x, pos.y, theme.pad_radius, pad_color, theme)
        elif isinstance(atom, StripAtom):
            _render_strip_pads(scene, atom, pos, strip_color, isolated_color, theme)


# ---------------------------------------------------------------------------
# Strip rendering
# ---------------------------------------------------------------------------


def _render_strip_pads(
    scene: QGraphicsScene,
    atom: StripAtom,
    center: Point,
    strip_color: QColor,
    isolated_color: QColor,
    theme: Theme,
) -> None:
    length = atom.length if isinstance(atom.length, int) else 1

    if atom.isolated:
        cx, cy = center.x, center.y
        _add_pad(scene, cx, cy, theme.strip_radius, isolated_color, theme)
        r = theme.strip_radius * 0.7
        pen = QPen(QColor(theme.isolated_x_color), theme.isolated_x_width)
        for dx, dy in ((r, r), (-r, r)):
            line = QGraphicsLineItem(cx - dx, cy - dy, cx + dx, cy + dy)
            line.setPen(pen)
            scene.addItem(line)
        return

    track_h = theme.strip_radius * 1.1
    track_color = QColor(theme.strip_track_color)
    track_pen = QPen(QColor(theme.strip_track_outline), theme.strip_track_outline_width)
    if atom.orientation == "horizontal":
        start_x = center.x - length / 2.0 + 0.5
        end_x = start_x + length - 1
        track = QGraphicsRectItem(
            QRectF(start_x - theme.strip_radius, center.y - track_h,
                   (end_x - start_x) + 2 * theme.strip_radius, 2 * track_h)
        )
        track.setBrush(QBrush(track_color))
        track.setPen(track_pen)
        scene.addItem(track)
        for i in range(length):
            _add_pad(scene, start_x + i, center.y, theme.strip_radius, strip_color, theme)
    else:
        start_y = center.y - length / 2.0 + 0.5
        end_y = start_y + length - 1
        track_w = track_h
        track = QGraphicsRectItem(
            QRectF(center.x - track_w, start_y - theme.strip_radius,
                   2 * track_w, (end_y - start_y) + 2 * theme.strip_radius)
        )
        track.setBrush(QBrush(track_color))
        track.setPen(track_pen)
        scene.addItem(track)
        for i in range(length):
            _add_pad(scene, center.x, start_y + i, theme.strip_radius, strip_color, theme)


# ---------------------------------------------------------------------------
# Box frame outlines
# ---------------------------------------------------------------------------


def _render_frames(
    scene: QGraphicsScene, node: LayoutNode, *, is_root: bool, theme: Theme
) -> None:
    from protoboard.domain.board.boxes import BoardGrid, HBox, VBox

    if is_root or isinstance(node.box, (HBox, VBox, BoardGrid)):
        half_w = node.width / 2.0
        half_h = node.height / 2.0
        rect = QGraphicsRectItem(
            QRectF(node.x - half_w, node.y - half_h, node.width, node.height)
        )
        rect.setBrush(QBrush(QColor(0, 0, 0, 0)))
        if is_root:
            pen = QPen(QColor(theme.board_frame_color), theme.board_frame_width)
        else:
            c = QColor(theme.board_sub_frame_color)
            pen = QPen(c, theme.board_sub_frame_width)
            pen.setStyle(Qt.PenStyle.DashLine)
        rect.setPen(pen)
        scene.addItem(rect)

    for child in node.children:
        _render_frames(scene, child, is_root=False, theme=theme)


# ---------------------------------------------------------------------------
# Pad helper
# ---------------------------------------------------------------------------


def _add_pad(
    scene: QGraphicsScene, cx: float, cy: float, r: float, color: QColor,
    theme: Theme,
) -> None:
    ring = QGraphicsEllipseItem(QRectF(cx - r, cy - r, 2 * r, 2 * r))
    ring.setBrush(QBrush(color))
    ring.setPen(QPen(QColor(theme.pad_outline_color), theme.pad_outline_width))
    scene.addItem(ring)
    hr = r * 0.45
    hole = QGraphicsEllipseItem(QRectF(cx - hr, cy - hr, 2 * hr, 2 * hr))
    hole.setBrush(QBrush(QColor(theme.hole_color)))
    hole.setPen(QPen(QColor(theme.pad_outline_color), 0))
    scene.addItem(hole)
