# SPDX-License-Identifier: MIT
"""Parametric two-pin body spec — construction, validation, pitch helpers."""

from __future__ import annotations

import pytest

from protoboard.domain.components.two_pin_body import (
    BodyShape,
    DiscProfile,
    EdgeStyle,
    LeadArrangement,
    TwoPinBodyPackage,
    TwoPinBodySpec,
)


class TestCylinderSpec:
    def test_cylinder_with_length_and_diameter_constructs(self) -> None:
        spec = TwoPinBodySpec(
            package_id="resistor_axial",
            variant_id="quarter_watt",
            label="1/4 W",
            lead_arrangement=LeadArrangement.AXIAL,
            shape=BodyShape.CYLINDER,
            length_mm=6.5,
            diameter_mm=2.5,
        )
        assert spec.edge is EdgeStyle.ROUNDED
        # Material defaults to PLASTIC.
        from protoboard.domain.components.two_pin_body import Material
        assert spec.material is Material.PLASTIC

    def test_cylinder_missing_diameter_raises(self) -> None:
        with pytest.raises(ValueError, match="diameter_mm"):
            TwoPinBodySpec(
                package_id="resistor_axial",
                variant_id="bad",
                label="bad",
                lead_arrangement=LeadArrangement.AXIAL,
                shape=BodyShape.CYLINDER,
                length_mm=6.5,
            )

    def test_cylinder_missing_length_raises(self) -> None:
        with pytest.raises(ValueError, match="length_mm"):
            TwoPinBodySpec(
                package_id="resistor_axial",
                variant_id="bad",
                label="bad",
                lead_arrangement=LeadArrangement.AXIAL,
                shape=BodyShape.CYLINDER,
                diameter_mm=2.5,
            )

    def test_cylinder_with_disc_profile_raises(self) -> None:
        # disc_profile only applies to BodyShape.DISC.
        with pytest.raises(ValueError, match="disc_profile only valid"):
            TwoPinBodySpec(
                package_id="x", variant_id="v", label="v",
                lead_arrangement=LeadArrangement.AXIAL,
                shape=BodyShape.CYLINDER,
                length_mm=5.0, diameter_mm=3.0,
                disc_profile=DiscProfile.CANDY,
            )


class TestPrismSpec:
    def test_prism_with_three_dimensions(self) -> None:
        spec = TwoPinBodySpec(
            package_id="ceramic_brick",
            variant_id="0805",
            label="0805 brick",
            lead_arrangement=LeadArrangement.RADIAL,
            shape=BodyShape.PRISM,
            length_mm=2.0, width_mm=1.25, height_mm=1.0,
        )
        assert spec.shape is BodyShape.PRISM

    def test_prism_missing_height_raises(self) -> None:
        with pytest.raises(ValueError, match="height_mm"):
            TwoPinBodySpec(
                package_id="x", variant_id="v", label="v",
                lead_arrangement=LeadArrangement.RADIAL,
                shape=BodyShape.PRISM,
                length_mm=2.0, width_mm=1.0,
            )


class TestDiscSpec:
    def test_disc_with_profile_constructs(self) -> None:
        spec = TwoPinBodySpec(
            package_id="ceramic_disc",
            variant_id="5mm",
            label="5 mm ceramic",
            lead_arrangement=LeadArrangement.RADIAL,
            shape=BodyShape.DISC,
            diameter_mm=5.0, thickness_mm=2.0,
            disc_profile=DiscProfile.FLAT,
        )
        assert spec.disc_profile is DiscProfile.FLAT

    def test_disc_without_profile_raises(self) -> None:
        with pytest.raises(ValueError, match="requires disc_profile"):
            TwoPinBodySpec(
                package_id="x", variant_id="v", label="v",
                lead_arrangement=LeadArrangement.RADIAL,
                shape=BodyShape.DISC,
                diameter_mm=5.0, thickness_mm=2.0,
            )

    def test_disc_missing_thickness_raises(self) -> None:
        with pytest.raises(ValueError, match="thickness_mm"):
            TwoPinBodySpec(
                package_id="x", variant_id="v", label="v",
                lead_arrangement=LeadArrangement.RADIAL,
                shape=BodyShape.DISC,
                diameter_mm=5.0,
                disc_profile=DiscProfile.CANDY,
            )

    def test_disc_does_not_need_length(self) -> None:
        # Sanity: DISC's axial dimension is thickness_mm, not length_mm.
        TwoPinBodySpec(
            package_id="x", variant_id="v", label="v",
            lead_arrangement=LeadArrangement.RADIAL,
            shape=BodyShape.DISC,
            diameter_mm=5.0, thickness_mm=2.0,
            disc_profile=DiscProfile.FLAT,
        )


class TestLeadDiameter:
    def test_zero_lead_diameter_raises(self) -> None:
        with pytest.raises(ValueError, match="lead_diameter_mm"):
            TwoPinBodySpec(
                package_id="x", variant_id="v", label="v",
                lead_arrangement=LeadArrangement.AXIAL,
                shape=BodyShape.CYLINDER,
                length_mm=5.0, diameter_mm=3.0,
                lead_diameter_mm=0.0,
            )

    def test_negative_lead_diameter_raises(self) -> None:
        with pytest.raises(ValueError, match="lead_diameter_mm"):
            TwoPinBodySpec(
                package_id="x", variant_id="v", label="v",
                lead_arrangement=LeadArrangement.AXIAL,
                shape=BodyShape.CYLINDER,
                length_mm=5.0, diameter_mm=3.0,
                lead_diameter_mm=-0.5,
            )


class TestPitchHelpers:
    def test_radial_pitch_cylinder_uses_diameter(self) -> None:
        spec = TwoPinBodySpec(
            package_id="x", variant_id="v", label="v",
            lead_arrangement=LeadArrangement.RADIAL,
            shape=BodyShape.CYLINDER,
            length_mm=7.0, diameter_mm=4.0, lead_diameter_mm=0.5,
        )
        assert spec.min_radial_pitch_mm() == pytest.approx((4.0 + 0.5) / 2.0)

    def test_radial_pitch_disc_uses_diameter(self) -> None:
        spec = TwoPinBodySpec(
            package_id="x", variant_id="v", label="v",
            lead_arrangement=LeadArrangement.RADIAL,
            shape=BodyShape.DISC,
            diameter_mm=10.0, thickness_mm=5.0,
            disc_profile=DiscProfile.CANDY, lead_diameter_mm=0.6,
        )
        assert spec.min_radial_pitch_mm() == pytest.approx((10.0 + 0.6) / 2.0)

    def test_radial_pitch_prism_uses_width(self) -> None:
        spec = TwoPinBodySpec(
            package_id="x", variant_id="v", label="v",
            lead_arrangement=LeadArrangement.RADIAL,
            shape=BodyShape.PRISM,
            length_mm=3.0, width_mm=2.0, height_mm=1.0, lead_diameter_mm=0.5,
        )
        assert spec.min_radial_pitch_mm() == pytest.approx((2.0 + 0.5) / 2.0)

    def test_axial_pitch_cylinder_uses_length(self) -> None:
        spec = TwoPinBodySpec(
            package_id="x", variant_id="v", label="v",
            lead_arrangement=LeadArrangement.AXIAL,
            shape=BodyShape.CYLINDER,
            length_mm=6.5, diameter_mm=2.5, lead_diameter_mm=0.5,
        )
        assert spec.min_axial_pitch_mm() == pytest.approx(6.5 + 0.5)

    def test_axial_pitch_disc_uses_thickness(self) -> None:
        spec = TwoPinBodySpec(
            package_id="x", variant_id="v", label="v",
            lead_arrangement=LeadArrangement.AXIAL,
            shape=BodyShape.DISC,
            diameter_mm=10.0, thickness_mm=2.0,
            disc_profile=DiscProfile.FLAT, lead_diameter_mm=0.5,
        )
        assert spec.min_axial_pitch_mm() == pytest.approx(2.0 + 0.5)


class TestFrozen:
    def test_spec_is_immutable(self) -> None:
        from dataclasses import FrozenInstanceError
        spec = TwoPinBodySpec(
            package_id="x", variant_id="v", label="v",
            lead_arrangement=LeadArrangement.AXIAL,
            shape=BodyShape.CYLINDER,
            length_mm=5.0, diameter_mm=2.0,
        )
        with pytest.raises(FrozenInstanceError):
            spec.length_mm = 10.0  # type: ignore[misc]


class TestFamily:
    def _spec(self, vid: str, package_id: str = "fam") -> TwoPinBodySpec:
        return TwoPinBodySpec(
            package_id=package_id, variant_id=vid, label=vid,
            lead_arrangement=LeadArrangement.AXIAL,
            shape=BodyShape.CYLINDER,
            length_mm=5.0, diameter_mm=2.0,
        )

    def test_family_default_is_first_variant(self) -> None:
        fam = TwoPinBodyPackage(
            package_id="fam",
            variants=(self._spec("a"), self._spec("b")),
        )
        assert fam.default_variant().variant_id == "a"

    def test_family_get_variant_returns_match(self) -> None:
        fam = TwoPinBodyPackage(
            package_id="fam",
            variants=(self._spec("a"), self._spec("b")),
        )
        assert fam.get_variant("b") is not None
        assert fam.get_variant("nonexistent") is None

    def test_empty_family_raises(self) -> None:
        with pytest.raises(ValueError, match="no variants"):
            TwoPinBodyPackage(package_id="empty", variants=())

    def test_mismatched_family_id_raises(self) -> None:
        with pytest.raises(ValueError, match="contains variant from"):
            TwoPinBodyPackage(
                package_id="fam",
                variants=(self._spec("a", package_id="other"),),
            )

    def test_duplicate_variant_ids_raise(self) -> None:
        with pytest.raises(ValueError, match="duplicate variant ids"):
            TwoPinBodyPackage(
                package_id="fam",
                variants=(self._spec("a"), self._spec("a")),
            )


class TestCanonicalRecipes:
    """Sanity check: the four package archetypes the user named all
    construct cleanly with realistic dimensions."""

    def test_resistor_axial_cylinder(self) -> None:
        TwoPinBodySpec(
            package_id="resistor_axial", variant_id="quarter_watt",
            label="1/4 W", lead_arrangement=LeadArrangement.AXIAL,
            shape=BodyShape.CYLINDER, edge=EdgeStyle.ROUNDED,
            length_mm=6.5, diameter_mm=2.5,
        )

    def test_electrolytic_radial_cylinder(self) -> None:
        TwoPinBodySpec(
            package_id="electrolytic_radial", variant_id="6mm",
            label="6 mm radial", lead_arrangement=LeadArrangement.RADIAL,
            shape=BodyShape.CYLINDER, edge=EdgeStyle.ROUNDED,
            length_mm=11.0, diameter_mm=6.3,
        )

    def test_ceramic_disc_flat(self) -> None:
        TwoPinBodySpec(
            package_id="ceramic_disc", variant_id="5mm",
            label="5 mm ceramic", lead_arrangement=LeadArrangement.RADIAL,
            shape=BodyShape.DISC, edge=EdgeStyle.SHARP,
            diameter_mm=5.0, thickness_mm=2.0,
            disc_profile=DiscProfile.FLAT,
        )

    def test_tantalum_disc_candy(self) -> None:
        TwoPinBodySpec(
            package_id="tantalum", variant_id="3u3",
            label="3.3 µF tantalum", lead_arrangement=LeadArrangement.RADIAL,
            shape=BodyShape.DISC, edge=EdgeStyle.ROUNDED,
            diameter_mm=3.5, thickness_mm=2.5,
            disc_profile=DiscProfile.CANDY,
        )

    def test_ceramic_brick_prism(self) -> None:
        TwoPinBodySpec(
            package_id="ceramic_brick", variant_id="0805",
            label="0805", lead_arrangement=LeadArrangement.RADIAL,
            shape=BodyShape.PRISM, edge=EdgeStyle.SHARP,
            length_mm=2.0, width_mm=1.25, height_mm=1.0,
        )
