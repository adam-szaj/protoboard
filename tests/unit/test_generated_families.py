# SPDX-License-Identifier: MIT
"""Tests for generated in-line package families (dip-N / wdip-N / sil-N)."""

from __future__ import annotations

import pytest

from protoboard.domain.components.definition import (
    ComponentDefinition,
    MetaPin,
    PackageOption,
)
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.package import resolve_component_package
from protoboard.domain.components.package.family import PackageFamily
from protoboard.domain.components.package.generated_families import (
    dip_family,
    generate_inline_families,
    inline_family_ids_for_pins,
    offered_package_options,
    sil_family,
)


class TestDipFamily:
    def test_narrow_vs_wide_row_spacing(self) -> None:
        narrow = dip_family(16, wide=False)
        wide = dip_family(16, wide=True)
        assert narrow is not None and wide is not None
        n_pkg = narrow.variants[0].package
        w_pkg = wide.variants[0].package
        n_cols = sorted({round(p.x, 1) for p in n_pkg.pin_positions()})
        w_cols = sorted({round(p.x, 1) for p in w_pkg.pin_positions()})
        assert n_cols == [0.0, 3.0]  # 0.3" narrow
        assert w_cols == [0.0, 6.0]  # 0.6" wide
        # The wide body is physically wider.
        assert w_pkg.bbox().lx > n_pkg.bbox().lx

    def test_body_width_matches_measured_mm(self) -> None:
        # Moulded-body width is a linear fit of the row spacing R (in 2.54 mm
        # units), measured off real parts: width = 2.54·R − 1.35 mm.  So a
        # narrow 0.3" DIP (R=3) ≈ 6.27 mm and a wide 0.6" DIP (R=6) ≈ 13.89 mm.
        # bbox() is in holes (2.5 mm raster), so convert before comparing.
        raster = 2.5
        narrow = dip_family(16, wide=False)
        wide = dip_family(16, wide=True)
        assert narrow is not None and wide is not None
        n_mm = narrow.variants[0].package.bbox().lx * raster
        w_mm = wide.variants[0].package.bbox().lx * raster
        assert round(n_mm, 2) == round(2.54 * 3 - 1.35, 2)  # 6.27
        assert round(w_mm, 2) == round(2.54 * 6 - 1.35, 2)  # 13.89

    def test_pin_count_matches_name(self) -> None:
        fam = dip_family(28, wide=False)
        assert fam is not None
        assert fam.family_id == "dip-28"
        assert fam.variants[0].package.pin_count() == 28

    def test_body_sits_between_rows_not_over_pins(self) -> None:
        # Regression: the body must be narrower than the row spacing and centred
        # on the pin cloud, so the legs show on both sides and pins aren't
        # covered.  (Was: pin-cloud + uniform 0.5-hole margin → body overlapped
        # the columns.)
        for fam in (dip_family(8, wide=False), dip_family(16, wide=True)):
            assert fam is not None
            pkg = fam.variants[0].package
            cols = sorted({round(p.x, 3) for p in pkg.pin_positions()})
            bb = pkg.bbox()
            # Body strictly inside the two pin columns (across the rows)…
            assert bb.x > cols[0] + 1e-6
            assert bb.x + bb.lx < cols[-1] - 1e-6
            # …and centred on the pin cloud across the rows.
            assert round(bb.x + bb.lx / 2.0, 3) == round((cols[0] + cols[-1]) / 2.0, 3)

    def test_body_extends_past_end_pins_along_axis(self) -> None:
        # Along the pin axis the body runs a little past the first/last pin.
        fam = dip_family(8, wide=False)
        assert fam is not None
        pkg = fam.variants[0].package
        ys = sorted({round(p.y, 3) for p in pkg.pin_positions()})
        bb = pkg.bbox()
        assert bb.y < ys[0]  # past the first pin
        assert bb.y + bb.ly > ys[-1]  # past the last pin

    @pytest.mark.parametrize("pins", [0, 2, 3, 15, -4])
    def test_odd_or_too_small_returns_none(self, pins: int) -> None:
        assert dip_family(pins, wide=False) is None


class TestSilFamily:
    def test_sil_family_pin_count(self) -> None:
        fam = sil_family(8)
        assert fam is not None
        assert fam.family_id == "sil-8"
        assert fam.variants[0].package.pin_count() == 8

    @pytest.mark.parametrize("pins", [1, 0, -3])
    def test_too_small_returns_none(self, pins: int) -> None:
        assert sil_family(pins) is None


class TestGenerateInlineFamilies:
    def test_contains_dip_wdip_sil(self) -> None:
        fams = generate_inline_families()
        assert "dip-16" in fams
        assert "wdip-16" in fams
        assert "sil-6" in fams
        # No odd DIP.
        assert "dip-15" not in fams

    def test_every_family_is_valid(self) -> None:
        # PackageFamily.__post_init__ raises on empties / dup ids; building the
        # whole set without error is the assertion.
        fams = generate_inline_families()
        assert all(isinstance(f, PackageFamily) for f in fams.values())


class TestInlineFamilyIdsForPins:
    def test_dual_row_even(self) -> None:
        assert inline_family_ids_for_pins(16, dual_row=True) == ["dip-16", "wdip-16"]

    def test_dual_row_odd_empty(self) -> None:
        assert inline_family_ids_for_pins(15, dual_row=True) == []

    def test_single_row(self) -> None:
        assert inline_family_ids_for_pins(6, dual_row=False) == ["sil-6"]


class TestOfferedPackageOptions:
    @staticmethod
    def _definition(*options: PackageOption) -> ComponentDefinition:
        return ComponentDefinition(
            name="X",
            meta_pins=(MetaPin(("A",)), MetaPin(("B",))),
            package_options=options,
        )

    @staticmethod
    def _catalogue() -> dict[str, PackageFamily]:
        declared = sil_family(2)
        unrelated = sil_family(3)
        assert declared is not None and unrelated is not None
        return {
            "declared": PackageFamily(
                family_id="declared",
                variants=(
                    declared.variants[0],
                    type(declared.variants[0])(
                        variant_id="alternate",
                        label="Alternate",
                        package=declared.variants[0].package,
                    ),
                ),
            ),
            "unrelated": unrelated,
        }

    def test_unrelated_family_is_not_offered_even_with_matching_pin_count(
        self,
    ) -> None:
        option = PackageOption(
            name="Declared",
            ref="declared/std",
            pin_map={1: (1,), 2: (2,)},
        )

        offered = offered_package_options(self._definition(option), self._catalogue())

        assert [item.ref for item in offered] == ["declared/std"]

    def test_family_reference_expands_variants_and_copies_pin_map(self) -> None:
        pin_map = {1: (2,), 2: (1,)}
        option = PackageOption(name="Declared", ref="declared", pin_map=pin_map)

        offered = offered_package_options(self._definition(option), self._catalogue())

        assert [item.ref for item in offered] == [
            "declared/std",
            "declared/alternate",
        ]
        assert [item.pin_map for item in offered] == [pin_map, pin_map]

    def test_variant_reference_remains_one_leaf(self) -> None:
        option = PackageOption(
            name="Alternate",
            ref="declared/alternate",
            pin_map={1: (1,), 2: (2,)},
        )

        offered = offered_package_options(self._definition(option), self._catalogue())

        assert offered == [option]

    def test_unresolvable_declared_option_is_not_offered(self) -> None:
        option = PackageOption(name="Missing", ref="missing", pin_map={})

        assert (
            offered_package_options(self._definition(option), self._catalogue()) == []
        )


class TestEndToEndResolution:
    def test_picking_wdip_resolves_wide_package(self) -> None:
        fams = generate_inline_families()
        defn = ComponentDefinition(
            name="U1",
            meta_pins=tuple(MetaPin((str(index),)) for index in range(1, 17)),
            package_options=(
                PackageOption(
                    name="DIP-16",
                    ref="dip-16/std",
                    pin_map={index: (index,) for index in range(1, 17)},
                ),
                PackageOption(
                    name="WDIP-16",
                    ref="wdip-16/std",
                    pin_map={index: (index,) for index in range(1, 17)},
                ),
            ),
        )
        inst = ComponentInstance.new(
            definition_ref="U1",
            refdes="U1",
            value="",
            package_variant="wdip-16/std",
        )
        pkg = resolve_component_package(defn, inst, fams)
        assert pkg is not None
        # Wide body: columns 6 holes apart.
        cols = sorted({round(p.x, 1) for p in pkg.pin_positions()})
        assert cols == [0.0, 6.0]
