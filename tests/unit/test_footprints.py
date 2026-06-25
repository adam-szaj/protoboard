# SPDX-License-Identifier: MIT
"""Tests for the Footprint API: DIL, SIL factories and TO92/TO220 constants.

A Footprint is now only pin count + pin positions. Concrete per-package
types (SingleRow/DualRow/GridFootprint/...) were removed; the surviving
public surface is the `DIL`/`SIL` factories, the `TO92`/`TO220` constants,
the `Footprint` ABC and `GenericFootprint`.

Coordinate convention: pin 1 lies at (0, 0). All other pins have
non-negative coordinates (for an unrotated footprint). Rotation is applied
later in the Layout layer.
"""

import pytest

from protoboard.domain.components.footprints import (
    DIL,
    SIL,
    TO92,
    TO220,
    Footprint,
    GenericFootprint,
)
from protoboard.geometry import Point


def _approx_point(a: Point, b: Point, tol: float = 1e-9) -> bool:
    return abs(a.x - b.x) <= tol and abs(a.y - b.y) <= tol


class TestSIL:
    def test_pin_count(self) -> None:
        fp = SIL(4)
        assert fp.pin_count() == 4

    def test_pin_positions_unit_pitch(self) -> None:
        fp = SIL(4)
        positions = fp.pin_positions()
        # Pin 1 at (0, 0), pin 2 at (0, 1), ...
        assert len(positions) == 4
        assert _approx_point(positions[0], Point(0.0, 0.0))
        assert _approx_point(positions[1], Point(0.0, 1.0))
        assert _approx_point(positions[2], Point(0.0, 2.0))
        assert _approx_point(positions[3], Point(0.0, 3.0))

    def test_pin_positions_custom_pitch(self) -> None:
        fp = SIL(3, pitch=0.5)
        positions = fp.pin_positions()
        assert _approx_point(positions[0], Point(0.0, 0.0))
        assert _approx_point(positions[1], Point(0.0, 0.5))
        assert _approx_point(positions[2], Point(0.0, 1.0))

    def test_pin_positions_returns_list(self) -> None:
        # Per user decision: plain list[Point], indexed by pin_number - 1.
        fp = SIL(3)
        result = fp.pin_positions()
        assert isinstance(result, list)
        assert all(isinstance(p, Point) for p in result)


class TestDIL:
    def test_dip8_pin_count(self) -> None:
        fp = DIL(n=8)
        assert fp.pin_count() == 8

    def test_dip8_pin_positions(self) -> None:
        # DIP-8: 1,2,3,4 down the left column (x=0); 5,6,7,8 up the right (x=3).
        # Pin 1 at (0, 0), pin 4 at (0, 3); pin 5 at (3, 3), pin 8 at (3, 0).
        fp = DIL(n=8)
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
        fp = DIL(n=14)
        positions = fp.pin_positions()
        assert len(positions) == 14
        assert _approx_point(positions[0], Point(0.0, 0.0))  # pin 1
        assert _approx_point(positions[6], Point(0.0, 6.0))  # pin 7
        assert _approx_point(positions[7], Point(3.0, 6.0))  # pin 8
        assert _approx_point(positions[13], Point(3.0, 0.0))  # pin 14

    def test_custom_rows_spacing(self) -> None:
        fp = DIL(n=8, rows_spacing=5.0)
        positions = fp.pin_positions()
        assert _approx_point(positions[0], Point(0.0, 0.0))
        assert _approx_point(positions[7], Point(5.0, 0.0))

    def test_odd_pin_count_raises(self) -> None:
        with pytest.raises(ValueError):
            DIL(n=7)

    def test_zero_raises(self) -> None:
        with pytest.raises(ValueError):
            DIL(n=0)


class TestConstants:
    def test_to92_is_sil3(self) -> None:
        # TO92 / TO220 are constants (not factories) — same connectivity as SIL(3).
        assert TO92.pin_count() == 3
        positions = TO92.pin_positions()
        assert _approx_point(positions[0], Point(0.0, 0.0))
        assert _approx_point(positions[2], Point(0.0, 2.0))

    def test_to220_is_sil3(self) -> None:
        assert TO220.pin_count() == 3


class TestFootprintProtocol:
    def test_factories_produce_footprints(self) -> None:
        for fp in (
            SIL(4),
            DIL(n=6),
            GenericFootprint([]),
        ):
            assert isinstance(fp, Footprint)
            assert fp.pin_count() == len(fp.pin_positions())

    def test_pin1_at_origin(self) -> None:
        # Reaffirm the origin convention for every footprint.
        for fp in (
            SIL(3),
            DIL(n=6),
        ):
            positions = fp.pin_positions()
            assert _approx_point(positions[0], Point(0.0, 0.0))

    def test_all_pins_non_negative_for_unrotated(self) -> None:
        for fp in (
            SIL(4),
            DIL(n=6, rows_spacing=3.0),
        ):
            for p in fp.pin_positions():
                assert p.x >= -1e-9
                assert p.y >= -1e-9
