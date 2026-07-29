# SPDX-License-Identifier: MIT
"""Pin-down parity tests for the axis-based geometry model migration.

Every expected value below is a LITERAL captured from the implementation
before the migration (Phase 0 of the plan). The swap→transform refactor
(``Prism.bbox`` / ``lead_axis_extents`` → ``local_size`` + ``body_frame``)
must reproduce these numbers exactly (tight float tolerance only).

Helpers ``assert_rect`` / ``along_across`` are the single indirection the
later phases update — the assertions themselves stay untouched.
"""

from __future__ import annotations

import pytest

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.components.package.body import LeadArrangement
from protoboard.domain.components.package.shapes import (
    Cylinder,
    Disc,
    Leads,
    Package,
    Prism,
    local_size_holes,
)
from protoboard.geometry import (
    Insets,
    Point,
    Rect,
    Size3D,
    Vector,
    Vector3D,
    flip_reflection_from_pins,
)

_DOWN = Vector3D(0.0, 0.0, -1.0)
_TOL = 1e-9


def _leads(*steps: tuple[float, float]) -> Leads:
    """Leads with explicit board_offset steps (holes); body_offset mirrors
    the grid steps in mm so BOARD_DEFAULT-independent fixtures stay simple."""
    return Leads(
        direction=tuple(_DOWN for _ in range(len(steps) + 1)),
        body_offset_mm=tuple(
            Vector(sx * 2.54, sy * 2.54) for sx, sy in steps),
        board_offset=tuple(Vector(sx, sy) for sx, sy in steps),
    )


def assert_rect(r: Rect, x: float, y: float, lx: float, ly: float) -> None:
    """Compare a body/board rectangle against (min-corner x, y, x-extent,
    y-extent). Single seam updated when Rect's fields are renamed."""
    assert r.x == pytest.approx(x, abs=_TOL)
    assert r.y == pytest.approx(y, abs=_TOL)
    assert r.lx == pytest.approx(lx, abs=_TOL)
    assert r.ly == pytest.approx(ly, abs=_TOL)


def along_across(package: Package) -> tuple[float, float]:
    """Body extents (along, across) the lead axis, in holes. Phase 3
    re-pointed this seam from ``lead_axis_extents`` at ``local_size_holes``."""
    s = local_size_holes(package)
    return s.lx, s.ly


# ---------------------------------------------------------------------------
# Prism
# ---------------------------------------------------------------------------


class TestPrismBbox:
    def test_pin_row_along_x(self) -> None:
        # length (10 mm → 4.0 holes) lands on x, width (6 mm → 2.4) on y.
        p = Prism(leads=_leads((3.0, 0.0)), size_mm=Size3D(10.0, 6.0))
        assert_rect(p.bbox(), -0.5, -1.2, 4.0, 2.4)
        assert along_across(p) == pytest.approx((4.0, 2.4), abs=_TOL)

    def test_pin_row_along_y(self) -> None:
        # Same part rotated: length on y, width on x.
        p = Prism(leads=_leads((0.0, 3.0)), size_mm=Size3D(10.0, 6.0))
        assert_rect(p.bbox(), -1.2, -0.5, 2.4, 4.0)
        assert along_across(p) == pytest.approx((4.0, 2.4), abs=_TOL)

    def test_square_pin_cloud_tie_keeps_along_y(self) -> None:
        p = Prism(
            leads=_leads((3.0, 0.0), (-3.0, 3.0), (3.0, 0.0)),
            size_mm=Size3D(10.0, 6.0),
        )
        assert_rect(p.bbox(), 0.3, -0.5, 2.4, 4.0)
        assert along_across(p) == pytest.approx((4.0, 2.4), abs=_TOL)

    def test_body_offset_shifts_centre(self) -> None:
        p = Prism(
            leads=_leads((3.0, 0.0)),
            size_mm=Size3D(10.0, 6.0),
            body_offset_mm=Vector(1.0, 2.0),
        )
        assert_rect(p.bbox(), -0.1, -0.4, 4.0, 2.4)

    def test_margin_derived_bbox_is_world_aligned(self) -> None:
        p = Prism(
            leads=_leads((3.0, 0.0), (-3.0, 2.0), (3.0, 0.0)),
            margin_mm=Insets(top=1.0, right=2.0, bottom=3.0, left=4.0),
        )
        assert_rect(p.bbox(), -1.6, -0.4, 5.4, 3.6)
        assert along_across(p) == pytest.approx((5.4, 3.6), abs=_TOL)

    def test_pitch_fallbacks(self) -> None:
        explicit = Prism(
            leads=_leads((3.0, 0.0)), size_mm=Size3D(10.0, 6.0))
        assert explicit.min_radial_pitch_mm() == pytest.approx(3.25, abs=_TOL)
        assert explicit.min_axial_pitch_mm() == pytest.approx(10.5, abs=_TOL)
        margin = Prism(
            leads=_leads((3.0, 0.0), (-3.0, 2.0), (3.0, 0.0)),
            margin_mm=Insets(top=1.0, right=2.0, bottom=3.0, left=4.0),
        )
        # Margin-derived fallbacks read the WORLD bbox today (width=x,
        # height=y) — pinned as-is; Phase 3 documents any deliberate change.
        assert margin.min_radial_pitch_mm() == pytest.approx(7.0, abs=_TOL)
        assert margin.min_axial_pitch_mm() == pytest.approx(9.5, abs=_TOL)


# ---------------------------------------------------------------------------
# Cylinder
# ---------------------------------------------------------------------------


class TestCylinderBbox:
    def test_radial_is_diameter_square(self) -> None:
        c = Cylinder(
            diameter_mm=5.0,
            length_mm=5.0,
            leads=_leads((1.0, 0.0)),
            arrangement=LeadArrangement.RADIAL,
        )
        assert_rect(c.bbox(), -0.5, -1.0, 2.0, 2.0)
        assert along_across(c) == pytest.approx((2.0, 2.0), abs=_TOL)

    def test_axial_along_x(self) -> None:
        c = Cylinder(diameter_mm=2.5, length_mm=8.0, leads=_leads((4.0, 0.0)))
        assert_rect(c.bbox(), 0.4, -0.5, 3.2, 1.0)
        assert along_across(c) == pytest.approx((3.2, 1.0), abs=_TOL)

    def test_axial_along_y(self) -> None:
        c = Cylinder(diameter_mm=2.5, length_mm=8.0, leads=_leads((0.0, 4.0)))
        assert_rect(c.bbox(), -0.5, 0.4, 1.0, 3.2)
        assert along_across(c) == pytest.approx((3.2, 1.0), abs=_TOL)

    def test_axial_diagonal_axis(self) -> None:
        # 3-4-5 axis: AABB projects the oriented L×d capsule rectangle.
        c = Cylinder(diameter_mm=2.5, length_mm=8.0, leads=_leads((3.0, 4.0)))
        assert_rect(c.bbox(), 0.14, 0.42, 2.72, 3.16)

    def test_radial_with_eccentricity(self) -> None:
        c = Cylinder(
            diameter_mm=5.0,
            length_mm=5.0,
            leads=Leads(
                direction=(_DOWN, _DOWN),
                body_offset_mm=(Vector(2.54, 0.0),),
                board_offset=(Vector(1.0, 0.0),),
                body_eccentricity_mm=Vector(1.27, 0.0),
            ),
            arrangement=LeadArrangement.RADIAL,
        )
        assert_rect(c.bbox(), 0.008, -1.0, 2.0, 2.0)


# ---------------------------------------------------------------------------
# Disc
# ---------------------------------------------------------------------------


class TestDiscBbox:
    def test_axis_along_x(self) -> None:
        # Lens: diameter (5 mm → 2.0) along the lead axis, thickness
        # (2 mm → 0.8) across it.
        d = Disc(diameter_mm=5.0, thickness_mm=2.0, leads=_leads((1.0, 0.0)))
        assert_rect(d.bbox(), -0.5, -0.4, 2.0, 0.8)
        assert along_across(d) == pytest.approx((2.0, 0.8), abs=_TOL)

    def test_axis_diagonal(self) -> None:
        d = Disc(diameter_mm=5.0, thickness_mm=2.0, leads=_leads((3.0, 4.0)))
        assert_rect(d.bbox(), 0.58, 0.96, 1.84, 2.08)


class TestDiagonalAlongAcross:
    """DELIBERATE Phase-3 change: on a DIAGONAL axis the old
    ``lead_axis_extents`` returned world-AABB extents (rotation-inflated:
    cylinder (3.16, 2.72), disc (2.08, 1.84)); ``local_size_holes`` returns
    the TRUE local body dims regardless of placement angle.
    """

    def test_cylinder_diagonal_true_local_dims(self) -> None:
        c = Cylinder(diameter_mm=2.5, length_mm=8.0, leads=_leads((3.0, 4.0)))
        assert along_across(c) == pytest.approx((3.2, 1.0), abs=_TOL)

    def test_disc_diagonal_true_local_dims(self) -> None:
        d = Disc(diameter_mm=5.0, thickness_mm=2.0, leads=_leads((3.0, 4.0)))
        assert along_across(d) == pytest.approx((2.0, 0.8), abs=_TOL)


# ---------------------------------------------------------------------------
# Flip reflection
# ---------------------------------------------------------------------------


class TestFlipReflection:
    def test_row_along_x_reflects_y(self) -> None:
        t = flip_reflection_from_pins([Point(0.0, 0.0), Point(3.0, 0.0)])
        assert t.coeffs() == pytest.approx(
            (1.0, 0.0, 0.0, -1.0, 0.0, 0.0), abs=_TOL)

    def test_row_along_y_reflects_x(self) -> None:
        t = flip_reflection_from_pins([Point(0.0, 0.0), Point(0.0, 3.0)])
        assert t.coeffs() == pytest.approx(
            (-1.0, 0.0, 0.0, 1.0, 0.0, 0.0), abs=_TOL)

    def test_square_cloud_tie_reflects_y_about_centre(self) -> None:
        pins = [
            Point(0.0, 0.0),
            Point(3.0, 0.0),
            Point(0.0, 3.0),
            Point(3.0, 3.0),
        ]
        t = flip_reflection_from_pins(pins)
        assert t.coeffs() == pytest.approx(
            (1.0, 0.0, 0.0, -1.0, 0.0, 3.0), abs=_TOL)


# ---------------------------------------------------------------------------
# Board bounds
# ---------------------------------------------------------------------------


class TestBoardBounds:
    def _board_4x7(self) -> Board:
        pad = PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(),
        )
        grid = BoardGrid(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            cell=pad,
            rows=4,
            cols=7,
            row_gap=0.0,
            col_gap=0.0,
        )
        return Board(root=grid)

    def test_bounds_top_left_origin(self) -> None:
        board = self._board_4x7()
        assert_rect(board.bounds, 0.0, 0.0, 7.0, 4.0)

    def test_size_mm(self) -> None:
        board = self._board_4x7()
        s = board.size_mm
        assert s.lx == pytest.approx(17.78, abs=_TOL)
        assert s.ly == pytest.approx(10.16, abs=_TOL)
