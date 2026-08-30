# SPDX-License-Identifier: MIT
"""The synthetic generic-package invariant."""

from __future__ import annotations

from protoboard.domain.components.footprints import DIL, SIL, GenericFootprint
from protoboard.domain.components.package.kind import (
    GENERIC_PACKAGE_KIND,
    is_generic_package,
    synth_generic_package,
)
from protoboard.domain.components.package.reach import effective_bend_specs
from protoboard.domain.components.package.shapes import is_rigid_package
from protoboard.paths import BUILTIN_LIBRARY_DIR
from protoboard.services.library.loader import load_library
from protoboard.services.library.package_family_loader import (
    load_full_package_catalogue,
)


def _loaded_library():  # type: ignore[no-untyped-def]
    families = load_full_package_catalogue()
    lib = load_library([BUILTIN_LIBRARY_DIR], families)
    return lib, {**families, **lib.package_families}


class TestSynthGenericPackage:
    def test_is_rigid_and_tagged(self) -> None:
        pkg = synth_generic_package(SIL(n=2))
        assert pkg is not None
        assert is_rigid_package(pkg)
        assert is_generic_package(pkg)
        assert getattr(pkg, "kind", None) == GENERIC_PACKAGE_KIND

    def test_pin_positions_match_footprint(self) -> None:
        # The synthetic package's pins must coincide with the footprint's, so it
        # is a pure body/bend fallback and never shifts pin geometry.
        for fp in (SIL(n=2), SIL(n=3), DIL(n=8)):
            pkg = synth_generic_package(fp)
            assert pkg is not None
            fp_pins = [(round(p.x, 6), round(p.y, 6)) for p in fp.pin_positions()]
            pk_pins = [(round(p.x, 6), round(p.y, 6)) for p in pkg.pin_positions()]
            assert pk_pins == fp_pins

    def test_zero_pin_footprint_returns_none(self) -> None:
        assert synth_generic_package(GenericFootprint(offsets=[])) is not None
        # (a 1-pin part still gets a degenerate body; only a truly empty cloud
        # could fail — guarded by the loader's pin_count >= 1 check.)

    def test_declared_package_is_not_generic(self) -> None:
        families = load_full_package_catalogue()
        # A real catalogue body (SMD-0805) must NOT be flagged generic.
        from protoboard.domain.components.package.family import resolve_variant

        var = resolve_variant(families, "chip-smd/imperial/0805")
        assert var is not None
        assert not is_generic_package(var.package)


class TestPackageInvariant:
    def test_every_library_part_has_a_package(self) -> None:
        from protoboard.domain.components.package import resolve_component_package

        lib, families = _loaded_library()
        assert all(
            resolve_component_package(lib.get(name), None, families) is not None
            for name in lib.names()
        )

    def test_family_ref_part_defaults_to_its_variant(self) -> None:
        from protoboard.domain.components.package import resolve_component_package

        lib, families = _loaded_library()
        # A diode declares only a package reference — since the 'footprint:'
        # field removal, the referenced variant IS its default geometry
        # (a real bendable axial body, not the synthetic generic).
        from protoboard.domain.components.package import is_rigid_package

        diode = lib.get("1N4148")
        package = resolve_component_package(diode, None, families)
        assert not is_generic_package(package)
        assert not is_rigid_package(package)


class TestBendSpecParityFrozen:
    """Parts that fall back to the generic package keep their authored
    ``bendable_pins`` — the invariant must not regress bendability (the same
    class of bug the SMD bend-spec fix addressed)."""

    def test_diode_bend_comes_from_its_axial_package(self) -> None:
        # The diode's default is the real DO-35 axial body now (like R): the
        # lead reach comes from that package, both leads bend.
        from protoboard.domain.components.package import resolve_component_package

        lib, families = _loaded_library()
        defn = lib.get("1N4148")
        assert not is_generic_package(resolve_component_package(defn, None, families))
        specs = effective_bend_specs(defn, families=families)
        assert set(specs) == {1, 2}

    def test_generic_resistor_uses_package_bend_specs(self) -> None:
        # R's default is now the real DIN-0207 axial body (the dead
        # ``footprint: TwoPin`` neutral default was dropped), so its lead reach
        # comes from that package geometry, not its empty explicit map.
        from protoboard.domain.components.package import resolve_component_package

        lib, families = _loaded_library()
        defn = lib.get("R")
        package = resolve_component_package(defn, None, families)
        assert not is_generic_package(package)
        specs = effective_bend_specs(defn, families=families)
        # Both leads bend, with the package-derived reach (not the YAML ring).
        assert set(specs) == {1, 2}
        assert specs != package.bendable_pins

    def test_crystal_with_no_bend_stays_empty(self) -> None:
        from protoboard.domain.components.package import resolve_component_package

        lib, families = _loaded_library()
        defn = lib.get("OSC_1MHz")
        package = resolve_component_package(defn, None, families)
        assert effective_bend_specs(defn, families=families) == package.bendable_pins
