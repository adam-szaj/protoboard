# SPDX-License-Identifier: MIT
"""Library-side default body variant: per-component ``default_body_variant``.

A library YAML pins the package + variant on the component
definition (``body_variant: DO-35/typ``).  The renderer's resolution
order is:

  1. ``ComponentInstance.body_variant`` (user override on this instance).
  2. ``ComponentDefinition.default_body_variant`` (library default).
  3. ``None`` → renderer falls back to :class:`GenericBody`.

There is no implicit ``component_class → family`` mapping: packages
are physical bodies, classes are flavours.  A two-pin component
without a body_variant draws as a plain rectangle.

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
    TwoPinBodyPackage,
    TwoPinBodySpec,
)


def _spec(package_id: str, variant_id: str, length_mm: float = 5.0,
          diameter_mm: float = 2.0) -> TwoPinBodySpec:
    return TwoPinBodySpec(
        package_id=package_id, variant_id=variant_id, label=variant_id,
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
            default_body_variant="DIN-0617/typ",
        )
        assert defn.default_body_variant == "DIN-0617/typ"

    def test_rejects_missing_slash(self) -> None:
        with pytest.raises(ValueError, match="family/variant_id"):
            ComponentDefinition(
                name="R_bad", footprint=TwoPin(),
                pin_names={}, component_class=ComponentClass.RESISTOR,
                default_body_variant="DIN-0617",
            )

    def test_rejects_empty_family(self) -> None:
        with pytest.raises(ValueError, match="family/variant_id"):
            ComponentDefinition(
                name="R_bad", footprint=TwoPin(),
                pin_names={}, component_class=ComponentClass.RESISTOR,
                default_body_variant="/typ",
            )

    def test_rejects_empty_variant(self) -> None:
        with pytest.raises(ValueError, match="family/variant_id"):
            ComponentDefinition(
                name="R_bad", footprint=TwoPin(),
                pin_names={}, component_class=ComponentClass.RESISTOR,
                default_body_variant="DIN-0617/",
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
    body_variant: "DIN-0411/typ"
""")
        lib = load_library([tmp_path])
        defn = lib.get("R_half_watt")
        assert defn.default_body_variant == "DIN-0411/typ"

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
    default_body_variant: "DIN-0617/typ"
""")
        lib = load_library([tmp_path])
        defn = lib.get("R_one_watt")
        assert defn.default_body_variant == "DIN-0617/typ"

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
    > None (no class-based fallback)."""

    def _pkgs(self) -> dict[str, TwoPinBodyPackage]:
        return {
            "DO-35": TwoPinBodyPackage(
                package_id="DO-35", label="DO-35",
                variants=(
                    _spec("DO-35", "S", length_mm=3.0, diameter_mm=1.5),
                    _spec("DO-35", "typ", length_mm=3.7, diameter_mm=1.8),
                ),
            ),
            "DO-41": TwoPinBodyPackage(
                package_id="DO-41", label="DO-41",
                variants=(
                    _spec("DO-41", "typ", length_mm=5.2, diameter_mm=2.7),
                ),
            ),
        }

    def test_instance_wins_over_definition_default(self) -> None:
        from protoboard.gui.renderers.body_shapes import _resolve_two_pin_spec
        defn = ComponentDefinition(
            name="D", footprint=TwoPin(),
            pin_names={}, component_class=ComponentClass.DIODE,
            default_body_variant="DO-35/S",
        )
        inst = ComponentInstance(
            id=uuid.uuid4(), definition_ref="D", refdes="D1", value="",
            body_variant="DO-41/typ",
        )
        spec = _resolve_two_pin_spec(defn, inst, self._pkgs())
        assert spec is not None
        assert spec.package_id == "DO-41"

    def test_definition_default_used_when_instance_absent(self) -> None:
        from protoboard.gui.renderers.body_shapes import _resolve_two_pin_spec
        defn = ComponentDefinition(
            name="D", footprint=TwoPin(),
            pin_names={}, component_class=ComponentClass.DIODE,
            default_body_variant="DO-35/typ",
        )
        spec = _resolve_two_pin_spec(defn, None, self._pkgs())
        assert spec is not None
        assert spec.package_id == "DO-35"
        assert spec.variant_id == "typ"

    def test_no_default_no_instance_yields_none(self) -> None:
        """No class-based fallback — without a body_variant the
        renderer dispatches to GenericBody, not to a guessed family."""
        from protoboard.gui.renderers.body_shapes import _resolve_two_pin_spec
        defn = ComponentDefinition(
            name="D", footprint=TwoPin(),
            pin_names={}, component_class=ComponentClass.DIODE,
        )
        assert _resolve_two_pin_spec(defn, None, self._pkgs()) is None

    def test_invalid_definition_default_yields_none(self) -> None:
        """Misnamed variant in the library default is silently None;
        renderer falls back to GenericBody rather than guessing."""
        from protoboard.gui.renderers.body_shapes import _resolve_two_pin_spec
        defn = ComponentDefinition(
            name="D", footprint=TwoPin(),
            pin_names={}, component_class=ComponentClass.DIODE,
            default_body_variant="DO-35/nonexistent",
        )
        assert _resolve_two_pin_spec(defn, None, self._pkgs()) is None
