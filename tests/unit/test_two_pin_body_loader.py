# SPDX-License-Identifier: MIT
"""Loader for ``TwoPinBodyPackage`` YAML assets — schema + bundled assets."""

from __future__ import annotations

from pathlib import Path

import pytest

from protoboard.domain.components.two_pin_body import (
    BodyShape,
    DiscProfile,
    EdgeStyle,
    LeadArrangement,
    Material,
)
from protoboard.services.library.two_pin_body_loader import (
    _load_one_file,
    load_two_pin_body_families,
)


@pytest.fixture()
def yaml_dir(tmp_path: Path) -> Path:
    return tmp_path


def _write(dir_: Path, name: str, content: str) -> Path:
    p = dir_ / name
    p.write_text(content, encoding="utf-8")
    return p


class TestParseSinglePackage:
    def test_axial_cylinder_package_with_one_variant(self, yaml_dir: Path) -> None:
        path = _write(yaml_dir, "pkg.yaml", """\
packages:
  - id: TEST-1
    label: "Test package"
    material: glass
    lead_arrangement: axial
    shape: cylinder
    edge: rounded
    variants:
      - id: typ
        label: "Typical"
        length_mm: 6.5
        diameter_mm: 2.5
        lead_diameter_mm: 0.5
""")
        pkgs = _load_one_file(path)
        assert len(pkgs) == 1
        pkg = pkgs[0]
        assert pkg.package_id == "TEST-1"
        v = pkg.default_variant()
        assert v.lead_arrangement is LeadArrangement.AXIAL
        assert v.shape is BodyShape.CYLINDER
        assert v.material is Material.GLASS
        assert v.edge is EdgeStyle.ROUNDED
        assert v.length_mm == 6.5
        assert v.diameter_mm == 2.5

    def test_radial_disc_package_with_profile(self, yaml_dir: Path) -> None:
        path = _write(yaml_dir, "pkg.yaml", """\
packages:
  - id: TEST-DISC
    label: "Test disc"
    material: ceramic
    lead_arrangement: radial
    shape: disc
    disc_profile: candy
    variants:
      - id: typ
        diameter_mm: 8.0
        thickness_mm: 4.0
""")
        pkg = _load_one_file(path)[0]
        v = pkg.default_variant()
        assert v.shape is BodyShape.DISC
        assert v.disc_profile is DiscProfile.CANDY
        assert v.material is Material.CERAMIC

    def test_prism_package(self, yaml_dir: Path) -> None:
        path = _write(yaml_dir, "pkg.yaml", """\
packages:
  - id: TEST-PRISM
    label: "Test prism"
    material: metal
    lead_arrangement: radial
    shape: prism
    edge: sharp
    variants:
      - id: typ
        length_mm: 22.0
        width_mm: 9.5
        height_mm: 9.0
""")
        v = _load_one_file(path)[0].default_variant()
        assert v.shape is BodyShape.PRISM
        assert v.edge is EdgeStyle.SHARP
        assert v.material is Material.METAL
        assert v.width_mm == 9.5

    def test_default_edge_is_rounded(self, yaml_dir: Path) -> None:
        path = _write(yaml_dir, "pkg.yaml", """\
packages:
  - id: TEST
    label: "Test"
    lead_arrangement: axial
    shape: cylinder
    variants:
      - id: typ
        length_mm: 5.0
        diameter_mm: 2.0
""")
        v = _load_one_file(path)[0].default_variant()
        assert v.edge is EdgeStyle.ROUNDED
        # Material defaults to PLASTIC.
        assert v.material is Material.PLASTIC


class TestPackageDefaultsInherited:
    def test_variant_inherits_package_level_shape(self, yaml_dir: Path) -> None:
        """A variant that omits ``shape`` picks up the package's value."""
        path = _write(yaml_dir, "pkg.yaml", """\
packages:
  - id: PKG
    label: "Pkg"
    material: glass
    lead_arrangement: axial
    shape: cylinder
    variants:
      - id: S
        diameter_mm: 1.5
        length_mm: 3.0
      - id: L
        diameter_mm: 2.0
        length_mm: 4.5
""")
        pkg = _load_one_file(path)[0]
        s = pkg.get_variant("S")
        assert s is not None and s.shape is BodyShape.CYLINDER
        assert s.material is Material.GLASS
        l = pkg.get_variant("L")
        assert l is not None and l.diameter_mm == 2.0

    def test_variant_overrides_package_material(self, yaml_dir: Path) -> None:
        path = _write(yaml_dir, "pkg.yaml", """\
packages:
  - id: PKG
    label: "Pkg"
    material: plastic
    lead_arrangement: axial
    shape: cylinder
    variants:
      - id: typ
        diameter_mm: 2.0
        length_mm: 5.0
      - id: special
        material: ceramic
        diameter_mm: 2.5
        length_mm: 5.5
""")
        pkg = _load_one_file(path)[0]
        assert pkg.get_variant("typ").material is Material.PLASTIC  # type: ignore[union-attr]
        assert pkg.get_variant("special").material is Material.CERAMIC  # type: ignore[union-attr]


class TestMultiplePackagesPerFile:
    def test_one_file_can_carry_two_packages(self, yaml_dir: Path) -> None:
        path = _write(yaml_dir, "pkg.yaml", """\
packages:
  - id: PKG-A
    label: "A"
    material: glass
    lead_arrangement: axial
    shape: cylinder
    variants:
      - id: typ
        diameter_mm: 1.5
        length_mm: 3.0
  - id: PKG-B
    label: "B"
    material: plastic
    lead_arrangement: axial
    shape: cylinder
    variants:
      - id: typ
        diameter_mm: 2.7
        length_mm: 5.2
""")
        pkgs = _load_one_file(path)
        assert len(pkgs) == 2
        assert pkgs[0].package_id == "PKG-A"
        assert pkgs[1].package_id == "PKG-B"


class TestErrors:
    def test_missing_shape_raises(self, yaml_dir: Path) -> None:
        path = _write(yaml_dir, "pkg.yaml", """\
packages:
  - id: PKG
    label: "P"
    lead_arrangement: axial
    variants:
      - id: typ
        length_mm: 5.0
        diameter_mm: 2.0
""")
        with pytest.raises(ValueError, match="shape"):
            _load_one_file(path)

    def test_unknown_enum_value_raises(self, yaml_dir: Path) -> None:
        path = _write(yaml_dir, "pkg.yaml", """\
packages:
  - id: PKG
    label: "P"
    lead_arrangement: axial
    shape: trapezoid
    variants:
      - id: typ
        length_mm: 5.0
        diameter_mm: 2.0
""")
        with pytest.raises(ValueError, match="trapezoid"):
            _load_one_file(path)

    def test_no_variants_raises(self, yaml_dir: Path) -> None:
        path = _write(yaml_dir, "pkg.yaml", """\
packages:
  - id: PKG
    label: "P"
    lead_arrangement: axial
    shape: cylinder
    variants: []
""")
        with pytest.raises(ValueError, match="no variants"):
            _load_one_file(path)

    def test_no_packages_key_raises(self, yaml_dir: Path) -> None:
        path = _write(yaml_dir, "pkg.yaml", """\
something_else: []
""")
        with pytest.raises(ValueError, match="packages"):
            _load_one_file(path)


class TestDirectoryLoader:
    def test_skips_broken_yaml_keeps_good_ones(self, yaml_dir: Path) -> None:
        _write(yaml_dir, "good.yaml", """\
packages:
  - id: GOOD
    label: "Good"
    material: glass
    lead_arrangement: axial
    shape: cylinder
    variants:
      - id: typ
        length_mm: 5.0
        diameter_mm: 2.0
""")
        _write(yaml_dir, "broken.yaml", "this is not yaml: : :")
        result = load_two_pin_body_families(yaml_dir)
        assert "GOOD" in result
        assert "broken" not in result

    def test_returns_empty_when_directory_missing(self, tmp_path: Path) -> None:
        result = load_two_pin_body_families(tmp_path / "does_not_exist")
        assert result == {}


class TestBundledAssets:
    """Smoke test: every shipped YAML loads cleanly under the new loader."""

    def test_all_shipped_packages_load(self) -> None:
        pkgs = load_two_pin_body_families()
        for required in (
            "DO-35", "DO-41", "DO-15", "DO-201",
            "DIN-0207", "DIN-0411", "DIN-0617",
            "Axial-Elko-D6.3", "Axial-Elko-D10",
            "Radial-Elko-D5", "Radial-Elko-D6.3", "Radial-Elko-D10",
            "Ceramic-Disc-D5", "Ceramic-Disc-D10",
            "LED-3mm", "LED-5mm", "LED-10mm",
            "HC-49",
            "Ceramic-Brick-Small", "Ceramic-Brick-Medium",
        ):
            assert required in pkgs, f"missing package {required}"

    def test_DO_35_has_three_variants_and_is_glass(self) -> None:
        pkgs = load_two_pin_body_families()
        pkg = pkgs["DO-35"]
        assert {v.variant_id for v in pkg.variants} == {"S", "typ", "L"}
        # Material is glass (subminiature signal-diode body).
        for v in pkg.variants:
            assert v.material is Material.GLASS

    def test_LED_5mm_is_radial_cylinder_with_led_lens(self) -> None:
        pkgs = load_two_pin_body_families()
        led = pkgs["LED-5mm"].default_variant()
        assert led.lead_arrangement is LeadArrangement.RADIAL
        assert led.shape is BodyShape.CYLINDER
        assert led.material is Material.LED_LENS

    def test_ceramic_disc_is_radial_disc_flat(self) -> None:
        pkgs = load_two_pin_body_families()
        disc = pkgs["Ceramic-Disc-D5"].default_variant()
        assert disc.lead_arrangement is LeadArrangement.RADIAL
        assert disc.shape is BodyShape.DISC
        assert disc.disc_profile is DiscProfile.FLAT
        assert disc.material is Material.CERAMIC

    def test_HC_49_is_radial_prism_metal(self) -> None:
        pkgs = load_two_pin_body_families()
        hc49 = pkgs["HC-49"].default_variant()
        assert hc49.lead_arrangement is LeadArrangement.RADIAL
        assert hc49.shape is BodyShape.PRISM
        assert hc49.material is Material.METAL

    def test_ceramic_brick_is_sharp_edged(self) -> None:
        pkgs = load_two_pin_body_families()
        brick = pkgs["Ceramic-Brick-Small"].default_variant()
        assert brick.edge is EdgeStyle.SHARP
