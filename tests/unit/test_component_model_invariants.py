# SPDX-License-Identifier: MIT
"""Contract tests for the component/package/footprint model.

These tests describe behaviour that should survive the upcoming domain-model
refactor. They intentionally avoid checking internal representation details
that are free to change.
"""

from pathlib import Path

import pytest

from protoboard.domain.components.definition import (
    ComponentDefinition,
    MetaPin,
    PackageOption,
)
from protoboard.domain.components.footprints import Footprint
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.package import (
    package_to_footprint,
    resolve_component_package,
)
from protoboard.domain.components.package.component_package import (
    resolved_footprint,
)
from protoboard.domain.components.package.generated_families import (
    generate_inline_families,
)
from protoboard.errors import SerializationError, UnboundInstanceError
from protoboard.services.library.loader import load_library


def test_metapin_requires_at_least_one_non_empty_label() -> None:
    with pytest.raises(ValueError):
        MetaPin(())
    with pytest.raises(ValueError):
        MetaPin(("B", ""))


def test_package_option_maps_one_metapin_to_many_package_pins() -> None:
    option = PackageOption(
        name="DIP",
        ref="dip-8/std",
        pin_map={1: (1, 8), 2: ()},
    )

    assert option.pin_map == {1: (1, 8), 2: ()}


def test_package_option_rejects_non_positive_metapin_index() -> None:
    with pytest.raises(ValueError):
        PackageOption(name="DIP", ref="dip-8/std", pin_map={0: (1,)})


def test_definition_exposes_metapins_by_one_based_ordinal() -> None:
    definition = ComponentDefinition(
        name="Q",
        meta_pins=(MetaPin(("B",)), MetaPin(("C",)), MetaPin(("E",))),
        package_options=(
            PackageOption(
                name="TO92",
                ref="to92/std",
                pin_map={1: (2,), 2: (1,), 3: (3,)},
            ),
        ),
    )

    assert definition.meta_pin(2).labels == ("C",)
    with pytest.raises(ValueError):
        definition.meta_pin(0)


def test_inline_package_definition_exposes_default_geometry(tmp_path: Path) -> None:
    (tmp_path / "mod.yaml").write_text(
        "components:\n"
        "  - name: MOD_2x4\n"
        "    component_class: devboard\n"
        "    package:\n"
        "      shape: prism\n"
        "      dims: {ly: 12.5, lx: 12.5, lz: 1.6}\n"
        "      leads:\n"
        "        direction: [0, 0, -1]\n"
        "        body_offset: {unit: 2.5, value: [[3, [0, 1]], [5, 0], [3, [0, -1]]]}\n"
        "        board_offset: default\n"
        "      bend: 0\n"
        "    meta_pins: [A, P2, P3, P4, P5, P6, P7, B]\n"
        "    pin_map: {1: [1], 2: [2], 3: [3], 4: [4], 5: [5], 6: [6], 7: [7], 8: [8]}\n"  # noqa: E501
    )

    library = load_library([tmp_path])
    defn = library.get("MOD_2x4")
    package = resolve_component_package(defn, None, library.package_families)

    assert defn.default_package_ref() == "MOD_2x4-package/std"
    assert "MOD_2x4-package" in library.package_families
    assert package is not None
    pins = package.pin_positions()
    assert [meta_pin.labels[0] for meta_pin in defn.meta_pins] == [
        "A",
        "P2",
        "P3",
        "P4",
        "P5",
        "P6",
        "P7",
        "B",
    ]
    assert len(defn.meta_pins) == 8
    assert [(round(p.x), round(p.y)) for p in pins] == [
        (0, 0),
        (0, 1),
        (0, 2),
        (0, 3),
        (5, 3),
        (5, 2),
        (5, 1),
        (5, 0),
    ]


def test_first_footprint_option_defines_default_geometry(tmp_path: Path) -> None:
    (tmp_path / "ic.yaml").write_text(
        "components:\n"
        "  - name: U_DIP14\n"
        "    component_class: ic\n"
        "    symbol: U\n"
        "    package_options:\n"
        "      - name: DIP-14\n"
        "        package: {kind: dip_ic}\n"
        "      - name: WDIP-14\n"
        "        package: {kind: wdip_ic}\n"
        "    meta_pins: [P1, P2, P3, P4, P5, P6, GND, P8, P9, P10, P11, P12, P13, VCC]\n"  # noqa: E501
    )

    library = load_library([tmp_path])
    defn = library.get("U_DIP14")
    footprint = defn.default_footprint(library.package_families)

    assert len(defn.package_options) == 2
    assert footprint.pin_count() == 14
    assert footprint.pin_positions()[7].x == 3.0
    assert [option.ref for option in defn.package_options] == [
        "U_DIP14-DIP-14-package/std",
        "U_DIP14-WDIP-14-package/std",
    ]


def test_instance_package_variant_overrides_definition_default(tmp_path: Path) -> None:
    (tmp_path / "ic.yaml").write_text(
        "components:\n"
        "  - name: U_DIP16\n"
        "    component_class: ic\n"
        "    symbol: U\n"
        "    package_options:\n"
        "      - {name: DIP-16, ref: '@dip-16/std'}\n"
        "      - {name: WDIP-16, ref: '@wdip-16/std'}\n"
        "    meta_pins: [P1, P2, P3, P4, P5, P6, P7, P8, P9, P10, P11, P12, P13, P14, P15, P16]\n"  # noqa: E501
    )

    families = generate_inline_families()
    defn = load_library([tmp_path], package_families=families).get("U_DIP16")
    inst = ComponentInstance.new(
        definition_ref="U_DIP16",
        refdes="U1",
        value="",
        package_variant="wdip-16/std",
    )
    bound = inst.bind(defn, families)

    pkg = resolve_component_package(defn, bound, families)
    fp = bound.resolved_footprint()

    assert pkg is not None
    assert fp.pin_count() == 16
    assert fp.pin_positions()[8].x == 6.0


def test_placed_component_geometry_requires_an_instance(tmp_path: Path) -> None:
    (tmp_path / "r.yaml").write_text(
        "components:\n"
        "  - name: R\n"
        "    component_class: resistor\n"
        "    symbol: R\n"
        "    package: {kind: sil}\n"
        "    meta_pins: [A, B]\n"
        "    pin_map: {1: [1], 2: [2]}\n"
    )

    defn = load_library([tmp_path]).get("R")

    with pytest.raises(UnboundInstanceError):
        resolved_footprint(defn, None, None)


def test_package_is_its_own_footprint() -> None:
    package = generate_inline_families()["dip-8"].variants[0].package

    assert isinstance(package, Footprint)
    assert package_to_footprint(package) is package


def test_component_instance_has_no_pin_variant_state() -> None:
    instance = ComponentInstance.new("R", "R1", "10k")

    assert not hasattr(instance, "pin_variant")


def test_legacy_family_ref_component_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "r.yaml").write_text(
        "components:\n"
        "  - name: R\n"
        "    component_class: resistor\n"
        "    package_family_ref: dip-16/std\n"
        "    package: {kind: dip_ic}\n"
        ""
    )

    with pytest.raises(SerializationError, match="package_family_ref"):
        load_library([tmp_path], package_families=generate_inline_families())
