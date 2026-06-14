# SPDX-License-Identifier: MIT
"""Tests for package shapes (Cylinder/Prism implementing Package).

Each test mirrors a worked example from ``docs/footprints/spec.md``: resistor
(axial solver), electrolytic (bend), TO-92 (body≠board via grid domain), DIP-8
(rigid multi-row), devboard (dims from pins + margin).
"""

import pytest

from protoboard.domain.components.package.offsets import (
    expand_offsets,
    scale_offsets,
)
from protoboard.domain.components.package.body import (
    DiscProfile,
    EdgeStyle,
    LeadArrangement,
)
from protoboard.domain.components.package.shapes import (
    BOARD_DEFAULT,
    BOARD_SOLVE,
    Cylinder,
    Disc,
    Leads,
    Package,
    Prism,
    SHAPE_REGISTRY,
)
from protoboard.domain.components.package.solver import MountOrientation
from protoboard.geometry import Insets, Point


def _pt(x: float, y: float) -> Point:
    return Point(float(x), float(y))


def _steps_mm(raw: list, unit: float) -> tuple[Point, ...]:
    return tuple(scale_offsets(expand_offsets(raw), unit))


class TestResistorAxial:
    """DIN0207: cylinder, body=0, board=solve. Reproduces spec 4/1/~28."""

    def _cyl(self) -> Cylinder:
        leads = Leads(
            direction=((0, 0, -1), (0, 0, 1)),
            body_offset_mm=(_pt(0, 0),),
            board_offset=BOARD_SOLVE,
            lead_length_mm=32.0,
            lead_r_mm=0.27,
        )
        return Cylinder(diameter_mm=2.5, length_mm=6.3, leads=leads)

    def test_is_package(self) -> None:
        assert isinstance(self._cyl(), Package)

    def test_horizontal_default_pin_spacing_continuous_mm(self) -> None:
        # Shape returns CONTINUOUS mm (physics domain); snap-to-hole (ceil) is
        # the consumer's job. r_min 7.92mm / 2.5 = 3.168 holes; consumer ceils
        # to 4 (spec's "4 rastry").
        import math

        pins = self._cyl().pin_positions(orientation=MountOrientation.HORIZONTAL)
        assert pins[0] == _pt(0, 0)
        assert pins[1].x == pytest.approx((6.3 + 6 * 0.27) / 2.5)  # 3.168
        assert math.ceil(pins[1].x) == 4

    def test_range_matches_solver(self) -> None:
        r = self._cyl().board_offset_range()
        assert r.r_min_mm == pytest.approx(6.3 + 6 * 0.27)
        assert r.r_max_mm == pytest.approx(7.92 + 2 * 32.0)


class TestElectrolyticRadial:
    """6.3x11: cylinder, body_offset=2.5mm, board=default, bend=4 holes."""

    def _cyl(self) -> Cylinder:
        leads = Leads(
            direction=((0, 0, -1), (0, 0, -1)),
            body_offset_mm=(_pt(2.5, 0),),   # 1 hole
            board_offset=BOARD_DEFAULT,
        )
        return Cylinder(diameter_mm=6.3, length_mm=11.0, leads=leads, bend_holes=4.0)

    def test_default_spacing_1_hole(self) -> None:
        pins = self._cyl().pin_positions()
        assert pins == [_pt(0, 0), _pt(1, 0)]  # 2.5mm/2.5 = 1 hole

    def test_bend_range_1_to_5(self) -> None:
        r = self._cyl().board_offset_range()
        # lower clamp = body_offset (2.5mm = 1 hole), upper = +4 holes (10mm)
        assert r.r_min_mm == pytest.approx(2.5)
        assert r.r_max_mm == pytest.approx(2.5 + 4 * 2.5)  # 12.5mm = 5 holes


class TestTO92:
    """3-pin prism, body 1.25mm inline, board=default → grid domain (1 hole)."""

    def _prism(self) -> Prism:
        leads = Leads(
            direction=((0, 0, -1), (0, 0, -1), (0, 0, -1)),
            body_offset_mm=_steps_mm([[1, 0], [1, 0]], 1.25),  # 1.25mm steps
            board_offset=BOARD_DEFAULT,
        )
        return Prism(leads=leads, width_mm=4.5, length_mm=4.5, height_mm=5.2,
                     bend_holes=1.4142 * 2.5 / 2.5)

    def test_three_pins(self) -> None:
        assert self._prism().pin_count() == 3

    def test_board_default_snaps_to_grid_1_hole(self) -> None:
        # body 1.25mm → board read in holes = 1.25/2.5 = 0.5 hole steps.
        pins = self._prism().pin_positions()
        assert pins[0] == _pt(0, 0)
        assert pins[1] == _pt(0.5, 0)   # grid-domain reading of body vector
        assert pins[2] == _pt(1.0, 0)


class TestDIP8:
    """Rigid multi-row: prism, board=default, bend=0."""

    def _prism(self) -> Prism:
        body = _steps_mm([[3, [0, 1]], [3, 0], [3, [0, -1]]], 2.5)
        leads = Leads(
            direction=((0, 0, -1),) * 8,
            body_offset_mm=body,
            board_offset=BOARD_DEFAULT,
        )
        return Prism(leads=leads, width_mm=9.8, length_mm=10.2, height_mm=4.0)

    def test_pin_positions_dip_topology(self) -> None:
        pins = self._prism().pin_positions()
        assert len(pins) == 8
        assert pins[0] == _pt(0, 0)
        assert pins[3] == _pt(0, 3)   # bottom of left column
        assert pins[4] == _pt(3, 3)   # column jump
        assert pins[7] == _pt(3, 0)   # beside pin 1

    def test_rigid_range_is_point(self) -> None:
        r = self._prism().board_offset_range()
        assert r.r_min_mm == r.r_max_mm  # bend=0 → no slack


class TestDevboardMargin:
    """dims from bbox(pins) + margin; margin=0 on pin side."""

    def _prism(self) -> Prism:
        # 2-pin SIL spanning some holes; margin CSS 2-value: V=0, H=2.5mm.
        leads = Leads(
            direction=((0, 0, -1), (0, 0, -1)),
            body_offset_mm=_steps_mm([[0, 4]], 2.5),  # pins 4 holes apart
            board_offset=BOARD_DEFAULT,
        )
        return Prism(leads=leads, margin_mm=Insets(0.0, 2.5, 0.0, 2.5))

    def test_bbox_from_pins_plus_margin(self) -> None:
        bb = self._prism().bbox()
        # pins span y 0..4 holes (height 4), x 0; margin top/bottom 0, l/r 1 hole.
        assert bb.height == pytest.approx(4.0)        # 0 margin vertical
        assert bb.width == pytest.approx(0.0 + 1.0 + 1.0)  # l+r margin in holes
        assert bb.x == pytest.approx(-1.0)            # shifted left by margin
        assert bb.y == pytest.approx(0.0)             # 0 margin on pin side


def _radial_leads(*, lead_r: float | None = 0.25) -> Leads:
    return Leads(
        direction=((0.0, 0.0, -1.0), (0.0, 0.0, -1.0)),
        body_offset_mm=(Point(2.5, 0.0),),
        board_offset=BOARD_DEFAULT,
        lead_r_mm=lead_r,
    )


class TestDisc:
    def test_disc_implements_package(self) -> None:
        d = Disc(diameter_mm=5.0, thickness_mm=2.0, leads=_radial_leads())
        assert isinstance(d, Package)
        assert d.pin_count() == 2

    def test_disc_defaults(self) -> None:
        d = Disc(diameter_mm=5.0, thickness_mm=2.0, leads=_radial_leads())
        assert d.profile is DiscProfile.FLAT
        assert d.arrangement is LeadArrangement.RADIAL  # discs mount radially

    def test_disc_pin_positions_and_bbox(self) -> None:
        d = Disc(diameter_mm=5.0, thickness_mm=2.0, leads=_radial_leads())
        assert d.pin_positions() == [_pt(0, 0), _pt(1, 0)]
        bb = d.bbox()
        assert (bb.width, bb.height) == (5.0 / 2.5, 5.0 / 2.5)

    def test_disc_rejects_solver(self) -> None:
        leads = Leads(
            direction=((0.0, 0.0, -1.0), (0.0, 0.0, -1.0)),
            body_offset_mm=(),
            board_offset=BOARD_SOLVE,
        )
        with pytest.raises(ValueError):
            Disc(diameter_mm=5.0, thickness_mm=2.0, leads=leads).pin_positions()


class TestMinPitch:
    def test_cylinder_pitches(self) -> None:
        # lead_d = 2·0.25 = 0.5 → radial (D+0.5)/2, axial L+0.5
        leads = Leads(
            direction=((0.0, 0.0, -1.0), (0.0, 0.0, 1.0)),
            body_offset_mm=(Point(10.0, 0.0),),
            board_offset=BOARD_DEFAULT,
            lead_r_mm=0.25,
        )
        c = Cylinder(diameter_mm=2.5, length_mm=6.3, leads=leads)
        assert c.min_radial_pitch_mm() == (2.5 + 0.5) / 2.0
        assert c.min_axial_pitch_mm() == 6.3 + 0.5

    def test_disc_pitches(self) -> None:
        d = Disc(diameter_mm=5.0, thickness_mm=2.0, leads=_radial_leads())
        assert d.min_radial_pitch_mm() == (5.0 + 0.5) / 2.0
        assert d.min_axial_pitch_mm() == 2.0 + 0.5

    def test_lead_diameter_defaults_when_radius_missing(self) -> None:
        # No lead_r → 0.5 mm default lead diameter (legacy TwoPinBodySpec).
        d = Disc(diameter_mm=5.0, thickness_mm=2.0, leads=_radial_leads(lead_r=None))
        assert d.min_radial_pitch_mm() == (5.0 + 0.5) / 2.0

    def test_prism_pitch_from_explicit_dims(self) -> None:
        leads = Leads(
            direction=((0.0, 0.0, -1.0), (0.0, 0.0, -1.0)),
            body_offset_mm=(Point(2.5, 0.0),),
            board_offset=BOARD_DEFAULT,
            lead_r_mm=0.25,
        )
        p = Prism(leads=leads, width_mm=7.62, length_mm=10.0)
        assert p.min_radial_pitch_mm() == (7.62 + 0.5) / 2.0
        assert p.min_axial_pitch_mm() == 10.0 + 0.5


class TestPresentation:
    def test_color_and_edge_carried(self) -> None:
        c = Cylinder(
            diameter_mm=2.5, length_mm=6.3, leads=_radial_leads(),
            color="#d6c08c", edge=EdgeStyle.SHARP,
        )
        assert c.color == "#d6c08c"
        assert c.edge is EdgeStyle.SHARP

    def test_color_default_none(self) -> None:
        c = Cylinder(diameter_mm=2.5, length_mm=6.3, leads=_radial_leads())
        assert c.color is None


class TestRegistry:
    def test_shape_registry(self) -> None:
        assert SHAPE_REGISTRY["cylinder"] is Cylinder
        assert SHAPE_REGISTRY["prism"] is Prism
        assert SHAPE_REGISTRY["disc"] is Disc
