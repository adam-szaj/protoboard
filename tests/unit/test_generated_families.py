# SPDX-License-Identifier: MIT
"""Tests for generated in-line package families (dip-N / wdip-N / sil-N)."""

from __future__ import annotations

import pytest

from protoboard.domain.components.definition import (
    ComponentClass,
    ComponentDefinition,
)
from protoboard.domain.components.footprints import DIL, SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.package import resolve_component_package
from protoboard.domain.components.package.family import PackageFamily
from protoboard.domain.components.package.generated_families import (
    dip_family,
    generate_inline_families,
    inline_family_ids_for_pins,
    offered_body_families,
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
        assert n_cols == [0.0, 3.0]   # 0.3" narrow
        assert w_cols == [0.0, 6.0]   # 0.6" wide
        # The wide body is physically wider.
        assert w_pkg.bbox().width > n_pkg.bbox().width

    def test_body_width_matches_measured_mm(self) -> None:
        # Moulded-body width is a linear fit of the row spacing R (in 2.54 mm
        # units), measured off real parts: width = 2.54·R − 1.35 mm.  So a
        # narrow 0.3" DIP (R=3) ≈ 6.27 mm and a wide 0.6" DIP (R=6) ≈ 13.89 mm.
        # bbox() is in holes (2.5 mm raster), so convert before comparing.
        raster = 2.5
        narrow = dip_family(16, wide=False)
        wide = dip_family(16, wide=True)
        assert narrow is not None and wide is not None
        n_mm = narrow.variants[0].package.bbox().width * raster
        w_mm = wide.variants[0].package.bbox().width * raster
        assert round(n_mm, 2) == round(2.54 * 3 - 1.35, 2)   # 6.27
        assert round(w_mm, 2) == round(2.54 * 6 - 1.35, 2)   # 13.89

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
            assert bb.x + bb.width < cols[-1] - 1e-6
            # …and centred on the pin cloud across the rows.
            assert round(bb.x + bb.width / 2.0, 3) == round(
                (cols[0] + cols[-1]) / 2.0, 3
            )

    def test_body_extends_past_end_pins_along_axis(self) -> None:
        # Along the pin axis the body runs a little past the first/last pin.
        fam = dip_family(8, wide=False)
        assert fam is not None
        pkg = fam.variants[0].package
        ys = sorted({round(p.y, 3) for p in pkg.pin_positions()})
        bb = pkg.bbox()
        assert bb.y < ys[0]                 # past the first pin
        assert bb.y + bb.height > ys[-1]    # past the last pin

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


class TestOfferedBodyFamilies:
    """The offer is derived from the component's *explicit body* (its kind
    generator, or a plain two-pin package), never from the footprint type."""

    def _catalogue(self) -> dict[str, PackageFamily]:
        return dict(generate_inline_families())

    def _kind_defn(self, kind: str, pins: int) -> ComponentDefinition:
        from protoboard.domain.components.package.generators import (
            GeneratorContext,
        )
        from protoboard.domain.components.package.kind import (
            build_kind_package,
        )
        pkg = build_kind_package(kind, GeneratorContext(pin_count=pins)).package
        assert pkg is not None
        fp = DIL(n=pins) if kind in ("dip_ic", "wdip_ic") else SIL(n=pins)
        return ComponentDefinition(
            name="X", pin_names={},
            component_class=ComponentClass.IC,
            package=pkg,
        )

    def test_dip_body_offers_both_widths_of_matching_count(self) -> None:
        offered = offered_body_families(
            self._kind_defn("dip_ic", 16), self._catalogue(),
        )
        assert [f.family_id for f in offered] == ["dip-16", "wdip-16"]

    def test_wide_dip_body_offers_the_same_pair(self) -> None:
        offered = offered_body_families(
            self._kind_defn("wdip_ic", 28), self._catalogue(),
        )
        assert [f.family_id for f in offered] == ["dip-28", "wdip-28"]

    def test_sil_body_offers_matching_sil(self) -> None:
        offered = offered_body_families(
            self._kind_defn("sil", 6), self._catalogue(),
        )
        assert [f.family_id for f in offered] == ["sil-6"]

    def test_bare_multi_pin_part_offers_nothing(self) -> None:
        # No declared body and not a two-pin part → no offer (a SIL-3 pin
        # cloud fits a TO-92, a header or a trimmer — the footprint cannot
        # decide).
        defn = ComponentDefinition.from_footprint(
            name="HDR3", footprint=SIL(n=3), pin_names={},
            component_class=ComponentClass.CONNECTOR,
        )
        assert offered_body_families(defn, self._catalogue()) == []

    def _two_pin_catalogue(self) -> dict[str, PackageFamily]:
        # The generated in-line set plus one two-pin family (the generated
        # catalogue starts SIL at 3 — two-pin bodies live in the authored
        # catalogue in production).
        from protoboard.domain.components.package.generated_families import (
            sil_family,
        )
        catalogue = self._catalogue()
        two_pin = sil_family(2)
        assert two_pin is not None
        catalogue[two_pin.family_id] = two_pin
        return catalogue

    def test_bare_two_pin_part_offers_all_two_pin_families(self) -> None:
        # The resistor/diode body swap: a two-pin part with no declared body
        # is offered every two-pin family in the catalogue.
        defn = ComponentDefinition.from_footprint(
            name="R", footprint=SIL(n=2), pin_names={},
            component_class=ComponentClass.RESISTOR,
        )
        offered = offered_body_families(defn, self._two_pin_catalogue())
        assert [f.family_id for f in offered] == ["sil-2"]

    def test_synthetic_generic_body_is_not_a_declared_body(self) -> None:
        # The loader's `_generic` fallback must not be treated as a declared
        # body (nor as a parameterisable kind) — the two-pin offer still runs.
        from protoboard.domain.components.package.kind import (
            synth_generic_package,
        )
        defn = ComponentDefinition(
            name="R", pin_names={},
            component_class=ComponentClass.RESISTOR,
            package=synth_generic_package(SIL(n=2)),
        )
        offered = offered_body_families(defn, self._two_pin_catalogue())
        assert [f.family_id for f in offered] == ["sil-2"]


class TestEndToEndResolution:
    def test_picking_wdip_resolves_wide_package(self) -> None:
        fams = generate_inline_families()
        defn = ComponentDefinition.from_footprint(
            name="U1", component_class=ComponentClass.IC,
            footprint=DIL(n=16), pin_names={},
        )
        inst = ComponentInstance.new(
            definition_ref="U1", refdes="U1", value="",
            package_variant="wdip-16/std",
        )
        pkg = resolve_component_package(defn, inst, fams)
        assert pkg is not None
        # Wide body: columns 6 holes apart.
        cols = sorted({round(p.x, 1) for p in pkg.pin_positions()})
        assert cols == [0.0, 6.0]
