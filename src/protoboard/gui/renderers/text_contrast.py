# SPDX-License-Identifier: MIT
"""Ink colour for body-printed text, chosen from the body's luminance.

WCAG relative luminance picks the palette (light body → a dark ink, dark body
→ a light ink); the highest WCAG contrast ratio picks the ink within it.  The
two-stage rule is correctness-by-construction: a dark body can never receive a
dark ink regardless of arithmetic.  Palettes are plain hex lists, tunable in
one line.  The luminance / contrast core is Qt-free ``(r, g, b)`` math; only
``best_ink`` touches ``QColor`` (a value type) at the boundary.
"""

from __future__ import annotations

from PySide6.QtGui import QColor

#: Inks for light bodies (black, graphite, navy).
DARK_INKS: list[str] = ["#000000", "#2b2b2b", "#0d1b3e"]
#: Inks for dark bodies (white, light-grey, yellow).
LIGHT_INKS: list[str] = ["#ffffff", "#c8c8c8", "#ffd400"]

_LUMA_THRESHOLD = 0.5


def _chan_lin(c: float) -> float:
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb: tuple[float, float, float]) -> float:
    """WCAG relative luminance for sRGB channels in 0..1."""
    r, g, b = (_chan_lin(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(l1: float, l2: float) -> float:
    """WCAG contrast ratio between two relative luminances."""
    hi, lo = (l1, l2) if l1 >= l2 else (l2, l1)
    return (hi + 0.05) / (lo + 0.05)


def _rgb01(c: QColor) -> tuple[float, float, float]:
    return (c.redF(), c.greenF(), c.blueF())


def best_ink(body_color: QColor) -> QColor:
    """The highest-contrast ink for *body_color* from the luminance palette."""
    body_lum = relative_luminance(_rgb01(body_color))
    palette = DARK_INKS if body_lum > _LUMA_THRESHOLD else LIGHT_INKS
    best = max(
        palette,
        key=lambda hexs: contrast_ratio(
            body_lum, relative_luminance(_rgb01(QColor(hexs)))
        ),
    )
    return QColor(best)
