# SPDX-License-Identifier: MIT
"""QGraphicsScene renderer for the board (pads, strips, box frames).

Two-tier API:

- ``build_board_display_list(board, theme, layers)`` returns a pure
  data structure (``BoardDisplayList``) describing every primitive the
  board layer would draw — pads, holes, strip tracks, box frames,
  isolated-strip Xs.  No Qt scene state involved.

- ``paint_board(painter, display_list)`` walks the list and emits raw
  ``QPainter`` calls.  Used by the upcoming background-cache pipeline
  to rasterise the board into an offscreen pixmap (drawBackground
  refactor — see plan).

- ``render_board(scene, board, …)`` is the legacy entry point used by
  the current BoardView.refresh() path.  It builds the same display
  list and then converts every primitive into the corresponding
  ``QGraphicsItem`` so existing tests / drag interactions keep working
  unchanged.  When the background-cache lands this entry point will
  go away.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
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
    """Multiply the colour's existing alpha channel by *alpha*.

    The previous implementation called ``setAlphaF(alpha)``, which
    replaced the colour's per-pixel alpha entirely — so any user-set
    alpha in ``theme.pad_color`` (e.g. ``#FFFF0080`` for translucent
    yellow pads) was silently overwritten by the layer's whole-board
    ghost factor.  Multiplying instead preserves the user's alpha when
    the layer is fully visible (alpha=1.0) and still attenuates pads
    correctly when the board is ghosted under a routing/placement tool.
    """
    c = QColor(color)
    c.setAlphaF(c.alphaF() * alpha)
    return c


# ---------------------------------------------------------------------------
# Primitive descriptors — pure data, painted by ``paint_board``
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Pad:
    """One pad ring + matching hole.  Both circles share the outline pen."""

    cx: float
    cy: float
    r: float
    fill: QColor
    outline_pen: QPen
    hole_fill: QColor

    @property
    def hr(self) -> float:
        return self.r * 0.45


@dataclass(slots=True)
class _Line:
    """Single straight stroke (isolated-strip X arm)."""

    x1: float
    y1: float
    x2: float
    y2: float
    pen: QPen


@dataclass(slots=True)
class _FilledRect:
    """Filled and outlined rectangle (strip track underlay)."""

    rect: QRectF
    brush: QBrush
    pen: QPen


@dataclass(slots=True)
class _FrameRect:
    """Stroked-only rectangle (box frame outline)."""

    rect: QRectF
    pen: QPen


@dataclass(slots=True)
class BoardDisplayList:
    """Flat descriptors for everything the board layer paints.

    Order of buckets in this dataclass mirrors the Z-order used by
    ``paint_board``: tracks (under pads) → frame outlines → pad rings
    → isolated-strip X strokes (above pad rings) → holes (top-most).
    """

    filled_rects: list[_FilledRect] = field(default_factory=list)
    frame_rects: list[_FrameRect] = field(default_factory=list)
    pads: list[_Pad] = field(default_factory=list)
    lines: list[_Line] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Build phase — produce a display list from board + theme + layers
# ---------------------------------------------------------------------------


def _build_pad(
    dl: BoardDisplayList, cx: float, cy: float, r: float,
    color: QColor, theme: Theme,
) -> None:
    outline = QColor(theme.pad_outline_color)
    outline.setAlphaF(color.alphaF())
    outline_pen = QPen(outline, theme.pad_outline_width)
    hole_color = QColor(theme.hole_color)
    hole_color.setAlphaF(color.alphaF())
    dl.pads.append(_Pad(cx, cy, r, color, outline_pen, hole_color))


def _build_strip(
    dl: BoardDisplayList, atom: StripAtom, center: Point,
    strip_color: QColor, isolated_color: QColor, theme: Theme,
) -> None:
    length = atom.length if isinstance(atom.length, int) else 1

    if atom.isolated:
        cx, cy = center.x, center.y
        _build_pad(dl, cx, cy, theme.strip_radius, isolated_color, theme)
        r = theme.strip_radius * 0.7
        pen = QPen(QColor(theme.isolated_x_color), theme.isolated_x_width)
        for dx, dy in ((r, r), (-r, r)):
            dl.lines.append(_Line(cx - dx, cy - dy, cx + dx, cy + dy, pen))
        return

    track_h = theme.strip_radius * 1.1
    track_color = QColor(strip_color)
    track_pen = QPen(QColor(theme.strip_track_outline), theme.strip_track_outline_width)
    if atom.orientation == "horizontal":
        start_x = center.x - length / 2.0 + 0.5
        end_x = start_x + length - 1
        dl.filled_rects.append(_FilledRect(
            QRectF(start_x - theme.strip_radius, center.y - track_h,
                   (end_x - start_x) + 2 * theme.strip_radius, 2 * track_h),
            QBrush(track_color), track_pen,
        ))
        for i in range(length):
            _build_pad(dl, start_x + i, center.y, theme.strip_radius,
                       strip_color, theme)
    else:
        start_y = center.y - length / 2.0 + 0.5
        end_y = start_y + length - 1
        track_w = track_h
        dl.filled_rects.append(_FilledRect(
            QRectF(center.x - track_w, start_y - theme.strip_radius,
                   2 * track_w, (end_y - start_y) + 2 * theme.strip_radius),
            QBrush(track_color), track_pen,
        ))
        for i in range(length):
            _build_pad(dl, center.x, start_y + i, theme.strip_radius,
                       strip_color, theme)


def _build_frames(
    dl: BoardDisplayList, node: LayoutNode, *, is_root: bool, theme: Theme,
    alpha: float,
) -> None:
    from protoboard.domain.board.boxes import BoardGrid, HBox, VBox

    if is_root or isinstance(node.box, (HBox, VBox, BoardGrid)):
        half_w = node.width / 2.0
        half_h = node.height / 2.0
        rect = QRectF(node.x - half_w, node.y - half_h, node.width, node.height)
        if is_root:
            c = QColor(theme.board_frame_color)
            c.setAlphaF(c.alphaF() * alpha)
            pen = QPen(c, theme.board_frame_width)
        else:
            c = QColor(theme.board_sub_frame_color)
            c.setAlphaF(c.alphaF() * alpha)
            pen = QPen(c, theme.board_sub_frame_width)
            pen.setStyle(Qt.PenStyle.DashLine)
        dl.frame_rects.append(_FrameRect(rect, pen))

    for child in node.children:
        _build_frames(dl, child, is_root=False, theme=theme, alpha=alpha)


def build_board_display_list(
    board: Board,
    *,
    show_frames: bool = True,
    theme: Theme = DEFAULT_THEME,
    layers: LayerState = _DEFAULT_LAYERS,
) -> BoardDisplayList:
    """Return a flat descriptor list for everything the board layer draws.

    Pure: no Qt scene state, no scene allocation.  Suitable for either
    rasterising into an offscreen pixmap (background-cache pipeline) or
    converting back into ``QGraphicsItem`` instances (legacy scene path,
    via :func:`render_board`).
    """
    dl = BoardDisplayList()
    board_alpha = layers.alpha_for_board()
    if board_alpha == 0.0:
        return dl

    pad_color = _apply_alpha(QColor(theme.pad_color), board_alpha)
    strip_color = _apply_alpha(QColor(theme.strip_color), board_alpha)
    isolated_color = _apply_alpha(QColor(theme.isolated_color), board_alpha)

    if show_frames:
        _build_frames(dl, board.layout, is_root=True, theme=theme, alpha=board_alpha)
    for atom, pos in board.atoms():
        if isinstance(atom, PadAtom):
            _build_pad(dl, pos.x, pos.y, theme.pad_radius, pad_color, theme)
        elif isinstance(atom, StripAtom):
            _build_strip(dl, atom, pos, strip_color, isolated_color, theme)
    return dl


# ---------------------------------------------------------------------------
# Paint phase — render a display list via raw QPainter
# ---------------------------------------------------------------------------


def paint_board(painter: QPainter, display_list: BoardDisplayList) -> None:
    """Render *display_list* via direct ``QPainter`` calls.

    Bucket order matches the Z-order of the scene-item path: tracks are
    drawn first (under pads), then frame outlines, then pad rings, then
    isolated-strip Xs, finally holes on top.  This routine assumes the
    painter is already transformed into world coordinates (the caller
    sets up scale / translate before invoking it).
    """
    # Filled track rectangles (under pads on strips).
    for fr in display_list.filled_rects:
        painter.setBrush(fr.brush)
        painter.setPen(fr.pen)
        painter.drawRect(fr.rect)
    # Frame outlines.
    painter.setBrush(Qt.BrushStyle.NoBrush)
    for fr2 in display_list.frame_rects:
        painter.setPen(fr2.pen)
        painter.drawRect(fr2.rect)
    # Pad rings.
    for p in display_list.pads:
        painter.setBrush(QBrush(p.fill))
        painter.setPen(p.outline_pen)
        painter.drawEllipse(QRectF(
            p.cx - p.r, p.cy - p.r, 2 * p.r, 2 * p.r,
        ))
    # Isolated-strip X strokes (above pad rings).
    for line in display_list.lines:
        painter.setPen(line.pen)
        painter.drawLine(QPointF(line.x1, line.y1), QPointF(line.x2, line.y2))
    # Holes (top-most).
    for p in display_list.pads:
        painter.setBrush(QBrush(p.hole_fill))
        painter.setPen(p.outline_pen)
        painter.drawEllipse(QRectF(
            p.cx - p.hr, p.cy - p.hr, 2 * p.hr, 2 * p.hr,
        ))


# ---------------------------------------------------------------------------
# Legacy scene-based renderer — converts display list to QGraphicsItems
# ---------------------------------------------------------------------------


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

    This entry point survives for the existing ``BoardView.refresh()``
    path until the background-cache refactor migrates the scene off of
    these items entirely.  Internally it just builds a display list and
    converts each primitive to its matching ``QGraphicsItem``.
    """
    dl = build_board_display_list(
        board, show_frames=show_frames, theme=theme, layers=layers,
    )
    # Filled rects (strip tracks) — drawn under pads.
    for fr in dl.filled_rects:
        item = QGraphicsRectItem(fr.rect)
        item.setBrush(fr.brush)
        item.setPen(fr.pen)
        item.setZValue(Z_BOARD - 0.1)
        scene.addItem(item)
    # Frame rects (box outlines) — drawn just under board pads.
    for fr2 in dl.frame_rects:
        item = QGraphicsRectItem(fr2.rect)
        item.setBrush(QBrush(QColor(0, 0, 0, 0)))
        item.setPen(fr2.pen)
        item.setZValue(Z_BOARD - 0.2)
        scene.addItem(item)
    # Pads (ring + hole).
    for p in dl.pads:
        ring = QGraphicsEllipseItem(
            QRectF(p.cx - p.r, p.cy - p.r, 2 * p.r, 2 * p.r),
        )
        ring.setBrush(QBrush(p.fill))
        ring.setPen(p.outline_pen)
        ring.setZValue(Z_BOARD)
        scene.addItem(ring)
        hole = QGraphicsEllipseItem(
            QRectF(p.cx - p.hr, p.cy - p.hr, 2 * p.hr, 2 * p.hr),
        )
        hole.setBrush(QBrush(p.hole_fill))
        hole.setPen(p.outline_pen)
        hole.setZValue(Z_BOARD + 0.05)
        scene.addItem(hole)
    # Isolated-strip X strokes.
    for line in dl.lines:
        item = QGraphicsLineItem(line.x1, line.y1, line.x2, line.y2)
        item.setPen(line.pen)
        item.setZValue(Z_BOARD)
        scene.addItem(item)


__all__ = [
    "BOARD_BG",
    "BoardDisplayList",
    "build_board_display_list",
    "paint_board",
    "render_board",
]
