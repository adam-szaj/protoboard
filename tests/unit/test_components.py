# SPDX-License-Identifier: MIT
"""Tests for ComponentDefinition and ComponentInstance."""

import dataclasses
import uuid

import pytest

from protoboard.domain.components.definition import (
    REFDES_PREFIX,
    ComponentClass,
    ComponentDefinition,
    MetaPin,
    PackageOption,
)
from protoboard.domain.components.instance import ComponentInstance


def _definition(
    name: str = "R",
    labels: tuple[tuple[str, ...], ...] = (("A",), ("B",)),
    **kwargs: object,
) -> ComponentDefinition:
    return ComponentDefinition(
        name=name,
        meta_pins=tuple(MetaPin(item) for item in labels),
        package_options=(
            PackageOption(
                name="test",
                ref="test/std",
                pin_map={pin: (pin,) for pin in range(1, len(labels) + 1)},
            ),
        ),
        **kwargs,  # type: ignore[arg-type]
    )


class TestAppearance:
    def test_resolve_per_field_precedence(self) -> None:
        from protoboard.domain.components.appearance import Appearance

        # Instance wins per field; definition fills the gaps.
        eff = Appearance.resolve(
            Appearance(color="#111"),  # instance: color only
            Appearance(color="#222", corner_radius_mm=(0.5,)),  # defn: color + radius
        )
        assert eff.color == "#111"  # instance color wins
        assert eff.corner_radius_mm == (0.5,)  # radius falls through to defn
        assert eff.corner_radius_mm_scalar == 0.5

    def test_resolve_all_none_is_empty(self) -> None:
        from protoboard.domain.components.appearance import Appearance

        assert Appearance.resolve(None, None).is_empty
        assert Appearance().is_empty
        assert not Appearance(color="#fff").is_empty

    def test_negative_radius_rejected(self) -> None:
        from protoboard.domain.components.appearance import Appearance

        with pytest.raises(ValueError):
            Appearance(corner_radius_mm=(-1.0,))

    def test_too_many_radii_rejected(self) -> None:
        from protoboard.domain.components.appearance import Appearance

        with pytest.raises(ValueError):
            Appearance(corner_radius_mm=(1.0, 1.0, 1.0, 1.0, 1.0))

    def test_scalar_is_first_value(self) -> None:
        from protoboard.domain.components.appearance import Appearance

        assert Appearance(corner_radius_mm=(2.0, 3.0)).corner_radius_mm_scalar == 2.0
        assert Appearance().corner_radius_mm_scalar is None

    def test_corner_expansion_css_border_radius_order(self) -> None:
        # (top-left, top-right, bottom-right, bottom-left) per CSS shorthand:
        # 1 value → all; 2 → TL/BR, TR/BL; 3 → TL, TR/BL, BR; 4 → verbatim.
        from protoboard.domain.components.appearance import Appearance

        def corners(*radii: float) -> tuple[float, float, float, float] | None:
            return Appearance(corner_radius_mm=radii).corner_radius_mm_corners

        assert corners(1.0) == (1.0, 1.0, 1.0, 1.0)
        assert corners(1.0, 2.0) == (1.0, 2.0, 1.0, 2.0)
        assert corners(1.0, 2.0, 3.0) == (1.0, 2.0, 3.0, 2.0)
        assert corners(1.0, 2.0, 3.0, 4.0) == (1.0, 2.0, 3.0, 4.0)
        assert Appearance().corner_radius_mm_corners is None

    def test_parse_validates(self) -> None:
        from protoboard.domain.components.appearance import (
            Appearance,
            parse_style,
        )

        assert parse_style(None, ctx="t") is None
        assert parse_style("", ctx="t") is None  # empty → None
        assert parse_style("   ", ctx="t") is None  # whitespace → None
        assert parse_style("color: #abc", ctx="t") == Appearance(color="#abc")
        with pytest.raises(ValueError):
            parse_style("color: red", ctx="t")  # bad hex
        with pytest.raises(ValueError):
            parse_style("corner-radius: -2", ctx="t")  # negative
        with pytest.raises(ValueError):
            parse_style("fill: #abc", ctx="t")  # unknown property
        with pytest.raises(ValueError):
            parse_style("color #abc", ctx="t")  # missing colon
        with pytest.raises(ValueError):
            parse_style(42, ctx="t")  # non-string

    def test_parse_multi_value_and_combined(self) -> None:
        from protoboard.domain.components.appearance import (
            Appearance,
            parse_style,
        )

        assert parse_style("corner-radius: 2 2 0 0", ctx="t") == Appearance(
            corner_radius_mm=(2.0, 2.0, 0.0, 0.0)
        )
        assert parse_style("color: #1a1a1a; corner-radius: 0.4", ctx="t") == (
            Appearance(color="#1a1a1a", corner_radius_mm=(0.4,))
        )
        # Duplicate property: last wins (CSS behaviour).
        assert parse_style("color: #111; color: #222", ctx="t") == Appearance(
            color="#222"
        )
        with pytest.raises(ValueError):
            parse_style("corner-radius: 1 1 1 1 1", ctx="t")  # > 4 values

    def test_parse_alpha_hex_forms(self) -> None:
        # CSS channel order — alpha LAST (#rrggbbaa / #rgba); stored verbatim,
        # the renderer reorders for Qt (qcolor_from_css).
        from protoboard.domain.components.appearance import (
            Appearance,
            parse_style,
        )

        assert parse_style("color: #ff000080", ctx="t") == Appearance(color="#ff000080")
        assert parse_style("color: #f008", ctx="t") == Appearance(color="#f008")
        with pytest.raises(ValueError):
            parse_style("color: #ff00080", ctx="t")  # 7 digits
        with pytest.raises(ValueError):
            parse_style("color: #ff0000801", ctx="t")  # 9 digits

    def test_to_style_string_roundtrip(self) -> None:
        from protoboard.domain.components.appearance import (
            Appearance,
            parse_style,
        )

        for a in (
            Appearance(color="#1a1a1a"),
            Appearance(color="#1a1a1a80"),
            Appearance(corner_radius_mm=(0.4,)),
            Appearance(color="#1a1a1a", corner_radius_mm=(0.3,)),
            Appearance(corner_radius_mm=(2.0, 2.0, 0.0, 0.0)),
        ):
            assert parse_style(a.to_style_string(), ctx="rt") == a


class TestComponentClass:
    def test_refdes_prefix_total(self) -> None:
        # refdes_prefix indexes REFDES_PREFIX with no fallback, so every class
        # MUST have a prefix or a placed component of that class crashes.
        for cls in ComponentClass:
            assert cls in REFDES_PREFIX, f"REFDES_PREFIX missing {cls}"

    def test_devboard_prefix(self) -> None:
        assert REFDES_PREFIX[ComponentClass.DEVBOARD] == "U"


class TestPackageOption:
    def test_ref_only_is_valid(self) -> None:
        opt = PackageOption(name="DIP-14", ref="dip-14/std")
        assert opt.ref == "dip-14/std"
        assert opt.pin_map == {}

    def test_requires_ref(self) -> None:
        with pytest.raises(ValueError):
            PackageOption(name="x", ref="")

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValueError):
            PackageOption(name="", ref="a/b")


class TestComponentSymbol:
    def test_symbol_drives_refdes_prefix(self) -> None:
        defn = _definition(
            name="LED_5mm",
            component_class=ComponentClass.DIODE_LED,
            symbol="LED",
        )
        assert defn.refdes_prefix == "LED"

    def test_refdes_prefix_falls_back_to_class(self) -> None:
        # No explicit symbol → the class default (RESISTOR → "R").
        defn = _definition(
            name="R",
            component_class=ComponentClass.RESISTOR,
        )
        assert defn.symbol is None
        assert defn.refdes_prefix == "R"


class TestComponentDefinition:
    def test_package_option_becomes_default_package_ref(self) -> None:
        defn = ComponentDefinition(
            name="D",
            meta_pins=(MetaPin(("A",)), MetaPin(("K",))),
            package_options=(PackageOption(name="d-tht/DO/35", ref="d-tht/DO/35"),),
        )
        assert defn.default_package_ref() == "d-tht/DO/35"
        assert defn.default_package_option() is not None
        assert defn.default_package_option().ref == "d-tht/DO/35"  # type: ignore[union-attr]

    def test_first_package_is_the_default_package_option(self) -> None:
        defn = ComponentDefinition(
            name="R",
            meta_pins=(MetaPin(("A",)), MetaPin(("B",))),
            package_options=(
                PackageOption(name="A", ref="r-tht/DIN/0207"),
                PackageOption(name="B", ref="d-tht/DO/35"),
            ),
        )
        assert defn.default_package_ref() == "r-tht/DIN/0207"

    def test_meta_pin_labels(self) -> None:
        defn = ComponentDefinition(
            name="LM358",
            meta_pins=(
                MetaPin(("OUT_A", "GPIO1")),
                MetaPin(("IN-_A",)),
                MetaPin(("VCC", "POWER")),
            ),
            package_options=(PackageOption("DIP", "dip-8/std"),),
        )
        assert defn.name == "LM358"
        assert defn.meta_pin(1).labels[0] == "OUT_A"
        assert defn.meta_pin(1).labels == ("OUT_A", "GPIO1")
        assert defn.meta_pin(3).labels == ("VCC", "POWER")

    def test_pin_name_out_of_range_raises(self) -> None:
        defn = _definition()
        with pytest.raises(ValueError):
            defn.meta_pin(0).labels[0]
        with pytest.raises(ValueError):
            defn.meta_pin(3).labels[0]

    def test_meta_pin_out_of_range_raises(self) -> None:
        defn = _definition()
        with pytest.raises(ValueError):
            defn.meta_pin(3)

    def test_pin_count_property(self) -> None:
        defn = _definition(labels=tuple((str(pin),) for pin in range(1, 9)))
        assert len(defn.meta_pins) == 8

    def test_optional_description(self) -> None:
        defn = _definition(name="LM358", description="Dual op-amp")
        assert defn.description == "Dual op-amp"

    def test_default_description_is_empty(self) -> None:
        defn = _definition()
        assert defn.description == ""

    def test_metadata_default_empty(self) -> None:
        defn = _definition()
        assert defn.metadata == {}

    def test_metadata_carries_user_data(self) -> None:
        defn = _definition(
            name="R",
            metadata={"datasheet": "https://example.com/r.pdf"},
        )
        assert defn.metadata["datasheet"] == "https://example.com/r.pdf"

    def test_frozen(self) -> None:
        defn = _definition()
        with pytest.raises(dataclasses.FrozenInstanceError):
            defn.name = "C"  # type: ignore[misc]


class TestComponentInstance:
    def test_construct(self) -> None:
        inst_id = uuid.uuid4()
        inst = ComponentInstance(
            id=inst_id,
            definition_ref="LM358",
            refdes="U1",
            value="LM358",
        )
        assert inst.id == inst_id
        assert inst.definition_ref == "LM358"
        assert inst.refdes == "U1"
        assert inst.value == "LM358"

    def test_two_instances_have_distinct_ids_when_created_with_helper(self) -> None:
        # ComponentInstance.new() is the convenience constructor that
        # generates a fresh UUID.
        a = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
        b = ComponentInstance.new(definition_ref="R", refdes="R2", value="10k")
        assert a.id != b.id

    def test_refdes_can_change_independently_of_id(self) -> None:
        # The same instance can be renamed (refdes change) without affecting
        # connectivity references — the ID is the stable key.
        inst = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
        # Construct a "renamed" copy.
        renamed = dataclasses.replace(inst, refdes="R5")
        assert renamed.id == inst.id
        assert renamed.refdes == "R5"

    def test_empty_refdes_raises(self) -> None:
        with pytest.raises(ValueError):
            ComponentInstance.new(definition_ref="R", refdes="", value="10k")

    def test_empty_definition_ref_raises(self) -> None:
        with pytest.raises(ValueError):
            ComponentInstance.new(definition_ref="", refdes="R1", value="10k")

    def test_frozen(self) -> None:
        inst = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
        with pytest.raises(dataclasses.FrozenInstanceError):
            inst.refdes = "R2"  # type: ignore[misc]
