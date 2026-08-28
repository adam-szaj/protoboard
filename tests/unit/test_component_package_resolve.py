# SPDX-License-Identifier: MIT
"""Tests for resolve_component_package — variant-ref precedence."""

from __future__ import annotations

import pytest

from protoboard.domain.components.definition import ComponentDefinition, PackageOption
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.package import (
    PackageFamily,
    PackageVariant,
    resolve_component_package,
    resolve_package_variant_footprint,
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
            "dims": {"ly": 5.0, "lx": 5.0},
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
    package_ref = kw.pop("package_ref", None)
    if package_ref is not None and "footprints" not in kw:
        kw["footprints"] = (
            PackageOption(name=str(package_ref), ref=str(package_ref)),
        )
    if "package" in kw:
        return ComponentDefinition(name="X", pin_names={}, **kw)  # type: ignore[arg-type]
    return ComponentDefinition.from_footprint(
        name="X", footprint=SIL(n=2), pin_names={}, **kw,  # type: ignore[arg-type]
    )


class TestResolvePrecedence:
    def test_family_ref_resolves(self) -> None:
        d = _defn(package_ref="DO-35/S")
        pkg = resolve_component_package(d, None, _families())
        assert isinstance(pkg, Cylinder)
        assert pkg.length_mm == 3.0

    def test_bare_family_ref_uses_default(self) -> None:
        d = _defn(package_ref="DO-35/typ")
        pkg = resolve_component_package(d, None, _families())
        assert pkg.length_mm == 3.7  # type: ignore[union-attr]

    def test_instance_override_beats_family_ref(self) -> None:
        d = _defn(package_ref="DO-35/typ")
        inst = ComponentInstance.new(
            definition_ref="X", refdes="X1", value="", package_variant="DO-35/S",
        )
        pkg = resolve_component_package(d, inst, _families())
        assert pkg.length_mm == 3.0  # type: ignore[union-attr]

    def test_inline_package_is_last_rung(self) -> None:
        d = _defn(package=_inline_prism())
        assert isinstance(resolve_component_package(d, None, _families()), Prism)

    def test_family_ref_beats_inline(self) -> None:
        d = _defn(package=_inline_prism(), package_ref="DO-35/S")
        assert isinstance(resolve_component_package(d, None, _families()), Cylinder)

    def test_unresolved_definition_ref_raises(self) -> None:
        d = _defn(package=_inline_prism(), package_ref="NOPE/x")
        with pytest.raises(ValueError, match="NOPE/x"):
            resolve_component_package(d, None, _families())

    def test_unresolved_instance_override_raises(self) -> None:
        d = _defn(package_ref="DO-35/typ")
        inst = ComponentInstance.new(
            definition_ref="X", refdes="X1", value="", package_variant="NOPE/x",
        )
        with pytest.raises(ValueError, match="NOPE/x"):
            resolve_component_package(d, inst, _families())

    def test_nothing_resolves_to_definition_default(self) -> None:
        # ``package`` is required on the definition (the loader synthesizes a
        # generic one), so the last rung is always the definition default —
        # resolve never returns None for a real part.
        from protoboard.domain.components.package.kind import (
            is_generic_package,
        )
        pkg = resolve_component_package(_defn(), None, _families())
        assert pkg is not None and is_generic_package(pkg)


class TestFieldValidation:
    def test_bad_family_ref_shape_raises(self) -> None:
        with pytest.raises(ValueError):
            PackageOption(name="bad", ref="")

    def test_good_family_ref_ok(self) -> None:
        assert _defn(package_ref="a/b").default_package_ref() == "a/b"


class TestResolvedFootprint:
    """resolved_footprint gives the chosen variant's pin geometry, falling back
    to defn.default_footprint() when no package resolves."""

    def test_variant_footprint_is_resolved_package(self) -> None:
        from protoboard.domain.components.package import resolved_footprint

        d = _defn(package_ref="DO-35/S")
        inst = ComponentInstance.new(
            definition_ref="X", refdes="X1", value="",
        ).bind(d, _families())
        fp = resolved_footprint(d, inst, _families())
        assert isinstance(fp, Cylinder)

    def test_variant_differs_from_default_footprint(self) -> None:
        # DO-35/S vs DO-35/typ have different lengths → different pin spacing.
        from protoboard.domain.components.package import resolved_footprint

        d = _defn(package_ref="DO-35/typ")
        # Instance overrides to DO-35/S; both bound to the same catalogue.
        inst = ComponentInstance.new(
            definition_ref="X", refdes="X1", value="", package_variant="DO-35/S",
        ).bind(d, _families())
        # No instance-level override → resolves the defn default (DO-35/typ).
        inst_default = ComponentInstance.new(
            definition_ref="X", refdes="X2", value="",
        ).bind(d, _families())
        fp_inst = resolved_footprint(d, inst, _families())
        fp_default = resolved_footprint(d, inst_default, _families())
        assert fp_inst.pin_positions() != fp_default.pin_positions()

    def test_no_package_falls_back_to_defn_footprint(self) -> None:
        from protoboard.domain.components.package import resolved_footprint

        d = _defn()  # no package / family_ref
        # Bound instance whose part has no package: defn.default_footprint() is its real
        # default geometry (correct default, not a silent fallback).
        inst = ComponentInstance.new(
            definition_ref="X", refdes="X1", value="",
        ).bind(d, {})
        fp = resolved_footprint(d, inst, _families())
        assert fp is d.package  # the definition default, no variant

    def test_none_instance_raises(self) -> None:
        from protoboard.domain.components.package import resolved_footprint
        from protoboard.errors import UnboundInstanceError

        d = _defn(package_ref="DO-35/S")
        # No instance reaching a geometry path is the silent-wrong-body bug this
        # model removes: it must raise rather than fall back to defn.default_footprint().
        with pytest.raises(UnboundInstanceError):
            resolved_footprint(d, None, None)

    def test_preplacement_variant_footprint_uses_selected_variant(self) -> None:
        d = _defn(package_ref="DO-35/typ")
        chosen = resolve_package_variant_footprint(
            d,
            "DO-35/S",
            _families(),
        )
        default = d.default_footprint()
        assert chosen.pin_positions() != default.pin_positions()

    def test_preplacement_unresolved_variant_raises(self) -> None:
        d = _defn(package_ref="DO-35/typ")
        with pytest.raises(ValueError, match="NOPE/x"):
            resolve_package_variant_footprint(d, "NOPE/x", _families())


class TestResolvePinWorldPositionVariant:
    """resolve_pin_world_position honours the chosen variant when given the
    catalogue — the placed component's pin lands on the variant geometry, not
    the definition default. This is the renderer/session path that drove the
    'SMD pin sits 3.2 holes away instead of 1' bug."""

    def test_variant_pin_position_differs_with_families(self) -> None:
        import uuid

        from protoboard.domain.layout.placement import (
            ComponentPlacement,
            resolve_pin_world_position,
        )
        from protoboard.geometry import Point

        d = _defn(package_ref="DO-35/typ")
        # Bound WITH the catalogue → the instance resolves its DO-35/S variant.
        inst_with = ComponentInstance.new(
            definition_ref="X", refdes="X1", value="", package_variant="DO-35/S",
        ).bind(d, _families())
        # Bound WITHOUT a catalogue → the selected variant cannot resolve.
        inst_without = ComponentInstance.new(
            definition_ref="X", refdes="X1", value="", package_variant="DO-35/S",
        ).bind(d, {})
        cid = inst_with.id
        pl = ComponentPlacement(
            component_id=cid, position=Point(0.0, 0.0), rotation=0, side="top")

        without = resolve_pin_world_position(
            cid, 2, placements={cid: pl}, instances={cid: inst_without},
            components={"X": d})
        with_fam = resolve_pin_world_position(
            cid, 2, placements={cid: pl}, instances={cid: inst_with},
            components={"X": d})
        assert without is None
        assert with_fam is not None
