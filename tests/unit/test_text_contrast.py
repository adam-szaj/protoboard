# SPDX-License-Identifier: MIT
"""Luminance-driven ink: palette by luminance, ink by best contrast."""

from __future__ import annotations

from PySide6.QtGui import QColor

from protoboard.gui.renderers.text_contrast import (
    DARK_INKS,
    LIGHT_INKS,
    best_ink,
    contrast_ratio,
    relative_luminance,
)


def _hex(c: QColor) -> str:
    return c.name().lower()


class TestLuminance:

    def test_white_is_one(self) -> None:
        assert relative_luminance((1.0, 1.0, 1.0)) == 1.0

    def test_black_is_zero(self) -> None:
        assert relative_luminance((0.0, 0.0, 0.0)) == 0.0

    def test_green_brighter_than_blue(self) -> None:
        assert relative_luminance((0.0, 1.0, 0.0)) > relative_luminance((0.0, 0.0, 1.0))


class TestContrastRatio:

    def test_white_black_is_21(self) -> None:
        assert contrast_ratio(1.0, 0.0) == 21.0


class TestBestInk:

    def test_light_body_gets_dark_ink(self) -> None:
        ink = _hex(best_ink(QColor("#f0f0f0")))
        assert ink in {h.lower() for h in DARK_INKS}

    def test_dark_body_gets_light_ink(self) -> None:
        ink = _hex(best_ink(QColor("#101010")))
        assert ink in {h.lower() for h in LIGHT_INKS}

    def test_saturated_blue_body_gets_light_ink(self) -> None:
        # Blue has low luminance -> treated as a dark body -> light ink.
        ink = _hex(best_ink(QColor("#0000ff")))
        assert ink in {h.lower() for h in LIGHT_INKS}

    def test_white_body_picks_black_ink(self) -> None:
        # Black is the highest-contrast dark ink on a white-ish body.
        assert _hex(best_ink(QColor("#ffffff"))) == "#111111"
