# SPDX-License-Identifier: MIT
"""Tests for reach.py — the Package-derived replacement for bendable_pins."""

from protoboard.domain.components.package import load_packages
from protoboard.domain.components.package.reach import (
    is_pin_bendable,
    package_to_bend_specs,
    pin_reaches,
)
from protoboard.domain.components.package.shapes import Package
from protoboard.domain.components.package.solver import RASTER_MM
from protoboard.geometry import Point, Vector

# Self-contained package catalogue for reach tests — the abstract bases plus a
# few representative parts (axial resistor, radial electrolytic, rigid DIP, a
# 3-pin bendable prism).  Inline so the tests own their geometry and do not
# depend on which named packages happen to live in the builtin generic.yaml
# (placed parts resolve through the package-family catalogue, not these).
_CATALOGUE: list[dict] = [
    {"name": "cylinder", "abstract": True, "shape": "cylinder",
     "dims": {"diameter": None, "length": None, "edge_radius": 0.0}},
    {"name": "prism", "abstract": True, "shape": "prism"},
    {"name": "cylinder_axial", "abstract": True, "extends": "cylinder",
     "leads": {"direction": [[0, 0, -1], [0, 0, 1]], "body_offset": 0,
               "board_offset": "solve", "lead_length": None, "lead_r": None,
               "body_eccentricity": 0.0}},
    {"name": "cylinder_radial", "abstract": True, "extends": "cylinder",
     "arrangement": "radial",
     "leads": {"direction": [[0, 0, -1], [0, 0, -1]],
               "body_offset": {"unit": 2.5, "value": [[1, 0]]},
               "board_offset": "default"}, "bend": 0.0},
    {"name": "prism_dil", "abstract": True, "extends": "prism",
     "leads": {"direction": [0, 0, -1], "board_offset": "default"},
     "bend": 0.0},
    # Axial resistor body (DIN 0207): solver-driven reach.
    {"name": "R_Axial_DIN0207", "extends": "cylinder_axial",
     "dims": {"diameter": 2.5, "length": 6.3},
     "leads": {"lead_r": 0.27, "lead_length": 32.0}},
    # Radial electrolytic 6.3x11: bend ring 1..5 holes.
    {"name": "CP_Elec_6.3x11", "extends": "cylinder_radial",
     "dims": {"diameter": 6.3, "length": 11.0},
     "leads": {"body_offset": {"unit": 2.5, "value": [[1, 0]]}}, "bend": 4.0},
    # DIP-8: rigid, two rows of 4 at 0.3in.
    {"name": "DIP-8", "extends": "prism_dil",
     "dims": {"width": 6.25, "length": 9.20, "height": 4.0},
     "leads": {"body_offset": {"unit": 2.5,
               "value": [[3, [0, 1]], [3, 0], [3, [0, -1]]]}}},
    # A 3-pin bendable prism (TO-92-like): each lead bends about its own home.
    {"name": "BEND3", "extends": "prism",
     "dims": {"width": 4.5, "length": 4.5, "height": 5.2},
     "leads": {"direction": [[0, 0, -1], [0, 0, -1], [0, 0, -1]],
               "body_offset": {"unit": 2.5, "value": [[1, 0], [1, 0]]},
               "board_offset": "default"}, "bend": 3.54},
]


def _builtin() -> dict:
    return load_packages(_CATALOGUE)


def _bendable_3pin() -> Package:
    return _builtin()["BEND3"]


class TestPinReaches:
    def test_all_pins_present_including_pin1(self) -> None:
        # The ORIGIN is fixed; every pin (incl. pin 1) may bend its lead.
        reaches = pin_reaches(_builtin()["CP_Elec_6.3x11"])
        assert 1 in reaches
        assert 2 in reaches

    def test_default_is_pin_home_origin_is_zero(self) -> None:
        c = _builtin()["CP_Elec_6.3x11"]
        reaches = pin_reaches(c)
        assert reaches[2].default == c.pin_positions()[1]
        assert reaches[2].origin == Point(0.0, 0.0)   # fixed origin
        assert reaches[1].origin == Point(0.0, 0.0)

    def test_electrolytic_reachable_within_bend(self) -> None:
        c = _builtin()["CP_Elec_6.3x11"]  # ring 1..5 holes from the pin's default
        reach = pin_reaches(c)[2]
        d = reach.default  # pin 2 home hole = (1, 0); the disc is centred here
        # measured from the pin's own default hole:
        assert reach.is_reachable(Point(d.x + 5.0, d.y), raster_mm=RASTER_MM) is True   # 5 holes = r_max
        assert reach.is_reachable(Point(d.x + 1.0, d.y), raster_mm=RASTER_MM) is True   # 1 hole = r_min
        assert reach.is_reachable(d, raster_mm=RASTER_MM) is False                      # on the default (< r_min)
        assert reach.is_reachable(Point(d.x + 6.0, d.y), raster_mm=RASTER_MM) is False  # beyond r_max

    def test_disc_centred_on_pin_default(self) -> None:
        # Reach is a disc centred on the pin's OWN default hole, not the origin.
        c = _builtin()["CP_Elec_6.3x11"]
        reach = pin_reaches(c)[2]
        d = reach.default                       # (1, 0), not the origin (0, 0)
        assert d == Point(1.0, 0.0)
        # A point r_max away from the default is reachable in any direction…
        assert reach.is_reachable(Point(d.x, d.y + 5.0), raster_mm=RASTER_MM) is True
        # (6, 0) is exactly r_max (5 holes) from the default (1,0) → reachable,
        # but 6 holes from the origin (0,0) → it would be OUT under the old
        # origin-centred model. Proves the centre moved to the pin default.
        assert reach.is_reachable(Point(d.x + 5.0, d.y), raster_mm=RASTER_MM) is True
        assert reach.is_reachable(Point(d.x + 6.0, d.y), raster_mm=RASTER_MM) is False

    def test_each_pin_has_own_centre(self) -> None:
        # N>2: every pin's disc is centred on that pin's default, independently.
        t = _bendable_3pin()                    # pins at (0,0), (1,0), (2,0)
        reaches = pin_reaches(t)
        assert reaches[1].default != reaches[2].default != reaches[3].default
        # A target on pin 3's far side: in pin 3's disc, outside pin 1's disc.
        far = Point(reaches[3].default.x + 4.0, 0.0)   # 4.0 holes from pin 3
        assert reaches[3].is_reachable(far, raster_mm=RASTER_MM) is True
        assert reaches[1].is_reachable(far, raster_mm=RASTER_MM) is False  # 6.0 from pin 1 > r_max


class TestIsPinBendable:
    def test_rigid_dip_not_bendable(self) -> None:
        d = _builtin()["DIP-8"]   # bend=0 → rigid
        assert not is_pin_bendable(d, 2)
        assert not is_pin_bendable(d, 8)

    def test_electrolytic_bendable(self) -> None:
        c = _builtin()["CP_Elec_6.3x11"]  # bend=4
        assert is_pin_bendable(c, 2)

    def test_axial_solver_bendable(self) -> None:
        r = _builtin()["R_Axial_DIN0207"]  # solver, lead_length 32 → wide ring
        assert is_pin_bendable(r, 2)

    def test_to92_bendable(self) -> None:
        t = _bendable_3pin()   # 3-pin prism, bend 3.54
        assert is_pin_bendable(t, 2)
        assert is_pin_bendable(t, 3)

    def test_unknown_pin_not_bendable(self) -> None:
        assert not is_pin_bendable(_builtin()["DIP-8"], 99)


class TestPackageToBendSpecs:
    """Adapter feeding place_tool: Package → legacy dict[int, PinBendSpec]."""

    def test_rigid_dip_yields_no_specs(self) -> None:
        # bend=0 → no movable pins → empty dict (place_tool: not bendable).
        assert package_to_bend_specs(_builtin()["DIP-8"]) == {}

    def test_electrolytic_specs_are_free_2d(self) -> None:
        specs = package_to_bend_specs(_builtin()["CP_Elec_6.3x11"])
        assert set(specs) == {1, 2}              # both pins bendable
        spec = specs[2]
        assert spec.is_free()                    # always free-2D, never discrete
        # ring 1..5 holes from origin; reach in HOLE units (mm ÷ 2.5).
        assert spec.min_reach == 1.0
        assert spec.max_reach == 5.0
        assert spec.default_offset == Vector(0.0, 0.0)

    def test_axial_specs_match_solver_in_holes(self) -> None:
        specs = package_to_bend_specs(_builtin()["R_Axial_DIN0207"])
        spec = specs[2]
        assert spec.is_free()
        # horizontal min 7.92mm / 2.5 = 3.168 holes
        assert round(spec.min_reach, 2) == 3.17

    def test_no_discrete_for_to92(self) -> None:
        # TO-92 was discrete in legacy; package model makes it free-2D.
        specs = package_to_bend_specs(_bendable_3pin())
        for spec in specs.values():
            assert spec.is_free()

    def test_is_reachable_from_pin_default_alias_matches_origin(self) -> None:
        # The new canonical check and the back-compat alias are pure renames:
        # both measure the euclidean radius from whatever centre is passed.
        spec = package_to_bend_specs(_builtin()["CP_Elec_6.3x11"])[2]
        centre = Point(3.0, 1.0)
        for target in (Point(3.0, 4.0), Point(9.0, 1.0), Point(3.0, 1.0)):
            assert (
                spec.is_reachable_from_pin_default(centre, target)
                == spec.is_reachable_from_origin(centre, target)
            )
