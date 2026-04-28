# SPDX-License-Identifier: MIT
"""Tests for FootprintTemplate hierarchy: SingleRow, DualRow, GridFootprint
and aliases (DIP, SIL, TO92, TO220).

Coordinate convention: pin 1 lies at (0, 0). All other pins have
non-negative coordinates (for unrotated footprint). Rotation is applied
later in the Layout layer.
"""

import dataclasses

import pytest

from protoboard.domain.components.footprints import (
    DIP,
    SIL,
    TO92,
    TO220,
    DualRow,
    FootprintTemplate,
    GridFootprint,
    SingleRow,
)
from protoboard.geometry import Point


def _approx_point(a: Point, b: Point, tol: float = 1e-9) -> bool:
    return abs(a.x - b.x) <= tol and abs(a.y - b.y) <= tol


class TestSingleRow:
    def test_pin_count(self) -> None:
        fp = SingleRow(n=4)
        assert fp.pin_count() == 4

    def test_default_pitch(self) -> None:
        fp = SingleRow(n=4)
        assert fp.pitch == 1.0

    def test_pin_positions_unit_pitch(self) -> None:
        fp = SingleRow(n=4)
        positions = fp.pin_positions()
        # Pin 1 at (0, 0), pin 2 at (0, 1), ...
        assert len(positions) == 4
        assert _approx_point(positions[0], Point(0.0, 0.0))
        assert _approx_point(positions[1], Point(0.0, 1.0))
        assert _approx_point(positions[2], Point(0.0, 2.0))
        assert _approx_point(positions[3], Point(0.0, 3.0))

    def test_pin_positions_custom_pitch(self) -> None:
        fp = SingleRow(n=3, pitch=0.5)
        positions = fp.pin_positions()
        assert _approx_point(positions[0], Point(0.0, 0.0))
        assert _approx_point(positions[1], Point(0.0, 0.5))
        assert _approx_point(positions[2], Point(0.0, 1.0))

    def test_pin_positions_returns_list(self) -> None:
        # Per user decision: plain list[Point], indexed by pin_number - 1.
        fp = SingleRow(n=3)
        result = fp.pin_positions()
        assert isinstance(result, list)
        assert all(isinstance(p, Point) for p in result)

    def test_zero_pins_raises(self) -> None:
        with pytest.raises(ValueError):
            SingleRow(n=0)

    def test_negative_pins_raises(self) -> None:
        with pytest.raises(ValueError):
            SingleRow(n=-1)

    def test_zero_pitch_raises(self) -> None:
        with pytest.raises(ValueError):
            SingleRow(n=3, pitch=0.0)

    def test_negative_pitch_raises(self) -> None:
        with pytest.raises(ValueError):
            SingleRow(n=3, pitch=-1.0)

    def test_frozen(self) -> None:
        fp = SingleRow(n=3)
        with pytest.raises(dataclasses.FrozenInstanceError):
            fp.n = 4  # type: ignore[misc]


class TestDualRow:
    def test_dip8_pin_count(self) -> None:
        fp = DualRow(n_per_row=4)
        assert fp.pin_count() == 8

    def test_default_dimensions(self) -> None:
        fp = DualRow(n_per_row=4)
        assert fp.pitch == 1.0
        assert fp.rows_spacing == 3.0

    def test_dip8_pin_positions(self) -> None:
        # DIP-8: 1,2,3,4 down the left column (x=0); 5,6,7,8 up the right (x=3).
        # Pin 1 at (0, 0), pin 4 at (0, 3); pin 5 at (3, 3), pin 8 at (3, 0).
        fp = DualRow(n_per_row=4)
        positions = fp.pin_positions()
        assert len(positions) == 8
        assert _approx_point(positions[0], Point(0.0, 0.0))  # pin 1
        assert _approx_point(positions[1], Point(0.0, 1.0))  # pin 2
        assert _approx_point(positions[2], Point(0.0, 2.0))  # pin 3
        assert _approx_point(positions[3], Point(0.0, 3.0))  # pin 4
        assert _approx_point(positions[4], Point(3.0, 3.0))  # pin 5
        assert _approx_point(positions[5], Point(3.0, 2.0))  # pin 6
        assert _approx_point(positions[6], Point(3.0, 1.0))  # pin 7
        assert _approx_point(positions[7], Point(3.0, 0.0))  # pin 8

    def test_dip14_pin_positions_corners(self) -> None:
        fp = DualRow(n_per_row=7)
        positions = fp.pin_positions()
        assert len(positions) == 14
        assert _approx_point(positions[0], Point(0.0, 0.0))  # pin 1
        assert _approx_point(positions[6], Point(0.0, 6.0))  # pin 7
        assert _approx_point(positions[7], Point(3.0, 6.0))  # pin 8
        assert _approx_point(positions[13], Point(3.0, 0.0))  # pin 14

    def test_custom_rows_spacing(self) -> None:
        fp = DualRow(n_per_row=4, rows_spacing=5.0)
        positions = fp.pin_positions()
        assert _approx_point(positions[0], Point(0.0, 0.0))
        assert _approx_point(positions[7], Point(5.0, 0.0))

    def test_zero_per_row_raises(self) -> None:
        with pytest.raises(ValueError):
            DualRow(n_per_row=0)

    def test_negative_per_row_raises(self) -> None:
        with pytest.raises(ValueError):
            DualRow(n_per_row=-2)

    def test_invalid_pitch_raises(self) -> None:
        with pytest.raises(ValueError):
            DualRow(n_per_row=4, pitch=0.0)

    def test_invalid_rows_spacing_raises(self) -> None:
        with pytest.raises(ValueError):
            DualRow(n_per_row=4, rows_spacing=-1.0)


class TestGridFootprint:
    def test_pin_count(self) -> None:
        fp = GridFootprint(rows=3, cols=4)
        assert fp.pin_count() == 12

    def test_default_pitch(self) -> None:
        fp = GridFootprint(rows=2, cols=2)
        assert fp.pitch == 1.0

    def test_pin_positions_row_major(self) -> None:
        # Row-major numbering: pin 1 at (0,0), pin 2 at (1,0), ..., next row starts.
        fp = GridFootprint(rows=2, cols=3)
        positions = fp.pin_positions()
        assert len(positions) == 6
        assert _approx_point(positions[0], Point(0.0, 0.0))
        assert _approx_point(positions[1], Point(1.0, 0.0))
        assert _approx_point(positions[2], Point(2.0, 0.0))
        assert _approx_point(positions[3], Point(0.0, 1.0))
        assert _approx_point(positions[4], Point(1.0, 1.0))
        assert _approx_point(positions[5], Point(2.0, 1.0))

    def test_zero_dimensions_raise(self) -> None:
        with pytest.raises(ValueError):
            GridFootprint(rows=0, cols=2)
        with pytest.raises(ValueError):
            GridFootprint(rows=2, cols=0)


class TestAliases:
    def test_dip_factory(self) -> None:
        fp = DIP(8)
        assert isinstance(fp, DualRow)
        assert fp.pin_count() == 8
        assert fp.n_per_row == 4

    def test_dip_with_custom_pitch(self) -> None:
        fp = DIP(8, pitch=2.0)
        assert fp.pitch == 2.0

    def test_dip_with_custom_rows_spacing(self) -> None:
        # Wide DIP (e.g. DIP-28 wide).
        fp = DIP(28, rows_spacing=6.0)
        assert fp.rows_spacing == 6.0

    def test_dip_odd_pin_count_raises(self) -> None:
        with pytest.raises(ValueError):
            DIP(7)

    def test_dip_zero_raises(self) -> None:
        with pytest.raises(ValueError):
            DIP(0)

    def test_sil_factory(self) -> None:
        fp = SIL(4)
        assert isinstance(fp, SingleRow)
        assert fp.pin_count() == 4

    def test_sil_with_custom_pitch(self) -> None:
        fp = SIL(4, pitch=0.5)
        assert fp.pitch == 0.5

    def test_to92_is_sil3(self) -> None:
        # TO92 / TO220 are constants (not factories) — same connectivity as SIL(3).
        assert isinstance(TO92, SingleRow)
        assert TO92.pin_count() == 3

    def test_to220_is_sil3(self) -> None:
        assert isinstance(TO220, SingleRow)
        assert TO220.pin_count() == 3


class TestFootprintTemplateProtocol:
    def test_all_implement_pin_count_and_positions(self) -> None:
        for fp in (
            SingleRow(n=4),
            DualRow(n_per_row=3),
            GridFootprint(rows=2, cols=2),
        ):
            assert isinstance(fp, FootprintTemplate)
            assert fp.pin_count() == len(fp.pin_positions())

    def test_pin1_at_origin(self) -> None:
        # Reaffirm the origin convention for every footprint type.
        for fp in (
            SingleRow(n=3),
            DualRow(n_per_row=3),
            GridFootprint(rows=2, cols=2),
        ):
            positions = fp.pin_positions()
            assert _approx_point(positions[0], Point(0.0, 0.0))

    def test_all_pins_non_negative_for_unrotated(self) -> None:
        for fp in (
            SingleRow(n=4),
            DualRow(n_per_row=3, rows_spacing=3.0),
            GridFootprint(rows=2, cols=3),
        ):
            for p in fp.pin_positions():
                assert p.x >= -1e-9
                assert p.y >= -1e-9
