# SPDX-License-Identifier: MIT
"""Contract tests for the component/package/footprint model.

These tests describe behaviour that should survive the upcoming domain-model
refactor. They intentionally avoid checking internal representation details
that are free to change.
"""

from pathlib import Path

import pytest

from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.footprints import Footprint
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
from protoboard.errors import UnboundInstanceError
from protoboard.services.library.loader import load_library


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
        "    pin_names: {1: A, 8: B}\n"
    )

    library = load_library([tmp_path])
    defn = library.get("MOD_2x4")
    package = resolve_component_package(defn, None, library.package_families)

    assert defn.default_package_ref() == "MOD_2x4-package/std"
    assert "MOD_2x4-package" in library.package_families
    assert package is not None
    pins = package.pin_positions()
    assert defn.pin_names == {
        1: "A",
        2: "P2",
        3: "P3",
        4: "P4",
        5: "P5",
        6: "P6",
        7: "P7",
        8: "B",
    }
    assert defn.pin_count == 8
    assert [(round(p.x), round(p.y)) for p in pins] == [
        (0, 0), (0, 1), (0, 2), (0, 3),
        (5, 3), (5, 2), (5, 1), (5, 0),
    ]


def test_first_footprint_option_defines_default_geometry(tmp_path: Path) -> None:
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
        "    footprints:\n"
        "      - {name: DIP-16, ref: dip-16/std}\n"
        "    package: {kind: dip_ic}\n"
        "    pin_names: {}\n"
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
        "    pins: {count: 2}\n"
        "    pin_names: {1: A, 2: B}\n"
    )

    defn = load_library([tmp_path]).get("R")

    with pytest.raises(UnboundInstanceError):
        resolved_footprint(defn, None, None)


def test_package_is_its_own_footprint() -> None:
    package = generate_inline_families()["dip-8"].variants[0].package

    assert isinstance(package, Footprint)
    assert package_to_footprint(package) is package


def test_legacy_family_ref_component_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "r.yaml").write_text(
        "components:\n"
        "  - name: R\n"
        "    component_class: resistor\n"
        "    package_family_ref: dip-16/std\n"
        "    package: {kind: dip_ic}\n"
        "    pin_names: {}\n"
    )

    library = load_library(
        [tmp_path], package_families=generate_inline_families())

    assert "R" not in library
