# SPDX-License-Identifier: MIT
"""Parametric (variable-pin-count) component definitions.

A ``package: {ref: "$…"}`` entry with no ``pins:`` loads as a parametric base;
placement-time parameters concretize it under the historical catalogue name
(``PinHeader_1x05``), and name lookups re-synthesize — so saved projects
referencing the old fixed-count names keep resolving.
"""

from __future__ import annotations

import pytest

from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.package.loader import PackageDefError
from protoboard.paths import BUILTIN_LIBRARY_DIR
from protoboard.services.library.loader import (
    _component_from_dict,
    load_library,
)
from protoboard.services.library.parametric import (
    ComponentCatalog,
    concretize,
    resolve_parametric_name,
)

PIN_HEADER_RAW = {
    "name": "PinHeader",
    "component_class": "connector",
    "package": {"ref": "$pin_header"},
    "description": "test header",
}
JST_RAW = {
    "name": "JST_XH",
    "component_class": "connector",
    "package": {"ref": "$jst"},
}


def _parse(raw: dict, families: dict | None = None) -> ComponentDefinition:
    from pathlib import Path

    catalogue = families if families is not None else {}
    return _component_from_dict(dict(raw), Path("test.yaml"), catalogue, catalogue)


class TestLoaderParametric:
    def test_kind_without_pins_loads_parametric(self) -> None:
        defn = _parse(PIN_HEADER_RAW)
        assert defn.parametric_pins
        # Preview body at the generator's preview count.
        assert len(defn.meta_pins) == 4

    def test_declared_meta_pins_stay_concrete(self) -> None:
        raw = {
            **PIN_HEADER_RAW,
            "meta_pins": [f"P{index}" for index in range(1, 7)],
        }
        defn = _parse(raw)
        assert not defn.parametric_pins
        assert len(defn.meta_pins) == 6


class TestConcretize:
    def test_pin_header_single_row_name_and_count(self) -> None:
        families: dict = {}
        base = _parse(PIN_HEADER_RAW, families)
        c = concretize(base, 5, families, {"rows": 1})
        assert c.name == "PinHeader_1x05"
        assert len(c.meta_pins) == 5
        assert not c.parametric_pins
        assert c.default_package_ref() == "PinHeader_1x05-package/std"
        assert "PinHeader_1x05-package" in families
        assert c.package_options[0].pin_map == {
            1: (1,),
            2: (2,),
            3: (3,),
            4: (4,),
            5: (5,),
        }

    def test_pin_header_dual_row(self) -> None:
        families: dict = {}
        base = _parse(PIN_HEADER_RAW, families)
        c = concretize(base, 16, families, {"rows": 2})
        assert c.name == "PinHeader_2x08"
        assert len(c.meta_pins) == 16

    def test_jst_name(self) -> None:
        families: dict = {}
        base = _parse(JST_RAW, families)
        c = concretize(base, 4, families, {})
        assert c.name == "JST_XH_4pin"
        assert len(c.meta_pins) == 4

    def test_unrealisable_parameters_raise(self) -> None:
        families: dict = {}
        base = _parse(PIN_HEADER_RAW, families)
        with pytest.raises(PackageDefError):
            concretize(base, 5, families, {"rows": 2})

    def test_non_parametric_definition_raises(self) -> None:
        families: dict = {}
        concrete = _parse(
            {**PIN_HEADER_RAW, "meta_pins": ["P1", "P2", "P3", "P4"]},
            families,
        )
        with pytest.raises(PackageDefError):
            concretize(concrete, 5, families, {})


class TestResolveByName:
    def test_legacy_pin_header_names_resolve(self) -> None:
        families: dict = {}
        components = {"PinHeader": _parse(PIN_HEADER_RAW, families)}
        for name, count in (
            ("PinHeader_1x02", 2),
            ("PinHeader_1x05", 5),
            ("PinHeader_2x05", 10),
            ("PinHeader_2x08", 16),
        ):
            synth = resolve_parametric_name(components, name, families)
            assert synth is not None, name
            assert synth.name == name
            assert len(synth.meta_pins) == count

    def test_legacy_jst_names_resolve(self) -> None:
        families: dict = {}
        components = {"JST_XH": _parse(JST_RAW, families)}
        synth = resolve_parametric_name(components, "JST_XH_4pin", families)
        assert synth is not None
        assert len(synth.meta_pins) == 4

    def test_garbage_and_unrealisable_names_do_not(self) -> None:
        families: dict = {}
        components = {"PinHeader": _parse(PIN_HEADER_RAW, families)}
        assert resolve_parametric_name(components, "PinHeader_ABC", families) is None
        assert resolve_parametric_name(components, "PinHeader_2x", families) is None
        assert resolve_parametric_name(components, "Other_1x05", families) is None


class TestComponentCatalog:
    def test_getitem_synthesizes_and_caches(self) -> None:
        families: dict = {}
        catalog = ComponentCatalog(
            {"PinHeader": _parse(PIN_HEADER_RAW, families)},
            package_families=families,
        )
        c = catalog["PinHeader_1x06"]
        assert len(c.meta_pins) == 6
        assert "PinHeader_1x06" in catalog  # cached
        assert catalog.get("PinHeader_1x06") is c

    def test_get_returns_default_on_true_miss(self) -> None:
        families: dict = {}
        catalog = ComponentCatalog(
            {"PinHeader": _parse(PIN_HEADER_RAW, families)},
            package_families=families,
        )
        assert catalog.get("NoSuchPart") is None
        with pytest.raises(KeyError):
            _ = catalog["NoSuchPart"]


class TestBuiltinLibrary:
    def test_headers_load_parametric_and_legacy_names_resolve(self) -> None:
        lib = load_library([BUILTIN_LIBRARY_DIR])
        base = lib.get("PinHeader")
        assert base.parametric_pins
        # Legacy fixed-count catalogue names synthesize through get().
        assert len(lib.get("PinHeader_1x04").meta_pins) == 4
        assert len(lib.get("PinHeader_2x03").meta_pins) == 6
        assert len(lib.get("JST_PH_2pin").meta_pins) == 2
        assert len(lib.get("JST_XH_5pin").meta_pins) == 5
        with pytest.raises(KeyError):
            lib.get("PinHeader_3x04")  # rows > 2 never realises
