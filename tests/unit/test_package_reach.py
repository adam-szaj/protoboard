# SPDX-License-Identifier: MIT
"""Tests for reach.py — the Package-derived replacement for bendable_pins."""

import pytest

from protoboard.domain.components.definition import (
    ComponentDefinition,
    MetaPin,
    PackageOption,
)
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.package import load_packages
from protoboard.domain.components.package.family import PackageFamily, PackageVariant
from protoboard.domain.components.package.reach import (
    effective_bend_specs,
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
    {
        "name": "cylinder",
        "abstract": True,
        "shape": "cylinder",
        "dims": {"diameter": None, "length": None, "edge_radius": 0.0},
    },
    {"name": "prism", "abstract": True, "shape": "prism"},
    {
        "name": "cylinder_axial",
        "abstract": True,
        "extends": "cylinder",
        "leads": {
            "direction": [[0, 0, -1], [0, 0, 1]],
            "body_offset": 0,
            "board_offset": "solve",
            "lead_length": None,
            "lead_r": None,
            "body_eccentricity": 0.0,
        },
    },
    {
        "name": "cylinder_radial",
        "abstract": True,
        "extends": "cylinder",
        "arrangement": "radial",
        "leads": {
            "direction": [[0, 0, -1], [0, 0, -1]],
            "body_offset": {"unit": 2.5, "value": [[1, 0]]},
            "board_offset": "default",
        },
        "bend": 0.0,
    },
    {
        "name": "prism_dil",
        "abstract": True,
        "extends": "prism",
        "leads": {"direction": [0, 0, -1], "board_offset": "default"},
        "bend": 0.0,
    },
    # Axial resistor body (DIN 0207): solver-driven reach.
    {
        "name": "R_Axial_DIN0207",
        "extends": "cylinder_axial",
        "dims": {"diameter": 2.5, "length": 6.3},
        "leads": {"lead_r": 0.27, "lead_length": 32.0},
    },
    # Radial electrolytic 6.3x11: bend ring 1..5 holes.
    {
        "name": "CP_Elec_6.3x11",
        "extends": "cylinder_radial",
        "dims": {"diameter": 6.3, "length": 11.0},
        "leads": {"body_offset": {"unit": 2.5, "value": [[1, 0]]}},
        "bend": 4.0,
    },
    # DIP-8: rigid, two rows of 4 at 0.3in.
    {
        "name": "DIP-8",
        "extends": "prism_dil",
        "dims": {"ly": 6.25, "lx": 9.20, "lz": 4.0},
        "leads": {
            "body_offset": {"unit": 2.5, "value": [[3, [0, 1]], [3, 0], [3, [0, -1]]]}
        },
    },
    # A 3-pin bendable prism (TO-92-like): each lead bends about its own home.
    {
        "name": "BEND3",
        "extends": "prism",
        "dims": {"ly": 4.5, "lx": 4.5, "lz": 5.2},
        "leads": {
            "direction": [[0, 0, -1], [0, 0, -1], [0, 0, -1]],
            "body_offset": {"unit": 2.5, "value": [[1, 0], [1, 0]]},
            "board_offset": "default",
        },
        "bend": 3.54,
    },
    # Explicit per-pin reach overrides the package-wide geometric bend.
    {
        "name": "BEND_OVERRIDE",
        "extends": "prism",
        "dims": {"ly": 4.5, "lx": 4.5, "lz": 5.2},
        "leads": {
            "direction": [[0, 0, -1], [0, 0, -1]],
            "body_offset": {"unit": 2.5, "value": [[1, 0]]},
            "board_offset": "default",
        },
        "bend": 3.54,
        "bendable_pins": {
            2: {"min_reach": 2.0, "max_reach": 9.0, "default": {"x": 0.0, "y": 4.0}},
        },
    },
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

    def test_default_is_each_pins_home(self) -> None:
        c = _builtin()["CP_Elec_6.3x11"]
        reaches = pin_reaches(c)
        assert reaches[2].default == c.pin_positions()[1]
        assert reaches[1].default == c.pin_positions()[0]

    def test_electrolytic_reachable_within_bend(self) -> None:
        c = _builtin()["CP_Elec_6.3x11"]  # ring 1..5 holes from the pin's default
        reach = pin_reaches(c)[2]
        d = reach.default  # pin 2 home hole = (1, 0); the disc is centred here
        # measured from the pin's own default hole:
        assert (
            reach.is_reachable(Point(d.x + 5.0, d.y), raster_mm=RASTER_MM) is True
        )  # 5 holes = r_max
        assert (
            reach.is_reachable(Point(d.x + 1.0, d.y), raster_mm=RASTER_MM) is True
        )  # 1 hole = r_min
        assert (
            reach.is_reachable(d, raster_mm=RASTER_MM) is False
        )  # on the default (< r_min)
        assert (
            reach.is_reachable(Point(d.x + 6.0, d.y), raster_mm=RASTER_MM) is False
        )  # beyond r_max

    def test_disc_centred_on_pin_default(self) -> None:
        # Reach is a disc centred on the pin's OWN default hole, not the origin.
        c = _builtin()["CP_Elec_6.3x11"]
        reach = pin_reaches(c)[2]
        d = reach.default  # (1, 0), not the origin (0, 0)
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
        t = _bendable_3pin()  # pins at (0,0), (1,0), (2,0)
        reaches = pin_reaches(t)
        assert reaches[1].default != reaches[2].default != reaches[3].default
        # A target on pin 3's far side: in pin 3's disc, outside pin 1's disc.
        far = Point(reaches[3].default.x + 4.0, 0.0)  # 4.0 holes from pin 3
        assert reaches[3].is_reachable(far, raster_mm=RASTER_MM) is True
        assert (
            reaches[1].is_reachable(far, raster_mm=RASTER_MM) is False
        )  # 6.0 from pin 1 > r_max


class TestIsPinBendable:
    def test_rigid_dip_not_bendable(self) -> None:
        d = _builtin()["DIP-8"]  # bend=0 → rigid
        assert not is_pin_bendable(d, 2)
        assert not is_pin_bendable(d, 8)

    def test_electrolytic_bendable(self) -> None:
        c = _builtin()["CP_Elec_6.3x11"]  # bend=4
        assert is_pin_bendable(c, 2)

    def test_axial_solver_bendable(self) -> None:
        r = _builtin()["R_Axial_DIN0207"]  # solver, lead_length 32 → wide ring
        assert is_pin_bendable(r, 2)

    def test_to92_bendable(self) -> None:
        t = _bendable_3pin()  # 3-pin prism, bend 3.54
        assert is_pin_bendable(t, 2)
        assert is_pin_bendable(t, 3)

    def test_unknown_pin_not_bendable(self) -> None:
        assert not is_pin_bendable(_builtin()["DIP-8"], 99)


class TestPackageToBendSpecs:
    """Package-derived per-pin mechanics used by placement tools."""

    def test_rigid_dip_yields_no_specs(self) -> None:
        # bend=0 → no movable pins → empty dict (place_tool: not bendable).
        assert package_to_bend_specs(_builtin()["DIP-8"]) == {}

    def test_explicit_per_pin_specs_override_geometric_reach(self) -> None:
        specs = package_to_bend_specs(_builtin()["BEND_OVERRIDE"])

        assert set(specs) == {2}
        assert specs[2].min_reach == 2.0
        assert specs[2].max_reach == 9.0
        assert specs[2].default_offset == Vector(0.0, 4.0)

    def test_electrolytic_specs_are_free_2d(self) -> None:
        specs = package_to_bend_specs(_builtin()["CP_Elec_6.3x11"])
        assert set(specs) == {1, 2}  # both pins bendable
        spec = specs[2]
        assert spec.is_free()  # always free-2D, never discrete
        # ring 1..5 holes from origin; reach in HOLE units (mm ÷ 2.5).
        assert spec.min_reach == 1.0
        assert spec.max_reach == 5.0
        assert spec.default_offset == Vector(0.0, 0.0)

    def test_axial_specs_span_both_orientations(self) -> None:
        # The reach an axial part offers the place tool is the UNION of its two
        # mount orientations: min_reach is the UPRIGHT (vertical) minimum so pin
        # 2 may sit on an adjacent pad, max_reach the FLAT (horizontal) maximum.
        specs = package_to_bend_specs(_builtin()["R_Axial_DIN0207"])
        spec = specs[2]
        assert spec.is_free()
        # vertical min 2.06mm / 2.5 = 0.824 holes (was the horizontal 3.17 —
        # that floor wrongly blocked upright placement on adjacent pads).
        assert round(spec.min_reach, 2) == 0.82
        # horizontal max 71.92mm / 2.5 = 28.768 holes.
        assert round(spec.max_reach, 2) == 28.77
        # An adjacent pad (1 hole) is now within reach → upright mount allowed.
        assert spec.min_reach <= 1.0 <= spec.max_reach

    def test_no_discrete_for_to92(self) -> None:
        # TO-92 was discrete in legacy; package model makes it free-2D.
        specs = package_to_bend_specs(_bendable_3pin())
        for spec in specs.values():
            assert spec.is_free()

    @pytest.mark.parametrize(
        "target, reachable",
        [
            (Point(3.0, 1.0), False),
            (Point(3.0, 2.0), True),
            (Point(3.0, 6.0), True),
            (Point(9.0, 1.0), False),
        ],
    )
    def test_reach_is_measured_from_pin_default(
        self, target: Point, reachable: bool
    ) -> None:
        spec = package_to_bend_specs(_builtin()["CP_Elec_6.3x11"])[2]
        centre = Point(3.0, 1.0)
        assert spec.is_reachable_from_pin_default(centre, target) is reachable


def test_unresolved_preplacement_variant_does_not_use_default_bend_specs() -> None:
    definition = ComponentDefinition(
        name="X",
        meta_pins=(MetaPin(("A",)), MetaPin(("B",))),
        package_options=(PackageOption(name="DIP", ref="dip/std"),),
    )
    families = {
        "dip": PackageFamily(
            family_id="dip",
            variants=(PackageVariant("std", "DIP", _builtin()["DIP-8"]),),
        )
    }

    with pytest.raises(ValueError, match="missing/std"):
        effective_bend_specs(
            definition, families=families, package_variant="missing/std"
        )


def test_preplacement_bend_specs_reject_existing_undeclared_package() -> None:
    packages = _builtin()
    definition = ComponentDefinition(
        name="X",
        meta_pins=(MetaPin(("A",)), MetaPin(("B",))),
        package_options=(PackageOption(name="DIP", ref="dip/std"),),
    )
    families = {
        "dip": PackageFamily(
            family_id="dip",
            variants=(PackageVariant("std", "DIP", packages["DIP-8"]),),
        ),
        "other": PackageFamily(
            family_id="other",
            variants=(PackageVariant("std", "Other", packages["BEND_OVERRIDE"]),),
        ),
    }

    with pytest.raises(ValueError, match="package option"):
        effective_bend_specs(definition, families=families, package_variant="other/std")


def test_explicit_empty_catalogue_does_not_use_instance_catalogue() -> None:
    definition = ComponentDefinition(
        name="X",
        meta_pins=(MetaPin(("A",)), MetaPin(("B",))),
        package_options=(PackageOption(name="DIP", ref="dip/std"),),
    )
    families = {
        "dip": PackageFamily(
            family_id="dip",
            variants=(PackageVariant("std", "DIP", _builtin()["DIP-8"]),),
        )
    }
    instance = ComponentInstance.new("X", "X1", "").bind(definition, families)

    with pytest.raises(ValueError, match="unresolved package variant"):
        effective_bend_specs(definition, instance, families={})


class TestReachOffsetRange:
    """reach_offset_range — the union-over-orientations ring the place tool sees,
    distinct from board_offset_range (one chosen orientation)."""

    def test_axial_reach_is_union_of_orientations(self) -> None:
        from protoboard.domain.components.package.solver import MountOrientation

        r = _builtin()["R_Axial_DIN0207"]
        h = r.board_offset_range(orientation=MountOrientation.HORIZONTAL)
        v = r.board_offset_range(orientation=MountOrientation.VERTICAL)
        reach = r.reach_offset_range()
        # Union: tightest (vertical) minimum, widest (horizontal) maximum.
        assert reach.r_min_mm == min(h.r_min_mm, v.r_min_mm) == v.r_min_mm
        assert reach.r_max_mm == max(h.r_max_mm, v.r_max_mm) == h.r_max_mm
        # The adjacent-pad spacing (one raster step) is inside the reach.
        assert reach.contains_mm(RASTER_MM)

    def test_axial_default_layout_stays_horizontal(self) -> None:
        # pin_positions (the ghost's default spacing) is unchanged — only the
        # reach widened. Pin 2 still defaults to the flat horizontal spacing.
        r = _builtin()["R_Axial_DIN0207"]
        positions = r.pin_positions()
        # Default pin-2 spacing equals the horizontal solver minimum (in holes).
        from protoboard.domain.components.package.solver import MountOrientation

        h = r.board_offset_range(orientation=MountOrientation.HORIZONTAL)
        assert round(positions[1].x, 3) == round(h.r_min_mm / RASTER_MM, 3)

    def test_dip_reach_equals_board_offset_range(self) -> None:
        # A non-axial part has one orientation → reach == board_offset_range.
        dip = _builtin()["DIP-8"]
        assert dip.reach_offset_range() == dip.board_offset_range()

    def test_radial_bend_reach_unchanged(self) -> None:
        # A radial (bend, non-solve) cylinder keeps its single ring.
        cp = _builtin()["CP_Elec_6.3x11"]
        assert cp.reach_offset_range() == cp.board_offset_range()
