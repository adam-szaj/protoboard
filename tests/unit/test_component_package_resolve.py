# SPDX-License-Identifier: MIT
"""Tests for resolve_component_package — variant-ref precedence."""

from __future__ import annotations

import pytest

from protoboard.domain.components.definition import (
    ComponentDefinition,
    MetaPin,
    PackageOption,
)
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.package import (
    PackageFamily,
    PackageVariant,
    resolve_component_package,
    resolve_package_variant_footprint,
)
from protoboard.domain.components.package.loader import build_package
from protoboard.domain.components.package.shapes import Cylinder


def _cyl(diameter: float, length: float) -> object:
    return build_package(
        {
            "shape": "cylinder",
            "dims": {"diameter": diameter, "length": length},
            "bend": 0,
            "leads": {
                "direction": [[0, 0, -1], [0, 0, 1]],
                "body_offset": 0,
                "board_offset": "solve",
                "lead_r": 0.2,
                "lead_length": 5.0,
            },
        },
        "C",
    )


def _families() -> dict[str, PackageFamily]:
    fam = PackageFamily(
        family_id="test/axial",
        variants=(
            PackageVariant("compact", "Small", _cyl(1.5, 3.0)),  # type: ignore[arg-type]
            PackageVariant("nominal", "Typical", _cyl(1.8, 3.7)),  # type: ignore[arg-type]
        ),
        default_id="nominal",
    )
    return {"test/axial": fam}


def _defn(**kw: object) -> ComponentDefinition:
    package_ref = kw.pop("package_ref", None)
    if package_ref is not None and "package_options" not in kw:
        kw["package_options"] = (
            PackageOption(name=str(package_ref), ref=str(package_ref)),
        )
    return ComponentDefinition(
        name="X",
        meta_pins=(MetaPin(("A",)), MetaPin(("B",))),
        **kw,  # type: ignore[arg-type]
    )


class TestResolvePrecedence:
    @pytest.mark.parametrize("address", ["test/axial/compact", "unrelated/std"])
    def test_existing_but_undeclared_variant_is_rejected(self, address: str) -> None:
        families = _families()
        families["unrelated"] = PackageFamily(
            family_id="unrelated",
            variants=(PackageVariant("std", "Unrelated", _cyl(2.0, 4.0)),),  # type: ignore[arg-type]
        )
        definition = _defn(package_ref="test/axial/nominal")
        instance = ComponentInstance.new("X", "X1", "", package_variant=address)

        with pytest.raises(ValueError, match="package option"):
            resolve_component_package(definition, instance, families)

        with pytest.raises(ValueError, match="package option"):
            resolve_package_variant_footprint(definition, address, families)

    def test_declared_family_allows_its_concrete_variants_without_binding(self) -> None:
        definition = _defn(package_ref="test/axial")
        instance = ComponentInstance.new(
            "X", "X1", "", package_variant="test/axial/compact"
        )
        family = _families()["test/axial"]
        families = {family.family_id: family}

        package = resolve_component_package(definition, instance, families)
        preview = resolve_package_variant_footprint(
            definition, "test/axial/compact", families
        )

        assert package is family.get_variant("compact").package  # type: ignore[union-attr]
        assert preview is package

    def test_family_ref_resolves(self) -> None:
        d = _defn(package_ref="test/axial/compact")
        pkg = resolve_component_package(d, None, _families())
        assert isinstance(pkg, Cylinder)
        assert pkg.length_mm == 3.0

    def test_bare_family_ref_uses_default(self) -> None:
        d = _defn(package_ref="test/axial")
        pkg = resolve_component_package(d, None, _families())
        assert pkg.length_mm == 3.7  # type: ignore[union-attr]

    def test_instance_override_beats_family_ref(self) -> None:
        d = _defn(package_ref="test/axial")
        inst = ComponentInstance.new(
            definition_ref="X",
            refdes="X1",
            value="",
            package_variant="test/axial/compact",
        )
        pkg = resolve_component_package(d, inst, _families())
        assert pkg.length_mm == 3.0  # type: ignore[union-attr]

    def test_unresolved_definition_ref_raises(self) -> None:
        d = _defn(package_ref="NOPE/x")
        with pytest.raises(ValueError, match="NOPE/x"):
            resolve_component_package(d, None, _families())

    def test_unresolved_instance_override_raises(self) -> None:
        d = _defn(package_ref="test/axial/nominal")
        inst = ComponentInstance.new(
            definition_ref="X",
            refdes="X1",
            value="",
            package_variant="NOPE/x",
        )
        with pytest.raises(ValueError, match="NOPE/x"):
            resolve_component_package(d, inst, _families())

    def test_empty_instance_override_does_not_use_default(self) -> None:
        definition = _defn(package_ref="test/axial/nominal")
        instance = ComponentInstance.new("X", "X1", "", package_variant="")

        with pytest.raises(ValueError, match="package option"):
            resolve_component_package(definition, instance, _families())

    def test_definition_without_package_option_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="package option"):
            ComponentDefinition(name="X", meta_pins=(MetaPin(("A",)),))


class TestFieldValidation:
    def test_bad_family_ref_shape_raises(self) -> None:
        with pytest.raises(ValueError):
            PackageOption(name="bad", ref="")

    def test_good_family_ref_ok(self) -> None:
        assert _defn(package_ref="a/b").default_package_ref() == "a/b"


class TestResolvedFootprint:
    """Bound instances expose their chosen package as footprint geometry."""

    def test_variant_footprint_is_resolved_package(self) -> None:
        d = _defn(package_ref="test/axial/compact")
        inst = ComponentInstance.new(
            definition_ref="X",
            refdes="X1",
            value="",
        ).bind(d, _families())
        fp = inst.resolved_footprint()
        assert isinstance(fp, Cylinder)
        assert fp is inst.resolved_package()

    def test_variant_differs_from_default_footprint(self) -> None:
        # test/axial/compact vs test/axial/nominal have different lengths → different pin spacing.  # noqa: E501
        d = _defn(package_ref="test/axial")
        # Instance overrides to test/axial/compact; both bound to the same catalogue.
        inst = ComponentInstance.new(
            definition_ref="X",
            refdes="X1",
            value="",
            package_variant="test/axial/compact",
        ).bind(d, _families())
        # No instance-level override → resolves the defn default (test/axial/nominal).
        inst_default = ComponentInstance.new(
            definition_ref="X",
            refdes="X2",
            value="",
        ).bind(d, _families())
        fp_inst = inst.resolved_footprint()
        fp_default = inst_default.resolved_footprint()
        assert fp_inst.pin_positions() != fp_default.pin_positions()

    def test_unbound_instance_raises(self) -> None:
        from protoboard.errors import UnboundInstanceError

        inst = ComponentInstance.new("X", "X1", "")
        with pytest.raises(UnboundInstanceError, match="bind"):
            inst.resolved_footprint()

    def test_preplacement_variant_footprint_uses_selected_variant(self) -> None:
        d = _defn(package_ref="test/axial")
        chosen = resolve_package_variant_footprint(
            d,
            "test/axial/compact",
            _families(),
        )
        default = d.default_footprint(_families())
        assert chosen.pin_positions() != default.pin_positions()

    def test_preplacement_unresolved_variant_raises(self) -> None:
        d = _defn(package_ref="test/axial/nominal")
        with pytest.raises(ValueError, match="NOPE/x"):
            resolve_package_variant_footprint(d, "NOPE/x", _families())

    def test_preplacement_empty_variant_does_not_use_default(self) -> None:
        definition = _defn(package_ref="test/axial/nominal")

        with pytest.raises(ValueError, match="package option"):
            resolve_package_variant_footprint(definition, "", _families())


class TestResolvePinWorldPositionVariant:
    """resolve_pin_world_position honours the chosen variant when given the
    catalogue — the placed component's pin lands on the variant geometry, not
    the definition default. This is the renderer/session path that drove the
    'SMD pin sits 3.2 holes away instead of 1' bug."""

    def test_variant_pin_position_differs_with_families(self) -> None:

        from protoboard.domain.layout.placement import (
            ComponentPlacement,
            resolve_pin_world_position,
        )
        from protoboard.geometry import Point

        d = _defn(package_ref="test/axial")
        # Bound WITH the catalogue → the instance resolves its test/axial/compact variant.  # noqa: E501
        inst_with = ComponentInstance.new(
            definition_ref="X",
            refdes="X1",
            value="",
            package_variant="test/axial/compact",
        ).bind(d, _families())
        # Bound WITHOUT a catalogue → the selected variant cannot resolve.
        inst_without = ComponentInstance.new(
            definition_ref="X",
            refdes="X1",
            value="",
            package_variant="test/axial/compact",
        ).bind(d, {})
        cid = inst_with.id
        pl = ComponentPlacement(
            component_id=cid, position=Point(0.0, 0.0), rotation=0, side="top"
        )

        without = resolve_pin_world_position(
            cid,
            2,
            placements={cid: pl},
            instances={cid: inst_without},
            components={"X": d},
        )
        with_fam = resolve_pin_world_position(
            cid,
            2,
            placements={cid: pl},
            instances={cid: inst_with},
            components={"X": d},
        )
        assert without is None
        assert with_fam is not None
