# SPDX-License-Identifier: MIT
"""Tests for protoboard.units — RasterUnit, Millimeter and conversions."""

import math

import pytest

from protoboard.units import Millimeter, RasterUnit, to_mm, to_raster


class TestRasterUnit:
    def test_construct_from_float(self) -> None:
        r = RasterUnit(2.5)
        assert float(r) == 2.5

    def test_supports_negative(self) -> None:
        r = RasterUnit(-1.5)
        assert float(r) == -1.5

    def test_supports_zero(self) -> None:
        r = RasterUnit(0.0)
        assert float(r) == 0.0

    def test_arithmetic_behaves_like_float(self) -> None:
        a = RasterUnit(2.0)
        b = RasterUnit(0.5)
        assert float(a) + float(b) == pytest.approx(2.5)
        assert float(a) - float(b) == pytest.approx(1.5)


class TestMillimeter:
    def test_construct_from_float(self) -> None:
        m = Millimeter(2.54)
        assert float(m) == 2.54

    def test_supports_negative(self) -> None:
        m = Millimeter(-3.0)
        assert float(m) == -3.0


class TestConversions:
    def test_default_pitch_is_2_54(self) -> None:
        # 1 RasterUnit at default pitch should be 2.54 mm.
        result = to_mm(RasterUnit(1.0))
        assert float(result) == pytest.approx(2.54)

    def test_to_mm_with_explicit_pitch(self) -> None:
        result = to_mm(RasterUnit(2.0), pitch_mm=2.0)
        assert float(result) == pytest.approx(4.0)

    def test_to_raster_with_explicit_pitch(self) -> None:
        result = to_raster(Millimeter(5.08), pitch_mm=2.54)
        assert float(result) == pytest.approx(2.0)

    def test_roundtrip_raster_to_mm_to_raster(self) -> None:
        original = RasterUnit(3.7)
        roundtripped = to_raster(to_mm(original))
        assert float(roundtripped) == pytest.approx(float(original))

    def test_roundtrip_mm_to_raster_to_mm(self) -> None:
        original = Millimeter(7.62)
        roundtripped = to_mm(to_raster(original))
        assert float(roundtripped) == pytest.approx(float(original))

    def test_preserves_sign(self) -> None:
        result = to_mm(RasterUnit(-2.0))
        assert float(result) < 0
        assert float(result) == pytest.approx(-5.08)

    def test_zero_is_zero(self) -> None:
        assert float(to_mm(RasterUnit(0.0))) == 0.0
        assert float(to_raster(Millimeter(0.0))) == 0.0

    def test_fractional_raster(self) -> None:
        # 0.5 raster at 2.54 pitch = 1.27 mm
        result = to_mm(RasterUnit(0.5))
        assert float(result) == pytest.approx(1.27)

    def test_pitch_zero_raises(self) -> None:
        with pytest.raises(ValueError):
            to_raster(Millimeter(1.0), pitch_mm=0.0)

    def test_pitch_negative_raises(self) -> None:
        with pytest.raises(ValueError):
            to_mm(RasterUnit(1.0), pitch_mm=-2.54)

    def test_finite_values_required(self) -> None:
        with pytest.raises(ValueError):
            to_mm(RasterUnit(math.inf))
