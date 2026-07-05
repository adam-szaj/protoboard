# SPDX-License-Identifier: MIT
"""Per-kind prism renderers — the declared body looks, as registry entries.

These replace the old ``_KIND_SILHOUETTE`` table in ``prism_silhouette.py``:
each procedural package ``kind`` (see ``domain/components/package/kind.py``)
registers a renderer whose ``_silhouette`` returns the *declared* look, so a
``dip_switch`` stays plain even though its two pin columns would column-guess
a DIP notch.  A kind with no registered renderer falls back to the base
:class:`~protoboard.gui.renderers.packages.prism.PrismRenderer` (the column
guess) — rendering never blocks on an unknown kind.
"""

from __future__ import annotations

from PySide6.QtGui import QBrush, QColor, QTransform
from PySide6.QtWidgets import QGraphicsItem, QGraphicsSimpleTextItem

from protoboard.domain.components.package.shapes import (
    Prism as _PkgPrism,
)
from protoboard.domain.components.package.shapes import (
    lead_axis_extents_or_none,
)
from protoboard.geometry import Point
from protoboard.gui.renderers.packages.base import register_renderer
from protoboard.gui.renderers.packages.canvas import (
    BodyCanvas,
    qcolor_from_css,
)
from protoboard.gui.renderers.packages.prism import PrismRenderer
from protoboard.gui.renderers.prism_silhouette import (
    PrismSilhouette,
    SilSilhouette,
    dip_silhouette,
)
from protoboard.gui.renderers.value_codes import parse_value, smd_code
from protoboard.gui.theme import Theme


@register_renderer(kind=("dip_ic", "wdip_ic"))
class DipIcRenderer(PrismRenderer):
    """DIP IC (narrow / wide): the notched dual-row body with a pin-1 marker."""

    def _silhouette(self, pkg: _PkgPrism) -> PrismSilhouette | None:
        # Parameterized by the two pin columns; a degenerate layout (not two
        # columns) yields None → the plain bbox rectangle, same as before.
        return dip_silhouette(pkg)


@register_renderer(kind="sil")
class SilHeaderRenderer(PrismRenderer):
    """SIL header: the rounded housing hugging the single pin row."""

    def _silhouette(self, pkg: _PkgPrism) -> PrismSilhouette | None:
        return SilSilhouette()


@register_renderer(package_name="prism_smd")
class SmdChipRenderer(PrismRenderer):
    """SMD chip family: the base chip outline plus metallised terminal caps.

    The base :class:`PrismRenderer` draws every two-pin prism as the flat chip
    rectangle; this subclass — registered for the ``prism_smd`` generic that
    every catalogue chip family extends — overlays the real part's visible
    detail: the light metallised terminal band at each end of the body.
    """

    #: Metallised band length as a fraction of the body length (a real 0805's
    #: terminals are ~0.4 mm of a 2.0 mm body per end).
    CAP_FRAC = 0.2
    #: Light metallic band colour (high contrast on the dark chip body).
    CAP_COLOR = "#c8ccd2"

    #: Printed-code ink — the light silkscreen marking of a real chip.
    CODE_COLOR = "#e8e8e8"

    def render_body(
        self,
        positions: list[Point],
        fill: QColor,
        border: QColor,
        alpha: float,
        z: float,
        theme: Theme,
    ) -> list[QGraphicsItem]:
        items = super().render_body(positions, fill, border, alpha, z, theme)
        pkg = self._pkg
        if (not isinstance(pkg, _PkgPrism) or pkg.pin_count() != 2
                or len(positions) < 2):
            return items
        p1, p2 = positions[0], positions[1]
        if (p2 - p1).length() < 1e-9:
            return items

        extents = lead_axis_extents_or_none(pkg)
        if extents is None:
            return items  # no dims to size the caps — keep the base drawing
        body_len, body_w = extents
        cap = body_len * self.CAP_FRAC
        canvas = self.axis_canvas(positions[:2], p1, p2, fill, border, alpha,
                                  z, theme)
        canvas.track(items)
        canvas.setFillColor(self.CAP_COLOR)
        cap_z = canvas.zAbove()
        canvas.addRect((-body_len / 2.0, -body_w / 2.0),
                       (-body_len / 2.0 + cap, body_w / 2.0), z=cap_z)
        canvas.addRect((body_len / 2.0 - cap, -body_w / 2.0),
                       (body_len / 2.0, body_w / 2.0), z=cap_z)
        items.extend(canvas.items)

        label = self._code_label(p1, p2, body_len, body_w, cap, alpha, z)
        if label is not None:
            items.append(label)
        return items

    def _smd_code_text(self) -> str | None:
        """The digit code printed on this chip, or ``None`` (no marking).

        Decoded from the placed part's ``instance.value``: the resistance
        code for a resistor (``22k`` → ``223``), the µH inductance code for
        an inductor.  No instance, another class, or a value that does not
        parse → no marking.
        """
        if self._instance is None:
            return None
        value = parse_value(self._component_class, self._instance.value)
        if value is None:
            return None
        return smd_code(value)

    def _code_label(
        self,
        p1: Point,
        p2: Point,
        body_len: float,
        body_w: float,
        cap: float,
        alpha: float,
        z: float,
    ) -> QGraphicsItem | None:
        """The printed value code, scaled into the body between the caps."""
        code = self._smd_code_text()
        avail_len = body_len * 0.9 - 2.0 * cap
        if not code or avail_len <= 0:
            return None
        label = QGraphicsSimpleTextItem(code)
        ink = qcolor_from_css(self.CODE_COLOR)
        ink.setAlphaF(ink.alphaF() * alpha)
        label.setBrush(QBrush(ink))
        br = label.boundingRect()
        if br.width() < 1e-9 or br.height() < 1e-9:
            return None
        scale = min(avail_len / br.width(), body_w * 0.75 / br.height())
        # Along the pad axis, flipped upright when the axis points leftwards —
        # a chip code prints along the body and should never read upside-down.
        angle = (p2 - p1).angle_deg()
        if not -90.0 < angle <= 90.0:
            angle += 180.0 if angle <= 0 else -180.0
        t = QTransform()
        t.translate((p1.x + p2.x) / 2.0, (p1.y + p2.y) / 2.0)
        t.rotate(angle)
        t.scale(scale, scale)
        t.translate(-br.width() / 2.0, -br.height() / 2.0)
        label.setTransform(t)
        label.setZValue(z + 2 * BodyCanvas.Z_STEP)
        return label


@register_renderer(kind=("dip_switch", "pin_header", "jst"))
class PlainBodyRenderer(PrismRenderer):
    """Kinds whose declared look is a plain rectangle — no notch, no housing.

    ``dip_switch`` is the case the column guess *cannot* distinguish from a
    real DIP-IC (same two columns, deliberately plain); ``pin_header`` / ``jst``
    connectors carry their look in the per-kind fill colour instead.
    """

    def _silhouette(self, pkg: _PkgPrism) -> PrismSilhouette | None:
        return None
