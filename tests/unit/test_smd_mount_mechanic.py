# SPDX-License-Identifier: MIT
"""SMD mounting mechanic — surface-mount pin 2 gets a discrete king-move mount.

An SMD chip body is rigid, but it rests on pads (not in holes), so its second
terminal may sit on any pad adjacent to the first (orthogonal or diagonal).
This is expressed as a discrete ``PinBendSpec`` for pin 2; the place tool's
discrete-snap and the properties-panel combo then drive the choice.
"""

from __future__ import annotations

import uuid

from protoboard.domain.components.footprints import TwoPin
from protoboard.domain.components.package import (
    MountStyle,
    effective_bend_specs,
    is_pin_bendable,
    package_to_bend_specs,
    surface_mount_offsets,
)
from protoboard.domain.components.package.loader import build_package
from protoboard.domain.components.package.shapes import Prism
from protoboard.domain.layout.placement import ComponentPlacement, pin_world_position
from protoboard.geometry import Point


def _smd_package(mount_points: list | None = None) -> Prism:
    node: dict = {
        "shape": "prism",
        "dims": {"length": 2.0, "width": 1.25},
        "arrangement": "axial",
        "mount": "surface",
        "bend": 0,
        "leads": {
            "direction": [[0, 0, -1], [0, 0, -1]],
            "body_offset": {"unit": 2.5, "value": [[1, 0]]},
            "board_offset": "default",
        },
    }
    if mount_points is not None:
        node["mount_points"] = mount_points
    pkg = build_package(node, "SMD_TEST")
    assert isinstance(pkg, Prism)
    return pkg


def _through_hole_prism() -> Prism:
    pkg = build_package(
        {
            "shape": "prism",
            "dims": {"width": 7.62, "length": 10.0},
            "bend": 0,
            "leads": {
                "direction": [0, 0, -1],
                "body_offset": {
                    "unit": 2.5, "value": [[3, [0, 1]], [3, 0], [3, [0, -1]]],
                },
                "board_offset": "default",
            },
        },
        "DIP_TEST",
    )
    assert isinstance(pkg, Prism)
    return pkg


class TestMountParsing:
    def test_mount_surface_parses(self) -> None:
        assert _smd_package().mount is MountStyle.SURFACE

    def test_default_mount_is_through_hole(self) -> None:
        assert _through_hole_prism().mount is MountStyle.THROUGH_HOLE

    def test_mount_points_parse(self) -> None:
        pkg = _smd_package(mount_points=[[1, 0], [0, 1]])
        assert pkg.mount_points == (Point(1.0, 0.0), Point(0.0, 1.0))


class TestSurfaceMountOffsets:
    def _world_neighbours(self, pkg: Prism) -> set[tuple[int, int]]:
        """Where pin 2 lands (relative to pin 1) for each mount offset."""
        from protoboard.domain.components.package.adapter import PackageFootprint
        pf = PackageFootprint(pkg)
        anchor = Point(10.0, 10.0)
        p1 = pin_world_position(
            pf, ComponentPlacement(
                component_id=uuid.UUID(int=0), position=anchor,
                rotation=0, side="top",
            ), 1,
        )
        out: set[tuple[int, int]] = set()
        for off in surface_mount_offsets(pkg):
            pl = ComponentPlacement(
                component_id=uuid.UUID(int=0), position=anchor,
                rotation=0, side="top", pin_offsets={2: off},
            )
            p2 = pin_world_position(pf, pl, 2)
            out.add((round(p2.x - p1.x), round(p2.y - p1.y)))
        return out

    def test_default_is_eight_king_move_neighbours(self) -> None:
        # Every mount lands pin 2 on a distinct king-move neighbour of pin 1,
        # never on pin 1's own pad.
        deltas = self._world_neighbours(_smd_package())
        expected = {
            (dx, dy)
            for dy in (-1, 0, 1) for dx in (-1, 0, 1)
            if (dx, dy) != (0, 0)
        }
        assert deltas == expected

    def test_custom_mount_points_restrict_the_set(self) -> None:
        # Ortho-only: pin 2 may only go E/W/N/S of pin 1.
        deltas = self._world_neighbours(
            _smd_package(mount_points=[[1, 0], [-1, 0], [0, 1], [0, -1]]),
        )
        assert deltas == {(1, 0), (-1, 0), (0, 1), (0, -1)}


class TestPackageToBendSpecs:
    def test_smd_pin2_gets_discrete_spec(self) -> None:
        specs = package_to_bend_specs(_smd_package())
        assert list(specs.keys()) == [2]
        spec = specs[2]
        assert not spec.is_free()       # discrete, not a free ring
        assert len(spec.discrete) == 8  # straight-across + 7 other neighbours
        assert spec.default_offset == Point(0.0, 0.0)

    def test_through_hole_prism_has_no_specs(self) -> None:
        assert package_to_bend_specs(_through_hole_prism()) == {}

    def test_is_pin_bendable_for_smd(self) -> None:
        pkg = _smd_package()
        assert is_pin_bendable(pkg, 2) is True
        assert is_pin_bendable(pkg, 1) is False


class TestEffectiveBendSpecs:
    def test_smd_definition_exposes_mount_spec(self) -> None:
        from protoboard.domain.components.definition import (
            ComponentClass,
            ComponentDefinition,
        )
        from protoboard.domain.components.package.adapter import PackageFootprint
        pkg = _smd_package()
        defn = ComponentDefinition(
            name="R_SMD", component_class=ComponentClass.RESISTOR,
            footprint=PackageFootprint(pkg), pin_names={}, package=pkg,
        )
        specs = effective_bend_specs(defn)
        assert list(specs.keys()) == [2]
        assert not specs[2].is_free()

    def test_plain_footprint_falls_back_to_bendable_pins(self) -> None:
        from protoboard.domain.components.definition import (
            ComponentClass,
            ComponentDefinition,
            PinBendSpec,
        )
        defn = ComponentDefinition(
            name="R", component_class=ComponentClass.RESISTOR,
            footprint=TwoPin(), pin_names={},
            bendable_pins={2: PinBendSpec(min_reach=1, max_reach=5)},
        )
        specs = effective_bend_specs(defn)
        assert list(specs.keys()) == [2]
        assert specs[2].is_free()


class TestLibrarySmdIsRigidWithMount:
    def test_consolidated_resistor_offers_rigid_smd_footprint(self) -> None:
        # The SMD chip is now a footprint option of the generic ``R`` (not a
        # separate ``R_SMD_0805`` component).  Resolving that option must still
        # yield a rigid package whose pin-2 carries the discrete mount spec.
        from protoboard.domain.components.package import (
            is_rigid_package,
            package_to_bend_specs,
            resolve_variant,
        )
        from protoboard.paths import BUILTIN_LIBRARY_DIR
        from protoboard.services.library.loader import load_library
        from protoboard.services.library.package_family_loader import (
            load_full_package_catalogue,
        )
        families = load_full_package_catalogue()
        lib = load_library([BUILTIN_LIBRARY_DIR], families)
        defn = lib.get("R")
        smd = next(o for o in defn.footprints if o.name == "SMD-0805")
        variant = resolve_variant(families, smd.ref)
        assert variant is not None
        assert is_rigid_package(variant.package)        # SMD body is rigid
        specs = package_to_bend_specs(variant.package)  # discrete mount spec
        assert list(specs.keys()) == [2]
        assert not specs[2].is_free()
        assert len(specs[2].discrete) == 8
