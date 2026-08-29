# SPDX-License-Identifier: MIT
"""Tests for the component library loader.

The loader reads YAML files from one or more directories and provides
lookup of `ComponentDefinition` by name. When the same name appears in
multiple directories, the *first* directory wins (user overrides
built-in definitions).
"""

from pathlib import Path

import pytest

from protoboard.domain.components.definition import (
    ComponentClass,
    ComponentDefinition,
)
from protoboard.errors import SerializationError
from protoboard.services.library.loader import (
    ComponentLibrary,
    _component_from_dict,
    load_library,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "library"


class TestMetaPinMapping:
    @staticmethod
    def _raw(pin_map: dict[int, list[int]]) -> dict[str, object]:
        return {
            "name": "X",
            "pins": {"count": 3},
            "package": {"kind": "sil"},
            "meta_pins": [["A", "alternate"], "B"],
            "pin_map": pin_map,
        }

    def test_normalizes_labels_and_one_to_many_mapping(self) -> None:
        families: dict = {}

        definition = _component_from_dict(
            self._raw({1: [1, 3], 2: [2]}),
            Path("metapins.yaml"),
            families,
            families,
        )

        assert [meta_pin.labels for meta_pin in definition.meta_pins] == [
            ("A", "alternate"),
            ("B",),
        ]
        assert definition.package_options[0].pin_map == {
            1: (1, 3),
            2: (2,),
        }

    @pytest.mark.parametrize(
        "pin_map",
        [
            {1: [1], 3: [2]},
            {1: [1, 2], 2: [2]},
            {1: [4]},
            {0: [1]},
        ],
    )
    def test_rejects_invalid_mapping(
        self, pin_map: dict[int, list[int]]
    ) -> None:
        families: dict = {}

        with pytest.raises(SerializationError):
            _component_from_dict(
                self._raw(pin_map),
                Path("metapins.yaml"),
                families,
                families,
            )


class TestDevboardInlinePackage:
    """A dev-board module is a component with an inline prism package whose pins
    are declared as aggregated (run-length) consecutive offsets — no footprint
    block.  Pin positions come from the package."""

    _LIB = """\
components:
  - name: MOD_2x4
    component_class: devboard
    package:
      shape: prism
      dims: {ly: 12.5, lx: 12.5, lz: 1.6}
      leads:
        direction: [0, 0, -1]
        body_offset: {unit: 2.5, value: [[3, [0, 1]], [5, 0], [3, [0, -1]]]}
        board_offset: default
      bend: 0
    pin_names: {1: A, 8: B}
"""

    def test_loads_as_devboard_with_package_pins(self, tmp_path: Path) -> None:
        (tmp_path / "m.yaml").write_text(self._LIB)
        defn = load_library([tmp_path]).get("MOD_2x4")
        assert defn.component_class is ComponentClass.DEVBOARD
        pins = defn.default_footprint().pin_positions()
        # 8 pins, cumulative from pin 1 = (0,0): down col A, jump 5, down… up col B.
        assert len(pins) == 8
        coords = [(round(p.x), round(p.y)) for p in pins]
        assert coords == [
            (0, 0),
            (0, 1),
            (0, 2),
            (0, 3),
            (5, 3),
            (5, 2),
            (5, 1),
            (5, 0),
        ]


class TestFootprintsList:
    """A component offers a ``footprints:`` list (ref or inline) instead of a
    single fixed footprint; the default/first entry derives ``defn.default_footprint()``.
    """

    def test_footprints_only_derives_default_geometry(self, tmp_path: Path) -> None:
        # No footprint:/package: block — geometry comes from the first entry.
        (tmp_path / "ic.yaml").write_text(
            "components:\n"
            "  - name: U_DIP14\n"
            "    component_class: ic\n"
            "    symbol: U\n"
            "    pins: {count: 14}\n"
            "    footprints:\n"
            "      - name: DIP-14\n"
            "        package: {kind: dip_ic}\n"
            "      - name: WDIP-14\n"
            "        package: {kind: wdip_ic}\n"
            "    pin_names: {7: GND, 14: VCC}\n"
        )
        defn = load_library([tmp_path]).get("U_DIP14")
        assert defn.default_footprint().pin_count() == 14  # from the first entry
        assert len(defn.package_options) == 2
        assert defn.package_options[0].name == "DIP-14"
        assert defn.package_options[1].package is not None  # inline package built

    def test_footprint_style_default_and_per_entry_override(
        self, tmp_path: Path
    ) -> None:
        from protoboard.domain.components.appearance import Appearance

        (tmp_path / "r.yaml").write_text(
            "components:\n"
            "  - name: R\n"
            "    component_class: resistor\n"
            "    symbol: R\n"
            "    pins: {count: 2}\n"
            "    footprint_style: 'color: #228822'\n"
            "    footprints:\n"
            "      - name: A\n"
            "        package: {kind: sil}\n"
            "        pins: 2\n"
            "      - name: B\n"
            "        package: {kind: sil}\n"
            "        pins: 2\n"
            "        style: 'color: #333333'\n"
            "    pin_names: {}\n"
        )
        defn = load_library([tmp_path]).get("R")
        # entry A inherits the list default; entry B overrides the colour.
        assert defn.package_options[0].style == Appearance(color="#228822")
        assert defn.package_options[1].style == Appearance(color="#333333")

    def test_ref_entry_resolves_against_catalogue(self, tmp_path: Path) -> None:
        from protoboard.domain.components.package.generated_families import (
            generate_inline_families,
        )

        (tmp_path / "ic.yaml").write_text(
            "components:\n"
            "  - name: U_DIP16\n"
            "    component_class: ic\n    symbol: U\n"
            "    footprints:\n"
            "      - ref: dip-16/std\n"
            "    pin_names: {}\n"
        )
        lib = load_library([tmp_path], package_families=generate_inline_families())
        defn = lib.get("U_DIP16")
        assert defn.default_footprint().pin_count() == 16
        assert defn.package_options[0].ref == "dip-16/std"
        assert defn.package_options[0].name == "dip-16/std"  # name defaults to ref

    def test_inline_option_resolves_via_synthesized_family(
        self, tmp_path: Path
    ) -> None:
        # An inline footprint option is registered as an @defn/name family so an
        # instance can select it via the normal resolve_component_package path.
        from protoboard.domain.components.instance import ComponentInstance
        from protoboard.domain.components.package import (
            resolve_component_package,
        )
        from protoboard.services.library.loader import (
            inline_option_address,
            inline_option_families,
        )

        (tmp_path / "ic.yaml").write_text(
            "components:\n"
            "  - name: U_DIP14\n"
            "    component_class: ic\n    symbol: U\n"
            "    pins: {count: 14}\n"
            "    footprints:\n"
            "      - name: DIP-14\n        package: {kind: dip_ic}\n"
            "    pin_names: {}\n"
        )
        lib = load_library([tmp_path])
        defn = lib.get("U_DIP14")
        families = inline_option_families(lib)
        addr = inline_option_address("U_DIP14", "DIP-14")
        assert addr in {f.address("std") for f in families.values()}
        inst = ComponentInstance.new(
            definition_ref="U_DIP14",
            refdes="U1",
            value="",
            package_variant=addr,
        )
        pkg = resolve_component_package(defn, inst, families)
        assert pkg is not None and pkg.pin_count() == 14
        # The address must resolve to the option family's OWN package —
        # not fall through to defn.package (the resolve_variant last-slash
        # split; the first-slash split failed every @defn/option/variant
        # address and this assertion passed vacuously via the fallback).
        family_id = addr.rsplit("/", 1)[0]
        assert pkg is families[family_id].variants[0].package


class TestLibraryFilePackagesAndFamilies:
    """Library files may carry ``packages:`` (reusable generics for ``extends``)
    and ``families:`` (catalogue entries for ``ref:``) alongside ``components:``
    — same file or same directory. Builtins win on a name clash."""

    _PRISM_BODY = (
        "    dims: {ly: 12.5, lx: 12.5, lz: 1.6}\n"
        "    leads:\n"
        "      direction: [0, 0, -1]\n"
        "      body_offset: {unit: 2.5, value: [[3, [0, 1]], [5, 0], [3, [0, -1]]]}\n"
        "      board_offset: default\n"
        "    bend: 0\n"
    )

    def test_component_extends_package_from_same_file(self, tmp_path: Path) -> None:
        (tmp_path / "mod.yaml").write_text(
            "packages:\n"
            "  - name: my_module_base\n"
            "    extends: prism\n" + self._PRISM_BODY + "components:\n"
            "  - name: MOD_A\n"
            "    component_class: devboard\n"
            "    package: {extends: my_module_base}\n"
            "    pin_names: {1: A}\n"
        )
        defn = load_library([tmp_path], package_families={}).get("MOD_A")
        assert defn.default_footprint().pin_count() == 8  # body pins from the generic

    def test_ref_resolves_family_from_same_file(self, tmp_path: Path) -> None:
        (tmp_path / "d.yaml").write_text(
            "families:\n"
            "  - family: MY-DO\n"
            "    extends: cylinder_axial\n"
            "    label: My axial body\n"
            "    color: '#112233'\n"
            "    variants:\n"
            "      - id: std\n"
            "        dims: {diameter: 1.8, length: 3.7}\n"
            "        leads: {lead_r: 0.2, lead_length: 5.6}\n"
            "components:\n"
            "  - name: D_MY\n"
            "    component_class: diode\n"
            "    symbol: D\n"
            "    footprints:\n"
            "      - ref: MY-DO/std\n"
            "    pin_names: {1: A, 2: K}\n"
        )
        lib = load_library([tmp_path], package_families={})
        defn = lib.get("D_MY")
        assert defn.default_footprint().pin_count() == 2
        assert defn.package_options[0].ref == "MY-DO/std"
        # The family rides on the library so the session's picker offers it.
        assert "MY-DO" in lib.package_families
        assert lib.package_families["MY-DO"].variants[0].variant_id == "std"

    def test_family_extends_package_across_files(self, tmp_path: Path) -> None:
        # The family file sorts BEFORE the packages file — order-independent,
        # like cross-file component extends (two-pass load).
        (tmp_path / "aa_parts.yaml").write_text(
            "families:\n"
            "  - family: MY-MOD\n"
            "    extends: my_module_base\n"
            "    variants:\n"
            "      - id: std\n"
            "components:\n"
            "  - name: MOD_B\n"
            "    component_class: devboard\n"
            "    footprints:\n"
            "      - ref: MY-MOD/std\n"
            "    pin_names: {}\n"
        )
        (tmp_path / "zz_bases.yaml").write_text(
            "packages:\n"
            "  - name: my_module_base\n"
            "    extends: prism\n" + self._PRISM_BODY
        )
        lib = load_library([tmp_path], package_families={})
        assert lib.get("MOD_B").default_footprint().pin_count() == 8
        assert "MY-MOD" in lib.package_families

    def test_builtin_family_wins_on_id_clash(self, tmp_path: Path) -> None:
        from protoboard.domain.components.package.generated_families import (
            generate_inline_families,
        )

        (tmp_path / "clash.yaml").write_text(
            "families:\n"
            "  - family: dip-16\n"  # clashes with generated
            "    extends: prism\n" + self._PRISM_BODY + "    variants:\n"
            "      - id: std\n"
            "components:\n"
            "  - name: U_CLASH\n"
            "    component_class: ic\n    symbol: U\n"
            "    footprints:\n"
            "      - ref: dip-16/std\n"
            "    pin_names: {}\n"
        )
        lib = load_library([tmp_path], package_families=generate_inline_families())
        # The builtin dip-16 (16 pins) won — not the 8-pin library imposter.
        assert lib.get("U_CLASH").default_footprint().pin_count() == 16
        assert "dip-16" not in lib.package_families

    def test_unknown_top_level_section_warns(self, tmp_path: Path) -> None:
        from structlog.testing import capture_logs

        (tmp_path / "typo.yaml").write_text(
            "familes:\n"  # typo for 'families'
            "  - family: LOST\n"
            "components:\n"
            "  - name: R_OK\n"
            "    component_class: resistor\n    symbol: R\n"
            "    pins: {count: 2}\n    package: {kind: sil}\n"
            "    pin_names: {}\n"
        )
        with capture_logs() as logs:
            lib = load_library([tmp_path], package_families={})
        assert "R_OK" in lib
        assert any(
            e["event"] == "library.unknown_section" and e.get("section") == "familes"
            for e in logs
        )


class TestOpenComponentClass:
    """``component_class`` is an OPEN tag: known values normalize to the
    ComponentClass enum, unknown ones load as-is with a warning — one new
    class in a library must never sink the whole load."""

    def test_unknown_class_loads_with_warning(self, tmp_path: Path) -> None:
        from structlog.testing import capture_logs

        (tmp_path / "k.yaml").write_text(
            "components:\n"
            "  - name: RELAY1\n"
            "    component_class: relay\n"  # not in ComponentClass
            "    symbol: K\n"
            "    pins: {count: 5}\n    package: {kind: sil}\n"
            "    pin_names: {}\n"
        )
        with capture_logs() as logs:
            lib = load_library([tmp_path], package_families={})
        defn = lib.get("RELAY1")
        assert defn.component_class == "relay"  # author's label survives
        assert defn.refdes_prefix == "K"  # symbol still wins
        assert any(
            e["event"] == "library.unknown_component_class"
            and e.get("component_class") == "relay"
            for e in logs
        )

    def test_unknown_class_without_symbol_gets_x_prefix(self, tmp_path: Path) -> None:
        (tmp_path / "k.yaml").write_text(
            "components:\n"
            "  - name: THING\n"
            "    component_class: frobnicator\n"
            "    pins: {count: 2}\n    package: {kind: sil}\n"
            "    pin_names: {}\n"
        )
        defn = load_library([tmp_path], package_families={}).get("THING")
        assert defn.refdes_prefix == "X"  # same neutral as OTHER

    def test_known_class_normalizes_to_enum_member(self, tmp_path: Path) -> None:
        (tmp_path / "r.yaml").write_text(
            "components:\n"
            "  - name: R1\n"
            "    component_class: resistor\n"
            "    pins: {count: 2}\n    package: {kind: sil}\n"
            "    pin_names: {}\n"
        )
        defn = load_library([tmp_path], package_families={}).get("R1")
        assert defn.component_class is ComponentClass.RESISTOR

    def test_empty_class_still_raises(self, tmp_path: Path) -> None:
        (tmp_path / "e.yaml").write_text(
            "components:\n"
            "  - name: E1\n"
            "    component_class: ''\n"
            "    pins: {count: 2}\n    package: {kind: sil}\n"
            "    pin_names: {}\n"
        )
        with pytest.raises(SerializationError):
            load_library([tmp_path], package_families={})


class TestCrossFileExtends:
    """`extends:` now resolves against a global index, so a base in one file can
    be extended from another (two-pass load)."""

    def test_child_extends_base_in_another_file(self, tmp_path: Path) -> None:
        (tmp_path / "_bases.yaml").write_text(
            "components:\n"
            "  - name: _DIP14_BASE\n"
            "    component_class: ic\n"
            "    symbol: U\n"
            "    pins: {count: 14}\n    package: {kind: dip_ic}\n"
            "    pin_names: {7: GND, 14: VCC}\n"
        )
        (tmp_path / "parts.yaml").write_text(
            "components:\n"
            "  - name: 74HC00\n"
            "    extends: _DIP14_BASE\n"
            "    pin_names: {1: A1, 2: B1, 3: Y1}\n"
        )
        lib = load_library([tmp_path])
        defn = lib.get("74HC00")
        assert defn.default_footprint().pin_count() == 14
        assert defn.refdes_prefix == "U"  # symbol inherited cross-file
        assert defn.pin_name(1) == "A1"  # child override
        assert defn.pin_name(7) == "GND"  # base pin_names deep-merged
        assert "_DIP14_BASE" not in lib  # templates not placeable

    def test_unknown_extends_still_raises(self, tmp_path: Path) -> None:
        (tmp_path / "p.yaml").write_text(
            "components:\n"
            "  - name: Orphan\n    extends: _NOPE\n"
            "    pins: {count: 2}\n    package: {kind: sil}\n    pin_names: {}\n"
        )
        with pytest.raises(SerializationError):
            load_library([tmp_path])


class TestKindPackage:
    """A ``kind:`` package field names a parameterized body; its geometry is the
    same prism the existing generator builds for the matching footprint."""

    def test_build_kind_dip_ic_matches_dual_row_prism(self) -> None:
        from protoboard.domain.components.package.kind import (
            GeneratorContext,
            build_kind_package,
        )

        # dip_ic + 14 pins == a narrow DIP-14 prism: two rows of 7 pins, 3 holes
        # apart, with the declared kind stamped on so the renderer can draw the
        # notch.
        ctx = build_kind_package("dip_ic", GeneratorContext(pin_count=14))
        built = ctx.package
        assert built is not None
        assert built.kind == "dip_ic"  # type: ignore[attr-defined]
        assert built.pin_count() == 14
        pins = built.pin_positions()
        xs = sorted({round(p.x, 1) for p in pins})
        assert xs == [0.0, 3.0]  # two rows 3 holes apart

    def test_kind_pin_count_from_component_pins(self, tmp_path: Path) -> None:
        # The component declares its pin count; the kind package builds from it.
        (tmp_path / "ic.yaml").write_text(
            "components:\n"
            "  - name: U_DIP14\n"
            "    component_class: ic\n"
            "    symbol: U\n"
            "    pins: {count: 14}\n"
            "    package: {kind: dip_ic}\n"
            "    pin_names: {7: GND, 14: VCC}\n"
        )
        defn = load_library([tmp_path]).get("U_DIP14")
        assert defn.default_footprint().pin_count() == 14
        assert defn.refdes_prefix == "U"

    def test_odd_pin_count_for_dip_is_rejected(self) -> None:
        from protoboard.domain.components.package.kind import (
            GeneratorContext,
            build_kind_package,
        )
        from protoboard.domain.components.package import PackageDefError

        with pytest.raises(PackageDefError):
            build_kind_package("dip_ic", GeneratorContext(pin_count=13))

    def test_unknown_kind_rejected(self) -> None:
        from protoboard.domain.components.package.kind import (
            GeneratorContext,
            build_kind_package,
        )
        from protoboard.domain.components.package import PackageDefError

        with pytest.raises(PackageDefError):
            build_kind_package("nope", GeneratorContext(pin_count=14))


class TestComponentStyle:
    """A component may carry a visual-only ``style:`` override string."""

    def test_parses_color_and_corner_radius(self, tmp_path: Path) -> None:
        from protoboard.domain.components.appearance import Appearance

        (tmp_path / "c.yaml").write_text(
            "components:\n"
            "  - name: R_BLACK\n"
            "    component_class: resistor\n"
            "    pins: {count: 2}\n    package: {kind: sil}\n"
            "    style: 'color: #1a1a1a; corner-radius: 0.4'\n"
            "    pin_names: {}\n"
        )
        defn = load_library([tmp_path]).get("R_BLACK")
        assert defn.appearance == Appearance(color="#1a1a1a", corner_radius_mm=(0.4,))

    def test_absent_style_is_none(self, tmp_path: Path) -> None:
        (tmp_path / "c.yaml").write_text(
            "components:\n"
            "  - name: R_PLAIN\n    component_class: resistor\n"
            "    pins: {count: 2}\n    package: {kind: sil}\n    pin_names: {}\n"
        )
        assert load_library([tmp_path]).get("R_PLAIN").appearance is None

    def test_short_hex_accepted(self, tmp_path: Path) -> None:
        from protoboard.domain.components.appearance import Appearance

        (tmp_path / "c.yaml").write_text(
            "components:\n"
            "  - name: R3\n    component_class: resistor\n"
            "    pins: {count: 2}\n    package: {kind: sil}\n    style: 'color: #abc'\n"
            "    pin_names: {}\n"
        )
        assert load_library([tmp_path]).get("R3").appearance == Appearance(color="#abc")

    def test_multi_value_corner_radius(self, tmp_path: Path) -> None:
        from protoboard.domain.components.appearance import Appearance

        (tmp_path / "c.yaml").write_text(
            "components:\n"
            "  - name: R4\n    component_class: resistor\n"
            "    pins: {count: 2}\n    package: {kind: sil}\n"
            "    style: 'corner-radius: 2 2 0 0'\n"
            "    pin_names: {}\n"
        )
        assert load_library([tmp_path]).get("R4").appearance == Appearance(
            corner_radius_mm=(2.0, 2.0, 0.0, 0.0)
        )

    def test_bad_hex_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "c.yaml").write_text(
            "components:\n"
            "  - name: RBAD\n    component_class: resistor\n"
            "    pins: {count: 2}\n    package: {kind: sil}\n    style: 'color: red'\n"
            "    pin_names: {}\n"
        )
        with pytest.raises(SerializationError):
            load_library([tmp_path])

    def test_negative_radius_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "c.yaml").write_text(
            "components:\n"
            "  - name: RNEG\n    component_class: resistor\n"
            "    pins: {count: 2}\n    package: {kind: sil}\n"
            "    style: 'corner-radius: -1'\n"
            "    pin_names: {}\n"
        )
        with pytest.raises(SerializationError):
            load_library([tmp_path])

    def test_unknown_property_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "c.yaml").write_text(
            "components:\n"
            "  - name: RUNK\n    component_class: resistor\n"
            "    pins: {count: 2}\n    package: {kind: sil}\n    style: 'fill: #abc'\n"
            "    pin_names: {}\n"
        )
        with pytest.raises(SerializationError):
            load_library([tmp_path])

    def test_legacy_appearance_block_is_ignored(self, tmp_path: Path) -> None:
        # The old ``appearance:`` mapping is no longer read; only ``style:`` is.
        # A leftover block yields no override (silently ignored, not an error).
        (tmp_path / "c.yaml").write_text(
            "components:\n"
            "  - name: ROLD\n    component_class: resistor\n"
            "    pins: {count: 2}\n    package: {kind: sil}\n"
            "    appearance: {color: '#1a1a1a'}\n    pin_names: {}\n"
        )
        assert load_library([tmp_path]).get("ROLD").appearance is None


class TestConsolidatedPassives:
    """The shipped passives are consolidated to generic R / C / C_POL, each
    offering its bodies as a footprints: list. These assert the consolidation
    preserves the default geometry and identity (no silent regression)."""

    def _lib(self) -> "ComponentLibrary":
        from protoboard.paths import BUILTIN_LIBRARY_DIR
        from protoboard.services.library.package_family_loader import (
            load_full_package_catalogue,
        )

        return load_library([BUILTIN_LIBRARY_DIR], load_full_package_catalogue())

    def test_generic_R_default_geometry_and_offers(self) -> None:
        defn = self._lib().get("R")
        assert defn.refdes_prefix == "R"
        # Default body = the first offered footprint (DIN-0207 axial) now that the
        # dead ``footprint: TwoPin`` default was dropped — the neutral generic
        # default is gone, the picker's first real body wins.
        pins = [
            (round(p.x), round(p.y)) for p in defn.default_footprint().pin_positions()
        ]
        assert pins == [(0, 0), (3, 0)]
        names = [o.name for o in defn.package_options]
        assert "DIN-0207" in names and "SMD-0805" in names
        assert defn.default_value == "?"

    def test_generic_C_and_C_POL(self) -> None:
        lib = self._lib()
        c = lib.get("C")
        cp = lib.get("C_POL")
        assert c.refdes_prefix == "C" and cp.refdes_prefix == "C"
        assert cp.pin_name(1) == "+" and cp.pin_name(2) == "-"  # polarity kept
        assert "Disc-D5" in [o.name for o in c.package_options]
        assert "Radial-D5" in [o.name for o in cp.package_options]

    def test_consolidated_names_dropped_aliases_kept(self) -> None:
        lib = self._lib()
        # The per-size component names are gone…
        for gone in ("R_SMD_0805", "R_SMD_0603", "C_SMD_0805", "C_Disc"):
            assert gone not in lib


class TestMigratedICs:
    """ICs are migrated off footprint:{type:DIP} onto pins+kind:dip_ic; shared
    opamp pinouts stay as templates, the rest carry per-component pin_names.
    These guard that the migration kept every IC's geometry and pin labels."""

    def _lib(self) -> "ComponentLibrary":
        from protoboard.paths import BUILTIN_LIBRARY_DIR
        from protoboard.services.library.package_family_loader import (
            load_full_package_catalogue,
        )

        return load_library([BUILTIN_LIBRARY_DIR], load_full_package_catalogue())

    def test_override_children_keep_pinout(self) -> None:
        lib = self._lib()
        for name, pc, pin, label in (
            ("74HC00", 14, 7, "GND"),
            ("74HC00", 14, 14, "VCC"),
            ("CD4001", 14, 7, "VSS"),
            ("74HC245", 20, 10, "GND"),
        ):
            d = lib.get(name)
            assert d.default_footprint().pin_count() == pc
            assert d.pin_name(pin) == label

    def test_kept_opamp_templates_still_feed_children(self) -> None:
        # Inherit-only children resolve their pinout through the surviving
        # _DUAL_OPAMP_* / _QUAD_OPAMP templates (a genuine shared pinout).
        lib = self._lib()
        assert lib.get("LM358").pin_name(4) == "GND"  # _DUAL_OPAMP_SS
        assert lib.get("TL072").pin_name(4) == "VEE"  # _DUAL_OPAMP_DS
        assert lib.get("LM324").pin_name(11) == "GND"  # _QUAD_OPAMP
        # Templates remain non-placeable (_-prefixed).
        assert "_DUAL_OPAMP_SS" not in lib

    def test_dropped_741_template_pinout_inlined(self) -> None:
        lib = self._lib()
        assert "_SINGLE_OPAMP_741" not in lib
        for name in ("uA741", "TL071", "LM741"):
            d = lib.get(name)
            assert d.default_footprint().pin_count() == 8
            assert d.pin_name(4) == "VEE" and d.pin_name(8) == "NC"

    def test_dip_geometry_and_notch_via_kind(self) -> None:
        from typing import cast

        from protoboard.domain.components.package.shapes import Prism
        from protoboard.gui.renderers.packages import DipIcRenderer, renderer_for
        from protoboard.gui.renderers.prism_silhouette import DipSilhouette

        lib = self._lib()
        d = lib.get("74HC00")
        assert d.package is not None
        prism = cast(Prism, d.package)
        assert prism.kind == "dip_ic"
        # The declared kind dispatches the DIP-IC renderer (notched body).
        assert renderer_for(prism) is DipIcRenderer
        # White-box: _silhouette lives on PrismRenderer (DipIcRenderer's base);
        # the @register_renderer TypeVar widens the decorated class back to
        # type[PackageRenderer] for pyright, hiding the subtype method.
        assert isinstance(
            DipIcRenderer(prism)._silhouette(
                prism
            ),  # pyright: ignore[reportAttributeAccessIssue]
            DipSilhouette,
        )
        # DIP-28 parts are wide.
        wide = lib.get("ATmega328P").package
        assert wide is not None
        assert cast(Prism, wide).kind == "wdip_ic"

    def test_regulators_untouched(self) -> None:
        # TO92/TO220 regulators keep their footprint (not dip_ic) and pinouts.
        lib = self._lib()
        assert lib.get("7805").pin_name(1) == "IN"
        assert lib.get("LM317").pin_name(1) == "ADJ"
        assert lib.get("7805").default_footprint().pin_count() == 3


class TestRigidPackageOption:
    """A rigid package reference (SMD) drives the footprint and drops bend;
    a bendable axial ref leaves the footprint and bend intact."""

    _LIB = """\
components:
  - name: R_SMD
    component_class: resistor
    pins: {count: 2}
    pin_names: {}
    footprints:
      - {name: SMD-0805, ref: "SMD-0805/typ"}
  - name: R_AXIAL
    component_class: resistor
    pins: {count: 2}
    pin_names: {}
    footprints:
      - {name: DIN-0207, ref: "DIN-0207/typ"}
"""

    def _lib(self, tmp_path: Path) -> ComponentLibrary:
        from protoboard.services.library.package_family_loader import (
            load_full_package_catalogue,
        )

        (tmp_path / "p.yaml").write_text(self._LIB)
        return load_library([tmp_path], load_full_package_catalogue())

    def test_rigid_smd_ref_overrides_footprint_and_clears_bend(
        self,
        tmp_path: Path,
    ) -> None:
        from protoboard.domain.components.package import Package

        defn = self._lib(tmp_path).get("R_SMD")
        # The SMD body itself implements the footprint API: rigid 1-hole pin
        # spacing and no bendable leads.
        assert isinstance(defn.default_footprint(), Package)
        xs = sorted({round(p.x, 1) for p in defn.default_footprint().pin_positions()})
        assert xs == [0.0, 1.0]
        from protoboard.domain.components.package import effective_bend_specs

        assert set(effective_bend_specs(defn)) == {2}

    def test_bendable_axial_ref_keeps_footprint_and_bend(
        self,
        tmp_path: Path,
    ) -> None:
        defn = self._lib(tmp_path).get("R_AXIAL")
        # An axial cylinder is not rigid (it bends): the referenced variant IS
        # the default geometry now, and the bendable leads survive.
        from protoboard.domain.components.package import is_rigid_package

        assert not is_rigid_package(defn.package)
        assert defn.default_footprint().pin_count() == 2
        from protoboard.domain.components.package import effective_bend_specs

        assert set(effective_bend_specs(defn)) == {1, 2}

    def test_no_catalogue_skips_ref_only_parts(
        self,
        tmp_path: Path,
    ) -> None:
        # With an explicit empty catalogue the ref — the parts' ONLY geometry
        # source — cannot resolve: the parts are skipped with a warning
        # instead of sinking the library (same policy as a malformed package).
        from structlog.testing import capture_logs

        (tmp_path / "p.yaml").write_text(self._LIB)
        with capture_logs() as logs:
            lib = load_library([tmp_path], {})
        assert "R_SMD" not in lib and "R_AXIAL" not in lib
        assert sum(1 for e in logs if e["event"] == "library.package_skipped") == 2


class TestLoadLibrary:
    def test_load_single_directory(self) -> None:
        lib = load_library([FIXTURES])
        assert isinstance(lib, ComponentLibrary)
        names = sorted(lib.names())
        assert "R" in names and "C" in names and "LM358" in names and "BC547" in names

    def test_get_lm358(self) -> None:
        lib = load_library([FIXTURES])
        defn = lib.get("LM358")
        assert isinstance(defn, ComponentDefinition)
        assert defn.name == "LM358"
        # DIP-8 geometry: two rows of 4 pins, columns 3 holes apart.
        assert defn.default_footprint().pin_count() == 8
        xs = sorted({round(p.x, 1) for p in defn.default_footprint().pin_positions()})
        assert xs == [0.0, 3.0]
        assert defn.pin_count == 8
        assert defn.pin_name(1) == "OUT_A"
        assert defn.pin_name(8) == "VCC"

    def test_get_resistor(self) -> None:
        lib = load_library([FIXTURES])
        defn = lib.get("R")
        # Two-pin declared generic prism: a single in-line pair of rigid pads.
        from protoboard.domain.components.package import is_rigid_package

        assert is_rigid_package(defn.package)
        assert defn.pin_count == 2
        pins = [
            (round(p.x), round(p.y)) for p in defn.default_footprint().pin_positions()
        ]
        assert pins == [(0, 0), (0, 1)]
        # Pin names not declared → defaults.
        assert defn.pin_name(1) == "P1"
        assert defn.pin_name(2) == "P2"

    def test_get_to92_transistor(self) -> None:
        lib = load_library([FIXTURES])
        defn = lib.get("BC547")
        # TO-92 catalogue body → single in-line row of 3 pins (along x).
        assert defn.default_footprint().pin_count() == 3
        ys = {round(p.y) for p in defn.default_footprint().pin_positions()}
        assert ys == {0}  # single row
        assert defn.pin_count == 3
        assert defn.pin_name(1) == "C"
        assert defn.pin_name(2) == "B"
        assert defn.pin_name(3) == "E"

    def test_unknown_component_raises(self) -> None:
        lib = load_library([FIXTURES])
        with pytest.raises(KeyError):
            lib.get("UNKNOWN")

    def test_contains(self) -> None:
        lib = load_library([FIXTURES])
        assert "LM358" in lib
        assert "NotThere" not in lib


class TestLibraryOverride:
    def test_first_directory_wins(self, tmp_path: Path) -> None:
        # User override directory contains a redefinition of LM358.
        override_dir = tmp_path / "override"
        override_dir.mkdir()
        (override_dir / "custom.yaml").write_text("""
components:
  - name: LM358
    description: "Customized op-amp"
    pins: {count: 8}
    package: {kind: dip_ic}
    pin_names: {}
""")
        lib = load_library([override_dir, FIXTURES])
        defn = lib.get("LM358")
        assert defn.description == "Customized op-amp"

    def test_second_directory_supplements(self, tmp_path: Path) -> None:
        # Second directory introduces a component absent in the first.
        extra_dir = tmp_path / "extra"
        extra_dir.mkdir()
        (extra_dir / "more.yaml").write_text("""
components:
  - name: NEW_PART
    description: "New"
    pins: {count: 4}
    package: {kind: sil}
    pin_names: {}
""")
        lib = load_library([FIXTURES, extra_dir])
        assert "NEW_PART" in lib
        assert "LM358" in lib  # still present from FIXTURES


class TestAliasInheritance:
    """An alias is the parent definition under a different name — it inherits
    every field, including footprints / default_value."""

    def test_alias_inherits_package_ref_and_value(
        self,
        tmp_path: Path,
    ) -> None:
        d = tmp_path / "lib"
        d.mkdir()
        (d / "parts.yaml").write_text("""
components:
  - name: R
    description: "Resistor"
    component_class: resistor
    pins: {count: 2}
    package: {kind: sil}
    pin_names: {}
    footprints:
      - {name: DIN-0207, ref: "DIN-0207/typ"}
    default_value: "?"
    aliases:
      - R_Axial_DIN0207
      - R_0603_1608Metric
""")
        lib = load_library([d])
        parent = lib.get("R")
        for alias_name in ("R_Axial_DIN0207", "R_0603_1608Metric"):
            a = lib.get(alias_name)
            assert (
                a.default_package_ref()
                == parent.default_package_ref()
                == "DIN-0207/typ"
            )
            assert a.default_value == parent.default_value == "?"
            assert a.component_class == parent.component_class


class TestLibraryErrors:
    def test_invalid_yaml_raises(self, tmp_path: Path) -> None:
        bad_dir = tmp_path / "bad"
        bad_dir.mkdir()
        (bad_dir / "broken.yaml").write_text("this is: : not valid")
        with pytest.raises(SerializationError):
            load_library([bad_dir])

    def test_removed_footprint_field_skips_part(self, tmp_path: Path) -> None:
        # The legacy field is rejected per-part (skip + hint), not per-library.
        from structlog.testing import capture_logs

        bad_dir = tmp_path / "bad"
        bad_dir.mkdir()
        (bad_dir / "x.yaml").write_text("""
components:
  - name: X
    footprint:
      type: SIL
      pins: 4
    pin_names: {}
""")
        with capture_logs() as logs:
            lib = load_library([bad_dir])
        assert "X" not in lib
        assert any(
            "'footprint:' field was removed" in str(e.get("error")) for e in logs
        )

    def test_missing_footprint_raises(self, tmp_path: Path) -> None:
        bad_dir = tmp_path / "bad"
        bad_dir.mkdir()
        (bad_dir / "x.yaml").write_text("""
components:
  - name: X
    pin_names: {}
""")
        with pytest.raises(SerializationError):
            load_library([bad_dir])

    def test_dip_odd_pins_skips_part(self, tmp_path: Path) -> None:
        # An unrealisable kind (odd DIP) is a PackageDefError → part skipped.
        bad_dir = tmp_path / "bad"
        bad_dir.mkdir()
        (bad_dir / "x.yaml").write_text("""
components:
  - name: X
    pins: {count: 7}
    package: {kind: dip_ic}
    pin_names: {}
""")
        lib = load_library([bad_dir])
        assert "X" not in lib

    def test_nonexistent_directory_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_library([tmp_path / "does_not_exist"])

    def test_empty_directory_yields_empty_library(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        lib = load_library([empty])
        assert list(lib.names()) == []


class TestExtendsResolution:
    """Tests for the 'extends' template inheritance mechanism."""

    def _make_lib(self, tmp_path: Path, yaml: str) -> "ComponentLibrary":
        d = tmp_path / "lib"
        d.mkdir(exist_ok=True)
        (d / "parts.yaml").write_text(yaml)
        return load_library([d])

    def test_extends_inherits_footprint_and_class(self, tmp_path: Path) -> None:
        lib = self._make_lib(
            tmp_path,
            """
components:
  - name: _BASE
    component_class: transistor_npn
    footprints: [{name: TO92, ref: "TO-92"}]
    pin_names: {1: C, 2: B, 3: E}
  - name: MyNPN
    extends: _BASE
    description: "custom npn"
""",
        )
        defn = lib.get("MyNPN")
        from protoboard.domain.components.definition import ComponentClass

        # TO-92 catalogue body → single in-line row of 3 pins, inherited
        # via extends (the row runs along x in the package frame).
        assert defn.default_footprint().pin_count() == 3
        assert {round(p.y) for p in defn.default_footprint().pin_positions()} == {0}
        assert defn.pin_count == 3
        assert defn.pin_name(1) == "C"
        assert defn.component_class == ComponentClass.TRANSISTOR_NPN
        assert defn.description == "custom npn"

    def test_template_not_placeable(self, tmp_path: Path) -> None:
        lib = self._make_lib(
            tmp_path,
            """
components:
  - name: _TMPL
    component_class: transistor_npn
    footprints: [{name: TO92, ref: "TO-92"}]
    pin_names: {1: C, 2: B, 3: E}
  - name: RealPart
    extends: _TMPL
""",
        )
        assert "_TMPL" not in lib
        assert "RealPart" in lib

    def test_child_overrides_pin_names(self, tmp_path: Path) -> None:
        lib = self._make_lib(
            tmp_path,
            """
components:
  - name: _BASE
    component_class: transistor_npn
    footprints: [{name: TO92, ref: "TO-92"}]
    pin_names: {1: C, 2: B, 3: E}
  - name: AltPinout
    extends: _BASE
    pin_names: {1: E, 2: B, 3: C}
""",
        )
        defn = lib.get("AltPinout")
        assert defn.pin_name(1) == "E"
        assert defn.pin_name(3) == "C"

    def test_multilevel_extends(self, tmp_path: Path) -> None:
        lib = self._make_lib(
            tmp_path,
            """
components:
  - name: _NPN
    component_class: transistor_npn
    footprints: [{name: TO92, ref: "TO-92"}]
    pin_names: {}
  - name: _NPN_CBE
    extends: _NPN
    pin_names: {1: C, 2: B, 3: E}
  - name: BC547
    extends: _NPN_CBE
    description: "NPN 45V"
""",
        )
        defn = lib.get("BC547")
        assert defn.pin_name(2) == "B"
        assert defn.description == "NPN 45V"

    def test_circular_extends_raises(self, tmp_path: Path) -> None:
        with pytest.raises(SerializationError, match="circular"):
            self._make_lib(
                tmp_path,
                """
components:
  - name: A
    extends: B
    footprints: [{name: TO92, ref: "TO-92"}]
    pin_names: {}
  - name: B
    extends: A
    footprints: [{name: TO92, ref: "TO-92"}]
    pin_names: {}
""",
            )

    def test_unknown_extends_target_raises(self, tmp_path: Path) -> None:
        with pytest.raises(SerializationError, match="unknown template"):
            self._make_lib(
                tmp_path,
                """
components:
  - name: MyPart
    extends: _NONEXISTENT
    footprints: [{name: TO92, ref: "TO-92"}]
    pin_names: {}
""",
            )

    def test_fixture_bc547_inherits_correctly(self) -> None:
        lib = load_library([FIXTURES])
        defn = lib.get("BC547")
        from protoboard.domain.components.definition import ComponentClass

        assert defn.pin_name(1) == "C"
        assert defn.component_class == ComponentClass.TRANSISTOR_NPN

    def test_empty_directory_yields_empty_library(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        lib = load_library([empty])
        assert list(lib.names()) == []


class TestPinVariants:
    def _make_lib(self, tmp_path: Path, yaml_text: str) -> ComponentLibrary:
        (tmp_path / "parts.yaml").write_text(yaml_text)
        return load_library([tmp_path])

    def test_variants_parsed(self, tmp_path: Path) -> None:
        lib = self._make_lib(
            tmp_path,
            """
components:
  - name: Header4
    component_class: connector
    pins: {count: 4}
    package: {kind: sil}
    pin_names: {1: "1", 2: "2", 3: "3", 4: "4"}
    variants:
      - id: i2c
        label: "I2C (VCC/GND/SDA/SCL)"
        pin_names: {1: VCC, 2: GND, 3: SDA, 4: SCL}
      - id: uart
        label: "UART"
        pin_names: {1: VCC, 2: GND, 3: TX, 4: RX}
""",
        )
        defn = lib.get("Header4")
        assert len(defn.variants) == 2
        assert defn.variants[0].id == "i2c"
        assert defn.variants[0].label == "I2C (VCC/GND/SDA/SCL)"
        assert defn.variants[1].id == "uart"

    def test_no_variants_default_empty(self, tmp_path: Path) -> None:
        lib = self._make_lib(
            tmp_path,
            """
components:
  - name: Header4
    component_class: connector
    pins: {count: 4}
    package: {kind: sil}
    pin_names: {1: "1", 2: "2", 3: "3", 4: "4"}
""",
        )
        defn = lib.get("Header4")
        assert defn.variants == ()

    def test_effective_pin_names_default(self, tmp_path: Path) -> None:
        lib = self._make_lib(
            tmp_path,
            """
components:
  - name: Header4
    component_class: connector
    pins: {count: 4}
    package: {kind: sil}
    pin_names: {1: "1", 2: "2", 3: "3", 4: "4"}
    variants:
      - id: i2c
        label: "I2C"
        pin_names: {1: VCC, 2: GND, 3: SDA, 4: SCL}
""",
        )
        defn = lib.get("Header4")
        assert defn.effective_pin_names(None) == {1: "1", 2: "2", 3: "3", 4: "4"}

    def test_effective_pin_names_with_variant(self, tmp_path: Path) -> None:
        lib = self._make_lib(
            tmp_path,
            """
components:
  - name: Header4
    component_class: connector
    pins: {count: 4}
    package: {kind: sil}
    pin_names: {1: "1", 2: "2", 3: "3", 4: "4"}
    variants:
      - id: i2c
        label: "I2C"
        pin_names: {1: VCC, 2: GND, 3: SDA, 4: SCL}
""",
        )
        defn = lib.get("Header4")
        names = defn.effective_pin_names("i2c")
        assert names == {1: "VCC", 2: "GND", 3: "SDA", 4: "SCL"}

    def test_effective_pin_names_partial_override(self, tmp_path: Path) -> None:
        lib = self._make_lib(
            tmp_path,
            """
components:
  - name: Header4
    component_class: connector
    pins: {count: 4}
    package: {kind: sil}
    pin_names: {1: "1", 2: "2", 3: "3", 4: "4"}
    variants:
      - id: partial
        label: "Partial"
        pin_names: {3: SDA, 4: SCL}
""",
        )
        defn = lib.get("Header4")
        names = defn.effective_pin_names("partial")
        assert names[1] == "1"
        assert names[2] == "2"
        assert names[3] == "SDA"
        assert names[4] == "SCL"

    def test_effective_pin_names_unknown_variant_fallback(self, tmp_path: Path) -> None:
        lib = self._make_lib(
            tmp_path,
            """
components:
  - name: Header4
    component_class: connector
    pins: {count: 4}
    package: {kind: sil}
    pin_names: {1: "1", 2: "2", 3: "3", 4: "4"}
    variants:
      - id: i2c
        label: "I2C"
        pin_names: {1: VCC, 2: GND, 3: SDA, 4: SCL}
""",
        )
        defn = lib.get("Header4")
        names = defn.effective_pin_names("nonexistent")
        assert names == {1: "1", 2: "2", 3: "3", 4: "4"}

    def test_connectors_resolve_parametrically(self) -> None:
        # headers.yaml carries parametric entries (variable pin count); the
        # historical fixed-count names synthesize on lookup.  The pin-name
        # presets (UART/I2C/…) were deliberately dropped with that rewrite —
        # they return as generator-side per-count config (docs/todo.md).
        from protoboard.services.library.package_family_loader import (
            load_full_package_catalogue,
        )

        lib = load_library(
            [FIXTURES.parent.parent.parent / "library"],
            load_full_package_catalogue(),
        )
        assert lib.get("PinHeader").parametric_pins
        defn = lib.get("PinHeader_1x04")
        assert defn.pin_count == 4
        assert not defn.parametric_pins
        assert defn.variants == ()


class TestPackageInLibrary:
    """Components may declare a parametric ``package:`` instead of footprint."""

    def _write(self, tmp_path: Path, text: str) -> Path:
        d = tmp_path / "lib"
        d.mkdir()
        (d / "parts.yaml").write_text(text)
        return d

    def test_component_with_package_loads(self, tmp_path: Path) -> None:
        d = self._write(
            tmp_path,
            """
components:
  - name: C_POL
    component_class: capacitor_polar
    pin_names: {1: "+", 2: "-"}
    package:
      extends: cylinder_radial
      dims: {diameter: 6.3, length: 11.0}
      leads:
        body_offset: {unit: 2.5, value: [[1, 0]]}
      bend: 4.0
""",
        )
        lib = load_library([d])
        defn = lib.get("C_POL")
        assert defn.package is not None  # package attached
        assert defn.pin_count == 2  # footprint derived from package
        # footprint pin positions come from the package
        assert defn.default_footprint().pin_positions()[0].x == 0.0

    def test_package_component_needs_no_footprint(self, tmp_path: Path) -> None:
        d = self._write(
            tmp_path,
            """
components:
  - name: R
    component_class: resistor
    pin_names: {}
    package:
      extends: cylinder_axial
      dims: {diameter: 2.5, length: 6.3}
      leads: {lead_r: 0.27, lead_length: 32.0}
""",
        )
        lib = load_library([d])
        assert lib.get("R").pin_count == 2  # no footprint: key needed

    def test_malformed_package_skips_component_not_crash(self, tmp_path: Path) -> None:
        # Missing required dims (null) → package won't build → component skipped,
        # but the rest of the library still loads.
        d = self._write(
            tmp_path,
            """
components:
  - name: BadPart
    component_class: resistor
    pin_names: {}
    package:
      extends: cylinder_axial
      leads: {lead_r: 0.27, lead_length: 32.0}
  - name: GoodPart
    pins: {count: 2}
    package: {kind: sil}
    pin_names: {}
""",
        )
        lib = load_library([d])
        assert "BadPart" not in lib  # malformed package → skipped
        assert "GoodPart" in lib  # rest loads fine
