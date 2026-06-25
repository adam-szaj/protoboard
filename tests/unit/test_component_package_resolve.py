# SPDX-License-Identifier: MIT
"""Tests for resolve_component_package — variant-ref precedence."""

from __future__ import annotations

import pytest

from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.package import (
    PackageFamily,
    PackageVariant,
    resolve_component_package,
)
from protoboard.domain.components.package.loader import build_package
from protoboard.domain.components.package.shapes import Cylinder, Prism


def _cyl(diameter: float, length: float) -> object:
    return build_package(
        {
            "shape": "cylinder",
            "dims": {"diameter": diameter, "length": length},
            "bend": 0,
            "leads": {
                "direction": [[0, 0, -1], [0, 0, 1]],
                "body_offset": 0, "board_offset": "solve",
                "lead_r": 0.2, "lead_length": 5.0,
            },
        },
        "C",
    )


def _inline_prism() -> object:
    return build_package(
        {
            "shape": "prism",
            "dims": {"width": 5.0, "length": 5.0},
            "bend": 0,
            "leads": {
                "direction": [0, 0, -1],
                "body_offset": {"unit": 2.5, "value": [[0, 1], [2, 0], [0, -1]]},
                "board_offset": "default",
            },
        },
        "U",
    )


def _families() -> dict[str, PackageFamily]:
    fam = PackageFamily(
        family_id="DO-35",
        variants=(
            PackageVariant("S", "Small", _cyl(1.5, 3.0)),    # type: ignore[arg-type]
            PackageVariant("typ", "Typical", _cyl(1.8, 3.7)),  # type: ignore[arg-type]
        ),
        default_id="typ",
    )
    return {"DO-35": fam}


def _defn(**kw: object) -> ComponentDefinition:
    return ComponentDefinition(
        name="X", footprint=SIL(n=2), pin_names={}, **kw,  # type: ignore[arg-type]
    )


class TestResolvePrecedence:
    def test_family_ref_resolves(self) -> None:
        d = _defn(package_family_ref="DO-35/S")
        pkg = resolve_component_package(d, None, _families())
        assert isinstance(pkg, Cylinder)
        assert pkg.length_mm == 3.0

    def test_bare_family_ref_uses_default(self) -> None:
        d = _defn(package_family_ref="DO-35/typ")
        pkg = resolve_component_package(d, None, _families())
        assert pkg.length_mm == 3.7  # type: ignore[union-attr]

    def test_instance_override_beats_family_ref(self) -> None:
        d = _defn(package_family_ref="DO-35/typ")
        inst = ComponentInstance.new(
            definition_ref="X", refdes="X1", value="", package_variant="DO-35/S",
        )
        pkg = resolve_component_package(d, inst, _families())
        assert pkg.length_mm == 3.0  # type: ignore[union-attr]

    def test_inline_package_is_last_rung(self) -> None:
        d = _defn(package=_inline_prism())
        assert isinstance(resolve_component_package(d, None, _families()), Prism)

    def test_family_ref_beats_inline(self) -> None:
        d = _defn(package=_inline_prism(), package_family_ref="DO-35/S")
        assert isinstance(resolve_component_package(d, None, _families()), Cylinder)

    def test_stale_ref_falls_through_to_inline(self) -> None:
        d = _defn(package=_inline_prism(), package_family_ref="NOPE/x")
        assert isinstance(resolve_component_package(d, None, _families()), Prism)

    def test_stale_instance_override_falls_through(self) -> None:
        d = _defn(package_family_ref="DO-35/typ")
        inst = ComponentInstance.new(
            definition_ref="X", refdes="X1", value="", package_variant="NOPE/x",
        )
        # Instance ref unresolvable → falls to the definition default.
        assert resolve_component_package(d, inst, _families()).length_mm == 3.7  # type: ignore[union-attr]

    def test_nothing_resolves_to_none(self) -> None:
        assert resolve_component_package(_defn(), None, _families()) is None


class TestFieldValidation:
    def test_bad_family_ref_shape_raises(self) -> None:
        with pytest.raises(ValueError):
            _defn(package_family_ref="no-slash")

    def test_good_family_ref_ok(self) -> None:
        assert _defn(package_family_ref="a/b").package_family_ref == "a/b"
