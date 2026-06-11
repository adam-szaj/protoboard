# SPDX-License-Identifier: MIT
"""Loader for ``TwoPinBodyFamily`` YAML assets — schema + bundled assets."""

from __future__ import annotations

from pathlib import Path

import pytest

from protoboard.domain.components.two_pin_body import (
    BodyShape,
    DiscProfile,
    EdgeStyle,
    LeadArrangement,
)
from protoboard.services.library.two_pin_body_loader import (
    _load_one,
    load_two_pin_body_families,
)


@pytest.fixture()
def yaml_dir(tmp_path: Path) -> Path:
    return tmp_path


def _write(dir_: Path, name: str, content: str) -> Path:
    p = dir_ / name
    p.write_text(content, encoding="utf-8")
    return p


class TestParseSingleVariant:
    def test_axial_cylinder_variant(self, yaml_dir: Path) -> None:
        path = _write(yaml_dir, "fam.yaml", """\
family: fam
variants:
  - id: v1
    label: "v1"
    lead_arrangement: axial
    shape: cylinder
    edge: rounded
    length_mm: 6.5
    diameter_mm: 2.5
    lead_diameter_mm: 0.5
""")
        fam = _load_one(path)
        assert fam.family == "fam"
        v = fam.default_variant()
        assert v.lead_arrangement is LeadArrangement.AXIAL
        assert v.shape is BodyShape.CYLINDER
        assert v.edge is EdgeStyle.ROUNDED
        assert v.length_mm == 6.5
        assert v.diameter_mm == 2.5

    def test_radial_disc_variant_with_profile(self, yaml_dir: Path) -> None:
        path = _write(yaml_dir, "fam.yaml", """\
family: fam
variants:
  - id: v1
    label: "v1"
    lead_arrangement: radial
    shape: disc
    disc_profile: candy
    polar: true
    diameter_mm: 8.0
    thickness_mm: 4.0
""")
        fam = _load_one(path)
        v = fam.default_variant()
        assert v.shape is BodyShape.DISC
        assert v.disc_profile is DiscProfile.CANDY
        assert v.polar is True

    def test_prism_variant(self, yaml_dir: Path) -> None:
        path = _write(yaml_dir, "fam.yaml", """\
family: fam
variants:
  - id: v1
    label: "v1"
    lead_arrangement: radial
    shape: prism
    edge: sharp
    length_mm: 22.0
    width_mm: 9.5
    height_mm: 9.0
""")
        v = _load_one(path).default_variant()
        assert v.shape is BodyShape.PRISM
        assert v.edge is EdgeStyle.SHARP
        assert v.width_mm == 9.5

    def test_default_edge_is_rounded(self, yaml_dir: Path) -> None:
        path = _write(yaml_dir, "fam.yaml", """\
family: fam
variants:
  - id: v1
    label: "v1"
    lead_arrangement: axial
    shape: cylinder
    length_mm: 5.0
    diameter_mm: 2.0
""")
        v = _load_one(path).default_variant()
        assert v.edge is EdgeStyle.ROUNDED


class TestMixedShapeFamily:
    def test_one_family_can_hold_mixed_shapes(self, yaml_dir: Path) -> None:
        """Capacitors come in cylinders AND discs.  The flat YAML schema
        lets a single family hold both."""
        path = _write(yaml_dir, "caps.yaml", """\
family: caps
variants:
  - id: axial_cyl
    label: "Axial cylinder"
    lead_arrangement: axial
    shape: cylinder
    polar: true
    length_mm: 7.0
    diameter_mm: 4.0
  - id: radial_disc
    label: "Radial disc"
    lead_arrangement: radial
    shape: disc
    disc_profile: flat
    diameter_mm: 5.0
    thickness_mm: 2.0
""")
        fam = _load_one(path)
        assert len(fam.variants) == 2
        assert fam.variants[0].shape is BodyShape.CYLINDER
        assert fam.variants[1].shape is BodyShape.DISC


class TestErrors:
    def test_missing_shape_raises(self, yaml_dir: Path) -> None:
        path = _write(yaml_dir, "fam.yaml", """\
family: fam
variants:
  - id: v1
    label: "v1"
    lead_arrangement: axial
    length_mm: 5.0
    diameter_mm: 2.0
""")
        with pytest.raises(ValueError, match="shape"):
            _load_one(path)

    def test_unknown_enum_value_raises(self, yaml_dir: Path) -> None:
        path = _write(yaml_dir, "fam.yaml", """\
family: fam
variants:
  - id: v1
    label: "v1"
    lead_arrangement: axial
    shape: trapezoid
    length_mm: 5.0
    diameter_mm: 2.0
""")
        with pytest.raises(ValueError, match="trapezoid"):
            _load_one(path)

    def test_no_variants_raises(self, yaml_dir: Path) -> None:
        path = _write(yaml_dir, "fam.yaml", """\
family: fam
variants: []
""")
        with pytest.raises(ValueError, match="no variants"):
            _load_one(path)


class TestDirectoryLoader:
    def test_skips_broken_yaml_keeps_good_ones(self, yaml_dir: Path) -> None:
        _write(yaml_dir, "good.yaml", """\
family: good
variants:
  - id: v1
    label: "v1"
    lead_arrangement: axial
    shape: cylinder
    length_mm: 5.0
    diameter_mm: 2.0
""")
        _write(yaml_dir, "broken.yaml", "this is not yaml: : :")
        result = load_two_pin_body_families(yaml_dir)
        assert "good" in result
        assert "broken" not in result

    def test_returns_empty_when_directory_missing(self, tmp_path: Path) -> None:
        result = load_two_pin_body_families(tmp_path / "does_not_exist")
        assert result == {}


class TestBundledAssets:
    """Smoke test: every shipped YAML loads cleanly under the new loader."""

    def test_all_shipped_families_load(self) -> None:
        families = load_two_pin_body_families()
        # Migrated legacy families.
        for required in (
            "resistor_axial",
            "inductor_axial",
            "capacitor_axial",
            "diode_axial",
        ):
            assert required in families, f"missing {required}"
        # New demonstration families.
        for new in (
            "led_radial",
            "electrolytic_radial",
            "ceramic_disc",
            "resistor_radial_brick",
        ):
            assert new in families, f"missing new family {new}"

    def test_led_is_radial_cylinder(self) -> None:
        families = load_two_pin_body_families()
        led = families["led_radial"].default_variant()
        assert led.lead_arrangement is LeadArrangement.RADIAL
        assert led.shape is BodyShape.CYLINDER
        assert led.polar is True

    def test_ceramic_disc_is_radial_disc(self) -> None:
        families = load_two_pin_body_families()
        disc = families["ceramic_disc"].default_variant()
        assert disc.lead_arrangement is LeadArrangement.RADIAL
        assert disc.shape is BodyShape.DISC
        assert disc.disc_profile is DiscProfile.FLAT

    def test_brick_is_radial_prism(self) -> None:
        families = load_two_pin_body_families()
        brick = families["resistor_radial_brick"].default_variant()
        assert brick.lead_arrangement is LeadArrangement.RADIAL
        assert brick.shape is BodyShape.PRISM
        assert brick.edge is EdgeStyle.SHARP


class TestLegacyLoaderTolerance:
    """The old BodyTypeSpec loader must still read the migrated YAMLs
    so the renderer keeps working until step 4 swaps it out."""

    def test_legacy_loader_reads_migrated_resistor(self) -> None:
        from protoboard.services.library.body_type_loader import (
            load_body_types,
        )
        legacy = load_body_types()
        assert "resistor_axial" in legacy
        spec = legacy["resistor_axial"]
        assert spec.shape == "cylinder"
        # 1/4 W variant survives.
        v = spec.get_variant("1_4w")
        assert v is not None
        assert v.diameter_mm == 2.0

    def test_legacy_loader_skips_non_cylinder_families(self) -> None:
        from protoboard.services.library.body_type_loader import (
            load_body_types,
        )
        legacy = load_body_types()
        # ceramic_disc has shape=disc per variant — legacy spec only
        # models cylinder, so this family drops out.  Other families
        # still load.
        assert "ceramic_disc" not in legacy
        assert "resistor_radial_brick" not in legacy
