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

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QGraphicsItem

from protoboard.domain.components.package.shapes import (
    Prism as _PkgPrism,
)
from protoboard.domain.components.package.shapes import (
    lead_axis_extents,
)
from protoboard.geometry import Point
from protoboard.gui.renderers.packages.prism import PrismRenderer
from protoboard.gui.renderers.prism_silhouette import (
    PrismSilhouette,
    SilSilhouette,
    dip_silhouette,
)
from protoboard.gui.theme import Theme


class DipIcRenderer(PrismRenderer):
    """DIP IC (narrow / wide): the notched dual-row body with a pin-1 marker."""

    renders = ("dip_ic", "wdip_ic")

    def _silhouette(self, pkg: _PkgPrism) -> PrismSilhouette | None:
        # Parameterized by the two pin columns; a degenerate layout (not two
        # columns) yields None → the plain bbox rectangle, same as before.
        return dip_silhouette(pkg)


class SilHeaderRenderer(PrismRenderer):
    """SIL header: the rounded housing hugging the single pin row."""

    renders = "sil"

    def _silhouette(self, pkg: _PkgPrism) -> PrismSilhouette | None:
        return SilSilhouette()


class SmdChipRenderer(PrismRenderer):
    """SMD chip family: the base chip outline plus metallised terminal caps.

    The base :class:`PrismRenderer` draws every two-pin prism as the flat chip
    rectangle; this subclass — registered for the ``prism_smd`` generic that
    every catalogue chip family extends — overlays the real part's visible
    detail: the light metallised terminal band at each end of the body.
    """

    renders = "prism_smd"

    #: Metallised band length as a fraction of the body length (a real 0805's
    #: terminals are ~0.4 mm of a 2.0 mm body per end).
    CAP_FRAC = 0.2
    #: Light metallic band colour (high contrast on the dark chip body).
    CAP_COLOR = "#c8ccd2"

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

        body_len, body_w = lead_axis_extents(pkg)
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
        return items


class PlainBodyRenderer(PrismRenderer):
    """Kinds whose declared look is a plain rectangle — no notch, no housing.

    ``dip_switch`` is the case the column guess *cannot* distinguish from a
    real DIP-IC (same two columns, deliberately plain); ``pin_header`` / ``jst``
    connectors carry their look in the per-kind fill colour instead.
    """

    renders = ("dip_switch", "pin_header", "jst")

    def _silhouette(self, pkg: _PkgPrism) -> PrismSilhouette | None:
        return None
