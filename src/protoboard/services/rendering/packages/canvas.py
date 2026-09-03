# SPDX-License-Identifier: MIT
"""BodyCanvas — painter-style item factory for package-body drawing.

The convenience API for :class:`PackageRenderer` subclasses and renderer
plugins: instead of hand-assembling ``QPainterPath → QGraphicsPathItem →
brush → pen → alpha → z → transform`` for every shape, a canvas carries that
state once and each ``add<Shape>`` call produces a finished, collected item.

Frames and units: canvas coordinates are **raster units in the frame given by
its transform** (the renderer supplies the same local→world transform the pins
use — rotation, bottom-side reflection, anchor — or the pad-axis frame for
axis-aligned bodies). Millimetre inputs convert via :meth:`BodyCanvas.mm`.

State vs inline: ``setFillColor`` / ``setBorderColor`` / ``setPen`` change the
defaults for subsequent calls; every ``add<Shape>`` also accepts the same
parameters as keyword arguments for that one item.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QRectF
from PySide6.QtGui import QBrush, QColor, QPainterPath, QPen, QTransform
from PySide6.QtWidgets import QGraphicsItem, QGraphicsPathItem

from protoboard.controller.view_options.theme import Theme
from protoboard.geometry import Point
from protoboard.instrumentation.debug_trace import traced
from protoboard.services.rendering.prism_silhouette import rounded_rect_path

#: Rounding for addRect: a single radius, per-corner radii
#: (top-left, top-right, bottom-right, bottom-left), or None for sharp corners.
CornerRound = float | tuple[float, float, float, float] | None

_XY = Point | tuple[float, float]


@dataclass(frozen=True)
class PaintState:
    """The per-draw paint parameters of one ``draw()`` call.

    Bundles what every body-drawing helper needs: the resolved body colours,
    the layer alpha, the base Z and the theme.  ``PackageRenderer.draw``
    builds one per call and exposes it as ``self.paint`` for the duration of
    the draw, so ``render_body`` overrides, internal helpers and the canvas
    factories need no parameter threading.
    """

    fill: QColor
    border: QColor
    alpha: float
    z: float
    theme: Theme


def qcolor_from_css(value: str) -> QColor:
    """``QColor`` from a CSS hex colour string.

    CSS puts the alpha channel LAST (``#rrggbbaa`` / ``#rgba``); Qt's
    ``QColor`` constructor expects it FIRST (``#aarrggbb`` / ``#argb``).
    Reorder the 4- and 8-digit forms; every other value passes through
    unchanged.  All colours authored in YAML (``style:`` strings, package
    ``color:``) use the CSS convention.
    """
    v = value.strip()
    if v.startswith("#"):
        digits = v[1:]
        if len(digits) == 8:
            return QColor(f"#{digits[6:]}{digits[:6]}")
        if len(digits) == 4:
            return QColor(f"#{digits[3]}{digits[:3]}")
    return QColor(v)


def body_pen(border: QColor, alpha: float, width: float = 0.06) -> QPen:
    """The standard body-outline pen: *border* at *alpha*."""
    c = QColor(border)
    c.setAlphaF(alpha)
    return QPen(c, width)


# Compatibility alias for renderer plugins using the old private helper.
_body_pen = body_pen


def _as_qcolor(value: QColor | str) -> QColor:
    return QColor(value) if isinstance(value, QColor) else qcolor_from_css(value)


def _xy(p: _XY) -> tuple[float, float]:
    if isinstance(p, Point):
        return p.x, p.y
    return float(p[0]), float(p[1])


def _radii(round: CornerRound) -> tuple[float, float, float, float]:
    if round is None:
        return (0.0, 0.0, 0.0, 0.0)
    if isinstance(round, tuple):
        return round
    return (round, round, round, round)


class BodyCanvas:
    """Collects QGraphicsItems for one body, applying shared drawing state.

    Every ``add<Shape>`` builds a :class:`QPainterPath` in the canvas frame,
    maps it through the transform, wraps it in a :class:`QGraphicsPathItem`
    with fill/pen/alpha/z applied, collects it and returns it. Read the result
    from :attr:`items`.
    """

    def __init__(
        self,
        theme: Theme,
        *,
        fill: QColor | str,
        border: QColor | str,
        alpha: float = 1.0,
        z: float = 0.0,
        transform: QTransform | None = None,
        bake: bool = False,
        units: str = "holes",
    ) -> None:
        """*bake* selects how the transform reaches the items: ``False``
        (default) sets it as the item transform (paths stay in the canvas
        frame — the axis-frame convention); ``True`` maps every path through
        it (world-frame paths, identity item transform — the body-placement
        convention of ``_draw_bbox`` / ``_draw_prism``).

        *units* selects the unit of the GEOMETRY inputs — coordinates, radii
        and prebuilt paths: ``"holes"`` (raster units, the default) or
        ``"mm"`` (converted to holes on input, for bodies authored straight
        from datasheet dimensions).  Style stays unit-less either way: pen
        widths remain in holes and Z is a layer index, so switching units
        never changes stroke weight or stacking."""
        if units not in ("holes", "mm"):
            raise ValueError(f"unknown canvas units: {units!r}")
        self._theme = theme
        self._alpha = alpha
        self._z = z
        self._transform = QTransform() if transform is None else transform
        self._bake = bake
        self._scale = 1.0 if units == "holes" else 1.0 / theme.raster_pitch_mm
        self._fill = _as_qcolor(fill)
        self._border = _as_qcolor(border)
        self._pen: QPen | None = None
        self._items: list[QGraphicsItem] = []
        self._z_seen: list[float] = []

    # -- state ---------------------------------------------------------

    def setFillColor(self, color: QColor | str) -> None:
        """Default fill for subsequent shapes."""
        self._fill = _as_qcolor(color)

    def setBorderColor(self, color: QColor | str) -> None:
        """Default outline colour for subsequent shapes (standard body pen)."""
        self._border = _as_qcolor(color)

    def setPen(self, pen: QPen | None) -> None:
        """Explicit default pen; ``None`` returns to the border-derived pen."""
        self._pen = pen

    def mm(self, value_mm: float) -> float:
        """Convert millimetres to raster units (holes).

        For per-value conversion on a ``units="holes"`` canvas; a
        ``units="mm"`` canvas converts its geometry inputs itself.
        """
        return value_mm / self._theme.raster_pitch_mm

    def _pt(self, p: _XY) -> tuple[float, float]:
        """A geometry input point, converted to holes per the canvas units."""
        x, y = _xy(p)
        return x * self._scale, y * self._scale

    @property
    def items(self) -> list[QGraphicsItem]:
        """The items created so far, in draw order."""
        return self._items

    #: Z separation between stacked body details (the ``z + 0.005`` idiom the
    #: hand-coded renderers used for markers / tabs above the plastic body).
    Z_STEP = 0.005

    def track(self, items: list[QGraphicsItem]) -> None:
        """Include externally created *items* (e.g. ``super().render_body()``
        output) in the :meth:`zAbove` / :meth:`zBelow` bookkeeping without
        collecting them."""
        self._z_seen.extend(it.zValue() for it in items)

    def zAbove(self) -> float:
        """A Z one :attr:`Z_STEP` above everything drawn/tracked so far.

        With nothing drawn yet this is the canvas base z — pass the result as
        the ``z=`` keyword of any ``add<Shape>`` call to stack a detail on top
        (a metal tab, a terminal cap, a polarity marker).
        """
        if not self._z_seen:
            return self._z
        return max(self._z_seen) + self.Z_STEP

    def zBelow(self) -> float:
        """A Z one :attr:`Z_STEP` below everything drawn/tracked so far."""
        if not self._z_seen:
            return self._z
        return min(self._z_seen) - self.Z_STEP

    # -- shapes ---------------------------------------------------------

    @traced()
    def addRect(
        self,
        p1: _XY,
        p2: _XY,
        *,
        round: CornerRound = None,
        fill: QColor | str | None = None,
        border: QColor | str | None = None,
        pen: QPen | None = None,
        pen_width: float | None = None,
        z_offset: float = 0.0,
        z: float | None = None,
    ) -> QGraphicsPathItem:
        """A rectangle spanning opposite corners *p1*/*p2*.

        ``round`` gives a uniform or per-corner (tl, tr, br, bl) radius.
        """
        x1, y1 = self._pt(p1)
        x2, y2 = self._pt(p2)
        rect = QRectF(min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))
        s = self._scale
        tl, tr, br, bl = _radii(round)
        path = rounded_rect_path(rect, (tl * s, tr * s, br * s, bl * s))
        return self._emit(path, fill, border, pen, pen_width, z_offset, z)

    @traced()
    def addCircle(
        self,
        center: _XY,
        r: float,
        *,
        fill: QColor | str | None = None,
        border: QColor | str | None = None,
        pen: QPen | None = None,
        pen_width: float | None = None,
        z_offset: float = 0.0,
        z: float | None = None,
    ) -> QGraphicsPathItem:
        """A circle of radius *r* centred on *center*."""
        return self.addEllipse(
            center,
            r,
            r,
            fill=fill,
            border=border,
            pen=pen,
            pen_width=pen_width,
            z_offset=z_offset,
            z=z,
        )

    @traced()
    def addEllipse(
        self,
        center: _XY,
        rx: float,
        ry: float,
        *,
        fill: QColor | str | None = None,
        border: QColor | str | None = None,
        pen: QPen | None = None,
        pen_width: float | None = None,
        z_offset: float = 0.0,
        z: float | None = None,
    ) -> QGraphicsPathItem:
        """An axis-aligned ellipse with half-extents *rx*/*ry*."""
        cx, cy = self._pt(center)
        rx, ry = rx * self._scale, ry * self._scale
        path = QPainterPath()
        path.addEllipse(QRectF(cx - rx, cy - ry, 2 * rx, 2 * ry))
        return self._emit(path, fill, border, pen, pen_width, z_offset, z)

    @traced()
    def addArc(
        self,
        center: _XY,
        r: float,
        start_deg: float,
        span_deg: float,
        *,
        border: QColor | str | None = None,
        pen: QPen | None = None,
        pen_width: float | None = None,
        z_offset: float = 0.0,
        z: float | None = None,
    ) -> QGraphicsPathItem:
        """A stroked (unfilled) circular arc — e.g. a DIP notch.

        Angles are Qt-style degrees: 0° at 3 o'clock, positive
        counter-clockwise.
        """
        cx, cy = self._pt(center)
        r = r * self._scale
        rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
        path = QPainterPath()
        path.arcMoveTo(rect, start_deg)
        path.arcTo(rect, start_deg, span_deg)
        item = self._emit(path, None, border, pen, pen_width, z_offset, z)
        item.setBrush(QBrush())  # stroke only
        return item

    @traced()
    def addPath(
        self,
        path: QPainterPath,
        *,
        fill: QColor | str | None = None,
        border: QColor | str | None = None,
        pen: QPen | None = None,
        pen_width: float | None = None,
        z_offset: float = 0.0,
        z: float | None = None,
    ) -> QGraphicsPathItem:
        """An arbitrary canvas-frame path (e.g. a prebuilt silhouette)."""
        if self._scale != 1.0:
            path = QTransform.fromScale(self._scale, self._scale).map(path)
        return self._emit(path, fill, border, pen, pen_width, z_offset, z)

    # -- internals ------------------------------------------------------

    def _emit(
        self,
        path: QPainterPath,
        fill: QColor | str | None,
        border: QColor | str | None,
        pen: QPen | None,
        pen_width: float | None,
        z_offset: float,
        z: float | None,
    ) -> QGraphicsPathItem:
        if self._bake:
            item = QGraphicsPathItem(self._transform.map(path))
        else:
            item = QGraphicsPathItem(path)
            if not self._transform.isIdentity():
                item.setTransform(self._transform)
        fill_c = QColor(self._fill if fill is None else _as_qcolor(fill))
        fill_c.setAlphaF(fill_c.alphaF() * self._alpha)
        item.setBrush(QBrush(fill_c))
        item.setPen(self._resolve_pen(border, pen, pen_width))
        item.setZValue((self._z + z_offset) if z is None else z)
        self._items.append(item)
        self._z_seen.append(item.zValue())
        return item

    def _resolve_pen(
        self,
        border: QColor | str | None,
        pen: QPen | None,
        pen_width: float | None,
    ) -> QPen:
        explicit = pen if pen is not None else self._pen
        if explicit is not None and pen_width is None and border is None:
            out = QPen(explicit)
            c = out.color()
            c.setAlphaF(c.alphaF() * self._alpha)
            out.setColor(c)
            return out
        border_c = self._border if border is None else _as_qcolor(border)
        if pen_width is not None:
            return body_pen(border_c, self._alpha, width=pen_width)
        return body_pen(border_c, self._alpha)
