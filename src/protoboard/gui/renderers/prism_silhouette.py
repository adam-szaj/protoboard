# SPDX-License-Identifier: MIT
"""Prism body silhouettes — DIP / SIL / Quad, drawn parametrically.

A prism :class:`~protoboard.domain.components.package.shapes.Prism` covers every
multi-pin in-line package: a single column (SIL header), two columns (DIP IC),
or four columns (quad in-line module).  The *silhouette* — the body outline plus
its class-agnostic decorations (pin-1 notch, SIL slots, quad outer strips,
pin-1 marker) — depends on that **column layout**, not on the component class.

Rather than branch on the layout inside the renderer, each layout is a
:class:`PrismSilhouette` that builds its own outline; :func:`silhouette_for`
classifies the package's canonical pin cloud into columns and picks the
strategy.  The geometry is built in the package-local frame (pin 1 at the
origin), exactly as the legacy ``Svg*Body`` generators did, so the caller places
it with one pin1→pin2 transform.

Pure geometry: a silhouette returns ``QPainterPath`` outlines, never placed
``QGraphicsItem``s — :class:`~protoboard.gui.renderers.body_shapes.PackageBody`
owns the colour, pen and transform.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QRectF
from PySide6.QtGui import QPainterPath

from protoboard.domain.components.package.shapes import Prism
from protoboard.geometry import Point
from protoboard.gui.theme import Theme

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


class PrismSilhouette(ABC):
    """Builds the local-frame outline of one prism column layout."""

    @abstractmethod
    def build(
        self, prism: Prism, theme: Theme, *, corner_r: float | None = None
    ) -> PrismDrawing:
        """Build the body outline.  ``corner_r`` (holes) overrides the theme's
        ``body_corner_radius`` when given (an appearance override); ``None`` keeps
        the theme default."""
        ...


def _notched_rect_path(
    x0: float, y0: float, x1: float, y1: float, *, corner_r: float, notch_r: float,
    notch_cx: float,
) -> QPainterPath:
    """Rounded rectangle [x0,y0]-[x1,y1] with a semicircular notch cut into its
    top edge, centred at ``notch_cx`` — the DIP / quad pin-1 marker edge.

    Mirrors the legacy two-arc SVG path: the top edge runs to the notch's left
    point, arcs (concave, into the body) to its right point, then continues.
    Degenerate notches (``notch_r <= 0``) collapse to a plain rounded rect.
    """
    r = max(0.0, min(corner_r, (x1 - x0) / 2.0, (y1 - y0) / 2.0))
    path = QPainterPath()
    if notch_r <= 0.0:
        path.addRoundedRect(QRectF(x0, y0, x1 - x0, y1 - y0), r, r)
        return path
    nx0 = notch_cx - notch_r
    path.moveTo(x0 + r, y0)
    path.lineTo(nx0, y0)
    # Concave semicircle dipping into the body (sweep matches the SVG A 0,0);
    # the 180° arc lands at the notch's right point (notch_cx + notch_r).
    path.arcTo(QRectF(nx0, y0 - notch_r, 2 * notch_r, 2 * notch_r), 180.0, 180.0)
    path.lineTo(x1 - r, y0)
    path.quadTo(x1, y0, x1, y0 + r)
    path.lineTo(x1, y1 - r)
    path.quadTo(x1, y1, x1 - r, y1)
    path.lineTo(x0 + r, y1)
    path.quadTo(x0, y1, x0, y1 - r)
    path.lineTo(x0, y0 + r)
    path.quadTo(x0, y0, x0 + r, y0)
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
        self, prism: Prism, theme: Theme, *, corner_r: float | None = None
    ) -> PrismDrawing:
        box = prism.bbox()
        body = _notched_rect_path(
            box.x, box.y, box.x + box.width, box.y + box.height,
            corner_r=theme.body_corner_radius if corner_r is None else corner_r,
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
        self, prism: Prism, theme: Theme, *, corner_r: float | None = None
    ) -> PrismDrawing:
        box = prism.bbox()
        r = theme.body_corner_radius if corner_r is None else corner_r
        body = QPainterPath()
        body.addRoundedRect(QRectF(box.x, box.y, box.width, box.height), r, r)
        return PrismDrawing(body=body, marker=_pin1_marker_path(0.08))


def _dip_silhouette(prism: Prism) -> PrismSilhouette | None:
    """A DIP notch parameterized by *prism*'s two pin columns (None if not two)."""
    cols = _columns(prism.pin_positions())
    if len(cols) == 2:
        return DipSilhouette(notch_cx=(cols[0][0] + cols[1][0]) / 2.0)
    return None


# Declared body kind → silhouette strategy. ``dip_switch`` is intentionally a
# *plain* dual-row body (returns None → plain bbox), the case the column-guess
# could not distinguish from a real DIP-IC. ``sil`` → housing. A kind absent
# from this table falls through to the column-based guess (``silhouette_for``).
_KIND_SILHOUETTE: dict[str, Callable[[Prism], PrismSilhouette | None]] = {
    "dip_ic": _dip_silhouette,
    "wdip_ic": _dip_silhouette,
    "dip_switch": lambda _prism: None,
    "sil": lambda _prism: SilSilhouette(),
    # Connectors render as a plain coloured rectangle (no DIP notch / SIL slots);
    # the 1×N column guess would otherwise add a SIL housing and 2×N a DIP notch.
    "pin_header": lambda _prism: None,
    "jst": lambda _prism: None,
}


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


def silhouette_for_prism(prism: Prism) -> PrismSilhouette | None:
    """The body silhouette for *prism*: declared ``kind`` first, else guessed.

    When the prism carries a declared ``kind`` (built from a package ``kind:``
    field) the look is *declared* — a ``dip_switch`` is a plain dual-row body, a
    ``dip_ic`` has the notch — instead of being inferred from the pin columns. An
    unknown / absent kind falls back to :func:`silhouette_for` (the column guess),
    so render never blocks on a kind it does not recognise.
    """
    factory = _KIND_SILHOUETTE.get(prism.kind) if prism.kind else None
    if factory is not None:
        return factory(prism)
    return silhouette_for(prism)


__all__ = [
    "DipSilhouette",
    "PrismDrawing",
    "PrismSilhouette",
    "SilSilhouette",
    "silhouette_for",
    "silhouette_for_prism",
]
