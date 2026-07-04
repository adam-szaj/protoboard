# SPDX-License-Identifier: MIT
"""Colour-band renderer for DIN axial bodies — resistors and small inductors.

Registered for the DIN ceramic-shell families (``DIN-0204`` … ``DIN-0922``),
the bodies resistors ship in — and that small axial inductors reuse (the
builtin ``L`` part references ``DIN-0207/typ``).  The bands are decoded from
the placed part's ``instance.value``:

* ``component_class: resistor`` — the IEC 60062 resistance code (ohms);
* ``component_class: inductor`` — the inductance code (µH), on the
  inductor's own body colour (a moulded green, not the resistor beige the
  shared DIN family declares);
* any other class in a DIN body, an unbound preview, or a value that does
  not decode (``"?"``, text) — the plain base body, no bands.

The code group (two or three significant digits + the multiplier) reads from
the pin-1 end; a gold tolerance band sits at the other end (tolerance is not
part of ``value``, so the canonical ±5 % gold is drawn).  Bands are visible
only in the HORIZONTAL mount — the flat side view between the pads; a part
standing upright (pad spacing shorter than the body) shows its top-view disc,
where a band code cannot be seen, so none is drawn.
"""

from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QGraphicsItem

from protoboard.domain.components.definition import ComponentClass
from protoboard.domain.components.package.shapes import Cylinder
from protoboard.geometry import Point
from protoboard.gui.renderers.packages import (
    CylinderRenderer,
    qcolor_from_css,
    register_renderer,
)
from protoboard.gui.renderers.value_codes import (
    code_digits,
    parse_inductance,
    parse_resistance,
)
from protoboard.gui.theme import Theme

# ---------------------------------------------------------------------------
# The IEC 60062 colour code
# ---------------------------------------------------------------------------

#: Significant-digit → band colour (0-9).
DIGIT_COLORS: dict[int, str] = {
    0: "#000000",  # black
    1: "#8b5a2b",  # brown
    2: "#d0342c",  # red
    3: "#ff8c1a",  # orange
    4: "#f2ce1b",  # yellow
    5: "#2f8f2f",  # green
    6: "#3667c8",  # blue
    7: "#8a2be2",  # violet
    8: "#8c8c8c",  # grey
    9: "#ffffff",  # white
}

#: Multiplier exponent → band colour (10^-2 silver … 10^9 white).
MULT_COLORS: dict[int, str] = {
    -2: "#c0c0c0",  # silver
    -1: "#d4af37",  # gold
    **DIGIT_COLORS,
}

#: The default tolerance band (±5 % gold) — ``value`` carries no tolerance.
TOLERANCE_COLOR = "#d4af37"


def code_bands(value: float) -> list[str] | None:
    """The code-group band colours for *value*: digits + the multiplier.

    Colour projection of :func:`code_digits` — ``None`` when the value does
    not express as a 2/3-digit code (an odd value draws no code rather than
    a wrong one).
    """
    code = code_digits(value)
    if code is None:
        return None
    digits, mult = code
    return [DIGIT_COLORS[d] for d in digits] + [MULT_COLORS[mult]]


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

@register_renderer(family=(
    "DIN-0204", "DIN-0207", "DIN-0411", "DIN-0617", "DIN-0922",
))
class DinColorBandRenderer(CylinderRenderer):
    """DIN axial body with the value decoded into colour bands."""

    #: Moulded-inductor body colour — the shared DIN families declare the
    #: resistor beige, so an inductor (no explicit style override) gets its
    #: real-world green shell instead.
    INDUCTOR_BODY_COLOR = "#7fa06a"

    #: Band width / gap as fractions of the body length.
    BAND_FRAC = 0.10
    GAP_FRAC = 0.06

    def _resolve_colors(
        self, fill: QColor, border: QColor,
    ) -> tuple[QColor, QColor]:
        fill, border = super()._resolve_colors(fill, border)
        if (self._component_class == ComponentClass.INDUCTOR
                and self._appearance.color is None):
            body = qcolor_from_css(self.INDUCTOR_BODY_COLOR)
            return body, body.darker(150)
        return fill, border

    def _band_colors(self) -> list[str] | None:
        """The code group for the placed part, or ``None`` (no bands)."""
        if self._instance is None:
            return None
        if self._component_class == ComponentClass.RESISTOR:
            value = parse_resistance(self._instance.value)
        elif self._component_class == ComponentClass.INDUCTOR:
            value = parse_inductance(self._instance.value)
        else:
            return None
        if value is None:
            return None
        return code_bands(value)

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
        code = self._band_colors()
        pkg = self._pkg
        if code is None or not isinstance(pkg, Cylinder) or len(positions) < 2:
            return items

        mm = theme.raster_pitch_mm
        body_len = pkg.length_mm / mm
        body_w = pkg.diameter_mm / mm
        # Same anchor-end swap as the base body, so the code group mirrors
        # with the part exactly like the cathode stripe does.
        if self._vertical_flipped:
            p1, p2 = positions[1], positions[0]
        else:
            p1, p2 = positions[0], positions[1]
        dist = (p2 - p1).length()
        # Bands are visible only on the HORIZONTAL body (the side view). An
        # upright part (pad spacing shorter than the body) draws its top-view
        # disc, and a degenerate spacing draws the bbox — neither shows a code.
        if dist < 1e-9 or dist < body_len - 1e-6:
            return items

        # Keep every band on the straight segment of the capsule, clear of the
        # rounded end caps (the cathode-stripe rule).
        edge_r = min(pkg.edge_radius_mm / mm, body_w / 2.0, body_len / 2.0)
        half = body_len / 2.0 - edge_r
        band_w = body_len * self.BAND_FRAC
        gap = body_len * self.GAP_FRAC

        canvas = self.axis_canvas(positions[:2], p1, p2, fill, border, alpha,
                                  z, theme)
        # Code group from the pin-1 end…
        x = -half + gap
        for color in code:
            canvas.addRect((x, -body_w / 2.0), (x + band_w, body_w / 2.0),
                           fill=color, pen_width=0.0, z_offset=0.01)
            x += band_w + gap
        # …and the tolerance band at the far end.
        canvas.addRect((half - gap - band_w, -body_w / 2.0),
                       (half - gap, body_w / 2.0),
                       fill=TOLERANCE_COLOR, pen_width=0.0, z_offset=0.01)
        items.extend(canvas.items)
        return items
