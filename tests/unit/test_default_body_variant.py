# SPDX-License-Identifier: MIT
"""Library-side default body variant: per-component ``default_body_variant``.

A library YAML may pin a sensible package size to a component (e.g.
``body_variant: resistor_axial/1_2w`` on a half-watt resistor entry).
The renderer's resolution order becomes:

  1. ``ComponentInstance.body_variant`` (user override on this instance).
  2. ``ComponentDefinition.default_body_variant`` (library default).
  3. Class-default family (``_CLASS_TO_FAMILY`` fallback).

Tests cover the dataclass validation, YAML parsing and the renderer's
new precedence.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from protoboard.domain.components.definition import (
    ComponentClass,
    ComponentDefinition,
)
from protoboard.domain.components.footprints import TwoPin
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.two_pin_body import (
    BodyShape as _TPShape,
    LeadArrangement,
    TwoPinBodyFamily,
    TwoPinBodySpec,
)


def _spec(family: str, variant_id: str, length_mm: float = 5.0,
          diameter_mm: float = 2.0) -> TwoPinBodySpec:
    return TwoPinBodySpec(
        family=family, variant_id=variant_id, label=variant_id,
        lead_arrangement=LeadArrangement.AXIAL,
        shape=_TPShape.CYLINDER,
        length_mm=length_mm, diameter_mm=diameter_mm,
    )


class TestDefaultBodyVariantField:
    def test_default_is_none(self) -> None:
        defn = ComponentDefinition(
            name="R", footprint=TwoPin(),
            pin_names={}, component_class=ComponentClass.RESISTOR,
        )
        assert defn.default_body_variant is None

    def test_accepts_well_formed_string(self) -> None:
        defn = ComponentDefinition(
            name="R_1W", footprint=TwoPin(),
            pin_names={}, component_class=ComponentClass.RESISTOR,
            default_body_variant="resistor_axial/1w",
        )
        assert defn.default_body_variant == "resistor_axial/1w"

    def test_rejects_missing_slash(self) -> None:
        with pytest.raises(ValueError, match="family/variant_id"):
            ComponentDefinition(
                name="R_bad", footprint=TwoPin(),
                pin_names={}, component_class=ComponentClass.RESISTOR,
                default_body_variant="resistor_axial_only",
            )

    def test_rejects_empty_family(self) -> None:
        with pytest.raises(ValueError, match="family/variant_id"):
            ComponentDefinition(
                name="R_bad", footprint=TwoPin(),
                pin_names={}, component_class=ComponentClass.RESISTOR,
                default_body_variant="/1_4w",
            )

    def test_rejects_empty_variant(self) -> None:
        with pytest.raises(ValueError, match="family/variant_id"):
            ComponentDefinition(
                name="R_bad", footprint=TwoPin(),
                pin_names={}, component_class=ComponentClass.RESISTOR,
                default_body_variant="resistor_axial/",
            )


class TestLibraryLoader:
    def _write(self, dir_: Path, content: str) -> Path:
        f = dir_ / "lib.yaml"
        f.write_text(content, encoding="utf-8")
        return f

    def test_yaml_body_variant_field_parses(self, tmp_path: Path) -> None:
        from protoboard.services.library.loader import load_library
        self._write(tmp_path, """\
components:
  - name: R_half_watt
    description: "1/2 W resistor"
    component_class: resistor
    footprint: {type: TwoPin}
    pin_names: {}
    body_variant: "resistor_axial/1_2w"
""")
        lib = load_library([tmp_path])
        defn = lib.get("R_half_watt")
        assert defn.default_body_variant == "resistor_axial/1_2w"

    def test_yaml_default_body_variant_synonym(self, tmp_path: Path) -> None:
        """The longer ``default_body_variant`` key is accepted as a synonym."""
        from protoboard.services.library.loader import load_library
        self._write(tmp_path, """\
components:
  - name: R_one_watt
    description: "1 W resistor"
    component_class: resistor
    footprint: {type: TwoPin}
    pin_names: {}
    default_body_variant: "resistor_axial/1w"
""")
        lib = load_library([tmp_path])
        defn = lib.get("R_one_watt")
        assert defn.default_body_variant == "resistor_axial/1w"

    def test_yaml_omitted_field_defaults_to_none(self, tmp_path: Path) -> None:
        from protoboard.services.library.loader import load_library
        self._write(tmp_path, """\
components:
  - name: R_generic
    component_class: resistor
    footprint: {type: TwoPin}
    pin_names: {}
""")
        lib = load_library([tmp_path])
        defn = lib.get("R_generic")
        assert defn.default_body_variant is None

    def test_yaml_non_string_value_raises(self, tmp_path: Path) -> None:
        from protoboard.errors import SerializationError
        from protoboard.services.library.loader import load_library
        self._write(tmp_path, """\
components:
  - name: R_bad
    component_class: resistor
    footprint: {type: TwoPin}
    pin_names: {}
    body_variant: 42
""")
        with pytest.raises(SerializationError, match="body_variant"):
            load_library([tmp_path])


class TestResolutionOrder:
    """``_resolve_two_pin_spec`` precedence: instance > definition default
    > class default."""

    def _families(self) -> dict[str, TwoPinBodyFamily]:
        # Two families so we can pick between them.
        return {
            "resistor_axial": TwoPinBodyFamily(
                family="resistor_axial",
                variants=(
                    _spec("resistor_axial", "1_4w", length_mm=3.5, diameter_mm=2.0),
                    _spec("resistor_axial", "1w", length_mm=10.5, diameter_mm=4.0),
                ),
            ),
            "electrolytic_radial": TwoPinBodyFamily(
                family="electrolytic_radial",
                variants=(
                    _spec("electrolytic_radial", "5mm", length_mm=11.0,
                          diameter_mm=5.0),
                ),
            ),
        }

    def test_instance_wins_over_definition_default(self) -> None:
        from protoboard.gui.renderers.body_shapes import _resolve_two_pin_spec
        defn = ComponentDefinition(
            name="R", footprint=TwoPin(),
            pin_names={}, component_class=ComponentClass.RESISTOR,
            default_body_variant="resistor_axial/1_4w",
        )
        inst = ComponentInstance(
            id=uuid.uuid4(), definition_ref="R", refdes="R1", value="",
            body_variant="resistor_axial/1w",
        )
        spec = _resolve_two_pin_spec(defn, inst, self._families())
        assert spec is not None
        assert spec.variant_id == "1w"

    def test_definition_default_wins_over_class_fallback(self) -> None:
        from protoboard.gui.renderers.body_shapes import _resolve_two_pin_spec
        # Definition pins a non-default family entirely.
        defn = ComponentDefinition(
            name="R_radial", footprint=TwoPin(),
            pin_names={}, component_class=ComponentClass.RESISTOR,
            default_body_variant="electrolytic_radial/5mm",
        )
        spec = _resolve_two_pin_spec(defn, None, self._families())
        assert spec is not None
        assert spec.family == "electrolytic_radial"
        assert spec.variant_id == "5mm"

    def test_class_fallback_when_no_definition_default(self) -> None:
        from protoboard.gui.renderers.body_shapes import _resolve_two_pin_spec
        defn = ComponentDefinition(
            name="R", footprint=TwoPin(),
            pin_names={}, component_class=ComponentClass.RESISTOR,
        )
        spec = _resolve_two_pin_spec(defn, None, self._families())
        assert spec is not None
        assert spec.family == "resistor_axial"
        # First variant in the family.
        assert spec.variant_id == "1_4w"

    def test_invalid_definition_default_falls_through(self) -> None:
        """When the definition pins a variant that doesn't exist in the
        catalogue, fall through to the class default rather than failing."""
        from protoboard.gui.renderers.body_shapes import _resolve_two_pin_spec
        defn = ComponentDefinition(
            name="R", footprint=TwoPin(),
            pin_names={}, component_class=ComponentClass.RESISTOR,
            default_body_variant="resistor_axial/nonexistent",
        )
        spec = _resolve_two_pin_spec(defn, None, self._families())
        # Falls through to class default (first variant).
        assert spec is not None
        assert spec.variant_id == "1_4w"
