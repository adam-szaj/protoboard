# SPDX-License-Identifier: MIT
"""Tests for PackageFamily — variant grouping + addressing."""

from __future__ import annotations

import pytest

from protoboard.domain.components.package import (
    PackageFamily,
    PackageVariant,
    resolve_variant,
)
from protoboard.domain.components.package.loader import build_package


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
        "V",
    )


def _family(default_id: str | None = None) -> PackageFamily:
    return PackageFamily(
        family_id="DO-35",
        label="DO-35 glass",
        variants=(
            PackageVariant("S", "Small", _cyl(1.5, 3.0)),    # type: ignore[arg-type]
            PackageVariant("typ", "Typical", _cyl(1.8, 3.7)),  # type: ignore[arg-type]
            PackageVariant("L", "Large", _cyl(2.0, 4.5)),    # type: ignore[arg-type]
        ),
        default_id=default_id,
    )


class TestPackageFamily:
    def test_default_is_first_without_default_id(self) -> None:
        assert _family().default_variant().variant_id == "S"

    def test_default_id_picks_named_variant(self) -> None:
        assert _family("typ").default_variant().variant_id == "typ"

    def test_get_variant(self) -> None:
        fam = _family()
        assert fam.get_variant("L").variant_id == "L"  # type: ignore[union-attr]
        assert fam.get_variant("nope") is None

    def test_address(self) -> None:
        assert _family().address("S") == "DO-35/S"

    def test_display_label_falls_back_to_id(self) -> None:
        fam = PackageFamily(
            family_id="X",
            variants=(PackageVariant("a", "A", _cyl(1, 2)),),  # type: ignore[arg-type]
        )
        assert fam.display_label == "X"

    def test_no_variants_raises(self) -> None:
        with pytest.raises(ValueError):
            PackageFamily(family_id="X", variants=())

    def test_duplicate_variant_ids_raise(self) -> None:
        with pytest.raises(ValueError):
            PackageFamily(
                family_id="X",
                variants=(
                    PackageVariant("a", "A", _cyl(1, 2)),  # type: ignore[arg-type]
                    PackageVariant("a", "B", _cyl(1, 2)),  # type: ignore[arg-type]
                ),
            )

    def test_unknown_default_id_raises(self) -> None:
        with pytest.raises(ValueError):
            _family("does-not-exist")


class TestResolveVariant:
    def test_full_address(self) -> None:
        fams = {"DO-35": _family("typ")}
        assert resolve_variant(fams, "DO-35/L").variant_id == "L"  # type: ignore[union-attr]

    def test_bare_family_resolves_to_default(self) -> None:
        fams = {"DO-35": _family("typ")}
        assert resolve_variant(fams, "DO-35").variant_id == "typ"  # type: ignore[union-attr]

    def test_unknown_family_is_none(self) -> None:
        assert resolve_variant({}, "DO-35/S") is None

    def test_unknown_variant_is_none(self) -> None:
        fams = {"DO-35": _family()}
        assert resolve_variant(fams, "DO-35/zzz") is None
