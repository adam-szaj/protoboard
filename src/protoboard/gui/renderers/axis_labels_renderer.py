# SPDX-License-Identifier: MIT
"""Render column/row index labels around the board on all four sides.

Labels reuse the overlay infrastructure from ``component_renderer`` so they
keep a constant on-screen size regardless of zoom — the same trick the
component refdes labels use.  Each axis (columns or rows) can pick its
format independently (numeric 0-based, 1-based, alphabetic, or disabled)
via :class:`AxisLabelSettings`.

Pixel-perfect centring with ``ItemIgnoresTransformations`` is awkward
because ``boundingRect()`` reports device pixels while ``setPos()`` takes
scene coordinates — the conversion needs the current view scale.  The
caller therefore passes ``view_scale`` (scene→pixel ratio) so we can
divide the pixel-wide bounding rect by it before laying the label out.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QFontMetricsF
from PySide6.QtWidgets import QGraphicsItem, QGraphicsScene

from protoboard.controller.view_options.theme import Theme
from protoboard.domain.axis_labels_settings import (
    AxisLabelFormat,
    AxisLabelSettings,
    format_index,
)
from protoboard.domain.board.board import Board
from protoboard.gui.layer_state import Z_OVERLAY
from protoboard.gui.renderers.component_renderer import (
    _get_refdes_font,
    _make_overlay_label,
)

# Distance (raster units) between the board edge and the closest edge of the
# label rectangle.  Small enough to feel attached to the board, large enough
# that the text doesn't bleed into pads/components.
_EDGE_MARGIN = 0.35


def render_axis_labels(
    scene: QGraphicsScene,
    board: Board,
    settings: AxisLabelSettings,
    *,
    theme: Theme,
    alpha: float,
    view_scale: float = 1.0,
) -> list[QGraphicsItem]:
    """Add column/row index labels to *scene*; return the added items.

    *view_scale* is the current scene→pixel ratio (e.g. ``view.transform().m11()``)
    used to convert the label's pixel-sized bounding rect into scene units
    when positioning.  Falls back to 1.0 (raw pixels treated as scene units)
    when callers don't have access to the view, which still produces
    something readable, just not perfectly centred.
    """
    if alpha <= 0.0 or not settings.any_enabled():
        return []
    if view_scale <= 0.0:
        view_scale = 1.0

    bounds = board.bounds
    n_cols = int(round(bounds.lx))
    n_rows = int(round(bounds.ly))

    font: QFont = _get_refdes_font(theme.overlay_font_size)
    metrics = QFontMetricsF(font)
    # Approximate label half-extent in scene units; assumes ItemIgnoresTransformations
    # so the displayed size is fixed in pixels and we divide by view_scale.
    # We compute per-text to keep variable-width strings centred.
    line_height_scene = metrics.height() / view_scale

    # Axis labels live on the canvas outside the board, so the surrounding
    # board_bg colour is the right background — nothing underneath the label
    # could be obscured by it.  Text colour is its own theme entry so the
    # default stays readable against a dark canvas.  Layer alpha is applied
    # only to the bg pill; text keeps its theme alpha so dimmed axis labels
    # stay legible.
    text_color = QColor(theme.axis_label_color)
    bg_color = QColor(theme.board_bg)
    bg_color.setAlphaF(bg_color.alphaF() * alpha)

    items: list[QGraphicsItem] = []

    def _half_w(text: str) -> float:
        return metrics.horizontalAdvance(text) / 2.0 / view_scale

    # Columns: labels above and below the board, horizontally centred on
    # each pad column (x = bounds.x + i + 0.5).
    if settings.columns != AxisLabelFormat.DISABLED:
        # Vertical offsets in scene units.  Above: subtract label height
        # plus margin so the bottom of the label sits at bounds.y - margin.
        y_above = bounds.y - _EDGE_MARGIN - line_height_scene
        y_below = bounds.y + bounds.ly + _EDGE_MARGIN
        for i in range(n_cols):
            text = format_index(settings.columns, i)
            if not text:
                continue
            x_centre = bounds.x + i + 0.5
            x_topleft = x_centre - _half_w(text)
            items.extend(
                _make_overlay_label(
                    text,
                    font,
                    text_color,
                    bg_color,
                    x_topleft,
                    y_above,
                    Z_OVERLAY,
                )
            )
            items.extend(
                _make_overlay_label(
                    text,
                    font,
                    text_color,
                    bg_color,
                    x_topleft,
                    y_below,
                    Z_OVERLAY,
                )
            )

    # Rows: labels left and right of the board, vertically centred on each
    # pad row.  Vertical centring uses the line height; horizontal placement
    # makes the right edge of the label sit at (bounds.x - margin) on the
    # left side and the left edge at (bounds.x + width + margin) on the
    # right side.
    if settings.rows != AxisLabelFormat.DISABLED:
        for j in range(n_rows):
            text = format_index(settings.rows, j)
            if not text:
                continue
            y_topleft = bounds.y + j + 0.5 - line_height_scene / 2.0
            half_w = _half_w(text) * 2.0  # full width
            x_left = bounds.x - _EDGE_MARGIN - half_w
            x_right = bounds.x + bounds.lx + _EDGE_MARGIN
            items.extend(
                _make_overlay_label(
                    text,
                    font,
                    text_color,
                    bg_color,
                    x_left,
                    y_topleft,
                    Z_OVERLAY,
                )
            )
            items.extend(
                _make_overlay_label(
                    text,
                    font,
                    text_color,
                    bg_color,
                    x_right,
                    y_topleft,
                    Z_OVERLAY,
                )
            )

    for it in items:
        scene.addItem(it)
    return items


__all__ = ["render_axis_labels"]
