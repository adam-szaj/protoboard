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
from protoboard.gui.layer_state import BOARD_GHOST_ALPHA, Z_BOARD, LayerState
from protoboard.gui.theme import DEFAULT_THEME, Theme

# Re-export BOARD_BG so board_view.py can import it without knowing about theme.
BOARD_BG = QColor(DEFAULT_THEME.board_bg)

_DEFAULT_LAYERS = LayerState()


def _apply_alpha(color: QColor, alpha: float) -> QColor:
    c = QColor(color)
    c.setAlphaF(alpha)
    return c


def render_board(
    scene: QGraphicsScene,
    board: Board,
    *,
    show_frames: bool = True,
    theme: Theme = DEFAULT_THEME,
    layers: LayerState = _DEFAULT_LAYERS,
) -> None:
    """Add board pad/strip items to the scene (does not clear first).

    When the active tool ghosts components (routing / placement) the board is
    also rendered with reduced alpha so back-side elements remain readable
    underneath it.
    """
    board_alpha = layers.alpha_for_board()
    if board_alpha == 0.0:
        return

    pad_color = _apply_alpha(QColor(theme.pad_color), board_alpha)
    strip_color = _apply_alpha(QColor(theme.strip_color), board_alpha)
    isolated_color = _apply_alpha(QColor(theme.isolated_color), board_alpha)

    if show_frames:
        _render_frames(scene, board.layout, is_root=True, theme=theme,
                       z=Z_BOARD, alpha=board_alpha)
    for atom, pos in board.atoms():
        if isinstance(atom, PadAtom):
            _add_pad(scene, pos.x, pos.y, theme.pad_radius, pad_color, theme,
                     z=Z_BOARD)
        elif isinstance(atom, StripAtom):
            _render_strip_pads(scene, atom, pos, strip_color, isolated_color,
                               theme, z=Z_BOARD)


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
    z: float = Z_BOARD,
) -> None:
    length = atom.length if isinstance(atom.length, int) else 1

    if atom.isolated:
        cx, cy = center.x, center.y
        _add_pad(scene, cx, cy, theme.strip_radius, isolated_color, theme, z=z)
        r = theme.strip_radius * 0.7
        pen = QPen(QColor(theme.isolated_x_color), theme.isolated_x_width)
        for dx, dy in ((r, r), (-r, r)):
            line = QGraphicsLineItem(cx - dx, cy - dy, cx + dx, cy + dy)
            line.setPen(pen)
            line.setZValue(z)
            scene.addItem(line)
        return

    track_h = theme.strip_radius * 1.1
    track_color = QColor(strip_color)
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
        track.setZValue(z - 0.1)
        scene.addItem(track)
        for i in range(length):
            _add_pad(scene, start_x + i, center.y, theme.strip_radius,
                     strip_color, theme, z=z)
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
        track.setZValue(z - 0.1)
        scene.addItem(track)
        for i in range(length):
            _add_pad(scene, center.x, start_y + i, theme.strip_radius,
                     strip_color, theme, z=z)


# ---------------------------------------------------------------------------
# Box frame outlines
# ---------------------------------------------------------------------------


def _render_frames(
    scene: QGraphicsScene, node: LayoutNode, *, is_root: bool, theme: Theme,
    z: float = Z_BOARD, alpha: float = 1.0,
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
            c = QColor(theme.board_frame_color)
            c.setAlphaF(alpha)
            pen = QPen(c, theme.board_frame_width)
        else:
            c = QColor(theme.board_sub_frame_color)
            c.setAlphaF(c.alphaF() * alpha)
            pen = QPen(c, theme.board_sub_frame_width)
            pen.setStyle(Qt.PenStyle.DashLine)
        rect.setPen(pen)
        rect.setZValue(z - 0.2)
        scene.addItem(rect)

    for child in node.children:
        _render_frames(scene, child, is_root=False, theme=theme, z=z, alpha=alpha)


# ---------------------------------------------------------------------------
# Pad helper
# ---------------------------------------------------------------------------


def _add_pad(
    scene: QGraphicsScene, cx: float, cy: float, r: float, color: QColor,
    theme: Theme, z: float = Z_BOARD,
) -> None:
    ring = QGraphicsEllipseItem(QRectF(cx - r, cy - r, 2 * r, 2 * r))
    ring.setBrush(QBrush(color))
    outline = QColor(theme.pad_outline_color)
    outline.setAlphaF(color.alphaF())
    ring.setPen(QPen(outline, theme.pad_outline_width))
    ring.setZValue(z)
    scene.addItem(ring)
    hr = r * 0.45
    hole = QGraphicsEllipseItem(QRectF(cx - hr, cy - hr, 2 * hr, 2 * hr))
    hole_color = QColor(theme.hole_color)
    hole_color.setAlphaF(color.alphaF())
    hole.setBrush(QBrush(hole_color))
    hole.setPen(QPen(outline, 0))
    hole.setZValue(z + 0.05)
    scene.addItem(hole)
