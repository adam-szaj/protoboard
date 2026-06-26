# SPDX-License-Identifier: MIT
"""SMD mounting mechanic — surface-mount pin 2 gets a free neighbour ring.

An SMD chip body is rigid, but it rests on pads (not in holes), so its second
terminal may sit on any pad adjacent to the first (orthogonal or diagonal).
This is expressed as a FREE reach ring ``[1, √2+ε]`` holes for pin 2 — the band
spanning pin 1's eight neighbours — so the same free-2D place/re-drag path that
serves every movable pin lands it on one of those pads.
"""

from __future__ import annotations

from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.package import (
    MountStyle,
    effective_bend_specs,
    is_pin_bendable,
    package_to_bend_specs,
)
from protoboard.domain.components.package.loader import build_package
from protoboard.domain.components.package.shapes import Prism
from protoboard.geometry import Point


def _smd_package(mount_points: list | None = None) -> Prism:
    node: dict = {
        "shape": "prism",
        "dims": {
            "length": 2.0,
            "width": 1.25
        },
        "arrangement": "axial",
        "mount": "surface",
        "bend": 0,
        "leads": {
            "direction": [[0, 0, -1], [0, 0, -1]],
            "body_offset": {
                "unit": 2.5,
                "value": [[1, 0]]
            },
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
            "dims": {
                "width": 7.62,
                "length": 10.0
            },
            "bend": 0,
            "leads": {
                "direction": [0, 0, -1],
                "body_offset": {
                    "unit": 2.5,
                    "value": [[3, [0, 1]], [3, 0], [3, [0, -1]]],
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


class TestPackageToBendSpecs:

    def test_smd_pin2_gets_free_neighbour_ring(self) -> None:
        # A surface-mount second terminal may land on any neighbour pad of pin 1.
        # This is a FREE reach ring [1, √2+ε] (the band over pin 1's orthogonal
        # and diagonal neighbours), not a discrete set — so the place-tool / re-
        # drag snap through the same free-2D path as any other movable pin.
        from math import sqrt

        specs = package_to_bend_specs(_smd_package())
        assert list(specs.keys()) == [2]
        spec = specs[2]
        assert spec.is_free()       # free ring, not a discrete mount set
        assert spec.min_reach == 1.0
        assert spec.max_reach == sqrt(2) + 0.1

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
        from protoboard.domain.components.package.adapter import (
            PackageFootprint,
        )
        pkg = _smd_package()
        defn = ComponentDefinition(
            name="R_SMD",
            component_class=ComponentClass.RESISTOR,
            footprint=PackageFootprint(pkg),
            pin_names={},
            package=pkg,
        )
        specs = effective_bend_specs(defn)
        assert list(specs.keys()) == [2]
        assert specs[2].is_free()  # SMD pin 2 is now a free neighbour ring

    def test_plain_footprint_falls_back_to_bendable_pins(self) -> None:
        from protoboard.domain.components.definition import (
            ComponentClass,
            ComponentDefinition,
            PinBendSpec,
        )
        defn = ComponentDefinition(
            name="R",
            component_class=ComponentClass.RESISTOR,
            footprint=SIL(n=2),
            pin_names={},
            bendable_pins={2: PinBendSpec(min_reach=1, max_reach=5)},
        )
        specs = effective_bend_specs(defn)
        assert list(specs.keys()) == [2]
        assert specs[2].is_free()


class TestLibrarySmdIsRigidWithMount:

    def test_consolidated_resistor_offers_rigid_smd_footprint(self) -> None:
        # The SMD chip is now a footprint option of the generic ``R`` (not a
        # separate ``R_SMD_0805`` component).  Resolving that option must still
        # yield a rigid package whose pin-2 carries the free neighbour-ring spec.
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
        assert smd.ref is not None  # catalogue footprint option, not inline
        variant = resolve_variant(families, smd.ref)
        assert variant is not None
        assert is_rigid_package(variant.package)  # SMD body is rigid
        specs = package_to_bend_specs(variant.package)  # free neighbour ring
        assert list(specs.keys()) == [2]
        assert specs[2].is_free()


class TestEffectiveBendSpecsResolvesChosenVariant:
    """Regression: a part whose bodies live in a ``footprints:`` list (``R``)
    must get the chosen variant's specs from ``effective_bend_specs`` — the
    place tool / properties panel / select-tool re-drag all rely on this so an
    SMD-0805 resistor gets its tight SMD neighbour ring, not the wide THT axial
    bend ring."""

    @staticmethod
    def _resistor_and_catalogue():  # type: ignore[no-untyped-def]
        from protoboard.paths import BUILTIN_LIBRARY_DIR
        from protoboard.services.library.loader import load_library
        from protoboard.services.library.package_family_loader import (
            load_full_package_catalogue,
        )
        families = load_full_package_catalogue()
        lib = load_library([BUILTIN_LIBRARY_DIR], families)
        return lib.get("R"), families

    def test_smd_variant_yields_neighbour_ring(self) -> None:
        from math import sqrt

        from protoboard.domain.components.instance import ComponentInstance

        defn, families = self._resistor_and_catalogue()
        inst = ComponentInstance.new(
            definition_ref="R", refdes="R1", value="",
            package_variant="SMD-0805/typ",
        )
        specs = effective_bend_specs(defn, inst, families)
        assert list(specs.keys()) == [2]
        # The SMD neighbour ring [1, √2+ε] — tight enough to land only on pin 1's
        # eight neighbours, distinct from a wide THT axial bend ring.
        assert specs[2].is_free()
        assert specs[2].min_reach == 1.0
        assert specs[2].max_reach == sqrt(2) + 0.1

    def test_axial_variant_stays_free(self) -> None:
        from protoboard.domain.components.instance import ComponentInstance

        defn, families = self._resistor_and_catalogue()
        inst = ComponentInstance.new(
            definition_ref="R", refdes="R2", value="",
            package_variant="DIN-0207/typ",
        )
        specs = effective_bend_specs(defn, inst, families)
        # A THT axial body keeps its free bendable lead (no regression).
        assert any(s.is_free() for s in specs.values())

    def test_no_catalogue_falls_back_to_bendable_pins(self) -> None:
        # Backward compat for every legacy caller that passes only the defn:
        # behaviour is unchanged (the THT axial free spec).
        defn, _ = self._resistor_and_catalogue()
        specs = effective_bend_specs(defn)
        assert specs == defn.bendable_pins
