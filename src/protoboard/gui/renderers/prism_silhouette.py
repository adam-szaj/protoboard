# SPDX-License-Identifier: MIT
"""Prism body silhouettes — DIP / SIL / Quad, drawn parametrically.

A prism :class:`~protoboard.domain.components.package.shapes.Prism` covers every
multi-pin in-line package: a single column (SIL header), two columns (DIP IC),
or four columns (quad in-line module).  The *silhouette* — the body outline plus
its class-agnostic decorations (pin-1 notch, SIL slots, quad outer strips,
pin-1 marker) — depends on that **column layout**, not on the component class.

Rather than branch on the layout inside the renderer, each layout is a
:class:`PrismSilhouette` that builds its own outline.  *Which* silhouette a
package gets is the renderer registry's decision: a declared ``kind`` maps to
a renderer subclass (:mod:`protoboard.gui.renderers.packages.prism_kinds`)
that picks its silhouette explicitly, while :func:`silhouette_for` remains the
column-layout *guess* the base :class:`PrismRenderer` falls back to.  The
geometry is built in the package-local frame (pin 1 at the origin), exactly as
the legacy ``Svg*Body`` generators did, so the caller places it with one
pin1→pin2 transform.

Pure geometry: a silhouette returns ``QPainterPath`` outlines, never placed
``QGraphicsItem``s — the package renderers own the colour, pen and transform.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from PySide6.QtCore import QRectF
from PySide6.QtGui import QPainterPath

from protoboard.controller.view_options.theme import Theme
from protoboard.domain.components.package.shapes import Prism
from protoboard.geometry import Point
from protoboard.gui.qt_geometry import to_qrectf

# Pins within this many holes of one another share a column.  DIP rows sit
# whole holes apart (rows_spacing >= 2), so a generous tolerance still separates
# columns cleanly while tolerating float noise from the offset solver.
_COLUMN_EPS = 0.25


def _columns(positions: list[Point]) -> list[tuple[float, int]]:
    """Each column in *positions* as ``(mean_x, pin_count)``, left to right.

    Pins whose x differ by less than :data:`_COLUMN_EPS` share a column; each
    column reports the mean x of its members and how many it holds.  The column
    count and per-column pin count select the silhouette (a two-column layout
    with one pin per column is a two-pin part, not a DIP); the two-column mean
    fixes the DIP notch.  The body's extent comes from the package bbox, so the
    per-column y-span is not needed here.
    """
    cols: list[list[float]] = []
    for p in sorted(positions, key=lambda q: q.x):
        if cols and abs(p.x - cols[-1][0]) < _COLUMN_EPS:
            cols[-1].append(p.x)
        else:
            cols.append([p.x])
    return [(sum(xs) / len(xs), len(xs)) for xs in cols]


@dataclass(frozen=True)
class PrismDrawing:
    """A built silhouette in the package-local frame (pin 1 at the origin).

    ``body`` is the filled outline; ``marker`` is the small solid pin-1 square.
    The caller fills and places them.
    """

    body: QPainterPath
    marker: QPainterPath | None = None


#: A ``corner_r`` argument: one radius for all corners, a per-corner 4-tuple
#: (top-left, top-right, bottom-right, bottom-left — the CSS ``border-radius``
#: expansion), or ``None`` for the theme default.  All values are in holes.
CornerRadii = float | tuple[float, float, float, float] | None


def _expand_corner_r(
    corner_r: CornerRadii, theme: Theme
) -> tuple[float, float, float, float]:
    """Normalise a ``corner_r`` argument to per-corner radii (holes)."""
    if corner_r is None:
        r = theme.body_corner_radius
        return (r, r, r, r)
    if isinstance(corner_r, tuple):
        return corner_r
    return (corner_r, corner_r, corner_r, corner_r)


def rounded_rect_path(
    rect: QRectF,
    radii: tuple[float, float, float, float],
) -> QPainterPath:
    """A rectangle path with per-corner radii (top-left, top-right,
    bottom-right, bottom-left), each clamped to the rect's half-extents.

    Uniform radii take Qt's ``addRoundedRect`` (exact quarter-ellipse arcs,
    byte-identical with the historical single-radius path); mixed radii build
    the outline with ``quadTo`` corners, the house style of
    :func:`_notched_rect_path`.
    """
    half = min(rect.width(), rect.height()) / 2.0
    tl, tr, br, bl = (max(0.0, min(r, half)) for r in radii)
    path = QPainterPath()
    if tl == tr == br == bl:
        if tl > 0:
            path.addRoundedRect(rect, tl, tl)
        else:
            path.addRect(rect)
        return path
    x0, y0, x1, y1 = rect.left(), rect.top(), rect.right(), rect.bottom()
    path.moveTo(x0 + tl, y0)
    path.lineTo(x1 - tr, y0)
    path.quadTo(x1, y0, x1, y0 + tr)
    path.lineTo(x1, y1 - br)
    path.quadTo(x1, y1, x1 - br, y1)
    path.lineTo(x0 + bl, y1)
    path.quadTo(x0, y1, x0, y1 - bl)
    path.lineTo(x0, y0 + tl)
    path.quadTo(x0, y0, x0 + tl, y0)
    path.closeSubpath()
    return path


class PrismSilhouette(ABC):
    """Builds the local-frame outline of one prism column layout."""

    @abstractmethod
    def build(
        self, prism: Prism, theme: Theme, *, corner_r: CornerRadii = None
    ) -> PrismDrawing:
        """Build the body outline.  ``corner_r`` (holes; scalar or per-corner
        4-tuple) overrides the theme's ``body_corner_radius`` when given (an
        appearance override); ``None`` keeps the theme default."""
        ...


def _notched_rect_path(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    *,
    radii: tuple[float, float, float, float],
    notch_r: float,
    notch_cx: float,
) -> QPainterPath:
    """Rounded rectangle [x0,y0]-[x1,y1] with a semicircular notch cut into its
    top edge, centred at ``notch_cx`` — the DIP / quad pin-1 marker edge.
    ``radii`` are the per-corner radii (top-left, top-right, bottom-right,
    bottom-left).

    Mirrors the legacy two-arc SVG path: the top edge runs to the notch's left
    point, arcs (concave, into the body) to its right point, then continues.
    Degenerate notches (``notch_r <= 0``) collapse to a plain rounded rect.
    """
    half = min((x1 - x0) / 2.0, (y1 - y0) / 2.0)
    tl, tr, br, bl = (max(0.0, min(r, half)) for r in radii)
    if notch_r <= 0.0:
        return rounded_rect_path(QRectF(x0, y0, x1 - x0, y1 - y0), (tl, tr, br, bl))
    nx0 = notch_cx - notch_r
    path = QPainterPath()
    path.moveTo(x0 + tl, y0)
    path.lineTo(nx0, y0)
    # Concave semicircle dipping into the body (sweep matches the SVG A 0,0);
    # the 180° arc lands at the notch's right point (notch_cx + notch_r).
    path.arcTo(QRectF(nx0, y0 - notch_r, 2 * notch_r, 2 * notch_r), 180.0, 180.0)
    path.lineTo(x1 - tr, y0)
    path.quadTo(x1, y0, x1, y0 + tr)
    path.lineTo(x1, y1 - br)
    path.quadTo(x1, y1, x1 - br, y1)
    path.lineTo(x0 + bl, y1)
    path.quadTo(x0, y1, x0, y1 - bl)
    path.lineTo(x0, y0 + tl)
    path.quadTo(x0, y0, x0 + tl, y0)
    path.closeSubpath()
    return path


def _pin1_marker_path(half: float) -> QPainterPath:
    """A small solid square centred on pin 1 (the origin)."""
    path = QPainterPath()
    path.addRect(QRectF(-half, -half, 2 * half, 2 * half))
    return path


@dataclass(frozen=True, slots=True)
class DipSilhouette(PrismSilhouette):
    """Two-column DIP / DIL body: a notched rounded rectangle spanning the rows.

    The body hugs the package bbox (anchored at pin 1); the pin-1 notch sits on
    the top edge midway between the two columns (``notch_cx``), on the pin-1
    side.
    """

    notch_cx: float

    def build(
        self, prism: Prism, theme: Theme, *, corner_r: CornerRadii = None
    ) -> PrismDrawing:
        box = prism.bbox()
        body = _notched_rect_path(
            box.x,
            box.y,
            box.x + box.lx,
            box.y + box.ly,
            radii=_expand_corner_r(corner_r, theme),
            notch_r=theme.dip_notch_radius,
            notch_cx=self.notch_cx,
        )
        return PrismDrawing(body=body, marker=_pin1_marker_path(0.12))


@dataclass(frozen=True, slots=True)
class SilSilhouette(PrismSilhouette):
    """Single-column SIL header body: a rounded housing hugging the pin row.

    The pins themselves are drawn as pads by the component renderer over the
    body, so the housing is a plain rounded rectangle with a pin-1 marker — no
    redundant per-pin slot decoration.
    """

    def build(
        self, prism: Prism, theme: Theme, *, corner_r: CornerRadii = None
    ) -> PrismDrawing:
        box = prism.bbox()
        body = rounded_rect_path(
            to_qrectf(box),
            _expand_corner_r(corner_r, theme),
        )
        return PrismDrawing(body=body, marker=_pin1_marker_path(0.08))


def dip_silhouette(prism: Prism) -> PrismSilhouette | None:
    """A DIP notch parameterized by *prism*'s two pin columns (None if not two).

    Used by the ``dip_ic`` / ``wdip_ic`` kind renderers
    (:mod:`protoboard.gui.renderers.packages.prism_kinds`) — the declared-look
    counterpart of the column guess below.
    """
    cols = _columns(prism.pin_positions())
    if len(cols) == 2:
        return DipSilhouette(notch_cx=(cols[0][0] + cols[1][0]) / 2.0)
    return None


def silhouette_for(prism: Prism) -> PrismSilhouette | None:
    """Pick the silhouette strategy for *prism* from its column layout.

    One column of ≥3 pins → SIL header; two columns of ≥2 pins each → DIP.
    Everything else — a two-pin part (incl. an SMD chip: two columns of one
    pin, or one column of two), a three-column oddity, or a multi-column
    dev-board module — has no dedicated silhouette and the caller falls back to
    the plain bbox rectangle (the SMD chip / dev-board body look).
    """
    cols = _columns(prism.pin_positions())
    if len(cols) == 1 and cols[0][1] >= 3:
        return SilSilhouette()
    if len(cols) == 2 and all(count >= 2 for _, count in cols):
        return DipSilhouette(notch_cx=(cols[0][0] + cols[1][0]) / 2.0)
    return None


__all__ = [
    "CornerRadii",
    "DipSilhouette",
    "PrismDrawing",
    "PrismSilhouette",
    "SilSilhouette",
    "dip_silhouette",
    "rounded_rect_path",
    "silhouette_for",
]
