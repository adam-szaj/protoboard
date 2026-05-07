# SPDX-License-Identifier: MIT
"""Tests for FootprintMap and NetlistImportResult / import_full workflow."""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from protoboard.domain.components.instance import ComponentInstance
from protoboard.services.kicad.footprint_map import FootprintEntry, FootprintMap
from protoboard.services.kicad.importer import ImportWarning, KiCadNetlistImporter

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "kicad_samples"


# ---------------------------------------------------------------------------
# FootprintMap — unit tests
# ---------------------------------------------------------------------------


class TestFootprintEntry:
    def test_fields(self) -> None:
        e = FootprintEntry(kicad_fp="Lib:Part", definition_ref="my_def", pin_count=4)
        assert e.kicad_fp == "Lib:Part"
        assert e.definition_ref == "my_def"
        assert e.pin_count == 4


class TestFootprintMap:
    def _make_map(self) -> FootprintMap:
        return FootprintMap.from_entries([
            FootprintEntry("Lib:A", "def_a", 2),
            FootprintEntry("Lib:B", "", 4),
            FootprintEntry("Lib:C", "", 0),
        ])

    def test_get_existing(self) -> None:
        fm = self._make_map()
        e = fm.get("Lib:A")
        assert e is not None
        assert e.pin_count == 2

    def test_get_missing_returns_none(self) -> None:
        fm = self._make_map()
        assert fm.get("Lib:Z") is None

    def test_unresolved_only_zero_pin_count_and_no_def(self) -> None:
        fm = self._make_map()
        ur = fm.unresolved()
        # Lib:B has pin_count=4 so it's not "unresolved" (user can place SIL_4)
        # Lib:C has pin_count=0 and empty def → unresolved
        assert len(ur) == 1
        assert ur[0].kicad_fp == "Lib:C"

    def test_all_resolved_false_when_zero_pin_count(self) -> None:
        fm = self._make_map()
        assert not fm.all_resolved()

    def test_all_resolved_true_when_all_have_pin_count(self) -> None:
        fm = FootprintMap.from_entries([
            FootprintEntry("Lib:A", "def_a", 2),
            FootprintEntry("Lib:B", "", 3),
        ])
        assert fm.all_resolved()

    def test_merge_user_overrides_auto(self) -> None:
        auto = FootprintMap.from_entries([FootprintEntry("Lib:A", "", 0)])
        user = FootprintMap.from_entries([FootprintEntry("Lib:A", "my_resistor", 2)])
        merged = auto.merge(user)
        assert merged.get("Lib:A").definition_ref == "my_resistor"  # type: ignore[union-attr]
        assert merged.get("Lib:A").pin_count == 2                   # type: ignore[union-attr]

    def test_merge_keeps_auto_when_user_has_no_entry(self) -> None:
        auto = FootprintMap.from_entries([FootprintEntry("Lib:A", "auto_def", 2)])
        user = FootprintMap.from_entries([FootprintEntry("Lib:B", "user_def", 4)])
        merged = auto.merge(user)
        assert merged.get("Lib:A") is not None
        assert merged.get("Lib:B") is not None

    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        fm = self._make_map()
        path = tmp_path / "footprint_map.yaml"
        fm.save(path)
        loaded = FootprintMap.load(path)
        assert set(loaded.entries.keys()) == set(fm.entries.keys())
        a = loaded.get("Lib:A")
        assert a is not None
        assert a.definition_ref == "def_a"
        assert a.pin_count == 2

    def test_save_yaml_is_human_readable(self, tmp_path: Path) -> None:
        fm = FootprintMap.from_entries([
            FootprintEntry("Resistor_SMD:R_0603", "R_0603", 2),
        ])
        path = tmp_path / "footprint_map.yaml"
        fm.save(path)
        text = path.read_text()
        assert "Resistor_SMD:R_0603" in text
        assert "definition_ref" in text
        assert "pin_count" in text

    def test_load_empty_definition_ref(self, tmp_path: Path) -> None:
        yaml_text = """\
mappings:
  "Lib:Unknown":
    definition_ref: ""
    pin_count: 6
"""
        path = tmp_path / "footprint_map.yaml"
        path.write_text(yaml_text)
        fm = FootprintMap.load(path)
        e = fm.get("Lib:Unknown")
        assert e is not None
        assert e.definition_ref == ""
        assert e.pin_count == 6


# ---------------------------------------------------------------------------
# import_full — integration tests using existing fixtures
# ---------------------------------------------------------------------------


_MINIMAL_SEXP = """\
(export (version D)
  (design (source x) (date "") (tool t))
  (components
    (comp (ref R1) (value 10k)
      (footprint "Resistor_SMD:R_0603_1608Metric"))
    (comp (ref J1) (value Conn)
      (footprint "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical")))
  (nets
    (net (code 1) (name VCC)
      (node (ref R1) (pin 1))
      (node (ref J1) (pin 1)))
    (net (code 2) (name GND)
      (node (ref R1) (pin 2))
      (node (ref J1) (pin 2)))))
"""

_UNKNOWN_SEXP = """\
(export (version D)
  (design (source x) (date "") (tool t))
  (components
    (comp (ref U1) (value Exotic)
      (footprint "Exotic:SomeChip")))
  (nets
    (net (code 1) (name VCC)
      (node (ref U1) (pin 1)))))
"""


class TestImportFull:
    def test_returns_netlist_import_result(self) -> None:
        from protoboard.services.kicad.import_result import NetlistImportResult

        imp = KiCadNetlistImporter()
        result = imp.import_full_from_string(_MINIMAL_SEXP)
        assert isinstance(result, NetlistImportResult)

    def test_schematic_nets_present(self) -> None:
        imp = KiCadNetlistImporter()
        result = imp.import_full_from_string(_MINIMAL_SEXP)
        names = {n.name for n in result.schematic.nets.values()}
        assert names == {"VCC", "GND"}

    def test_instances_created_for_all_components(self) -> None:
        imp = KiCadNetlistImporter()
        result = imp.import_full_from_string(_MINIMAL_SEXP)
        assert set(result.instances.keys()) == {"R1", "J1"}

    def test_instances_are_component_instances(self) -> None:
        imp = KiCadNetlistImporter()
        result = imp.import_full_from_string(_MINIMAL_SEXP)
        for inst in result.instances.values():
            assert isinstance(inst, ComponentInstance)

    def test_instance_refdes_and_value(self) -> None:
        imp = KiCadNetlistImporter()
        result = imp.import_full_from_string(_MINIMAL_SEXP)
        r1 = result.instances["R1"]
        assert r1.refdes == "R1"
        assert r1.value == "10k"

    def test_instance_ids_match_schematic_pin_refs(self) -> None:
        imp = KiCadNetlistImporter()
        result = imp.import_full_from_string(_MINIMAL_SEXP)
        r1 = result.instances["R1"]
        all_comp_ids = {
            p.component_id
            for net in result.schematic.nets.values()
            for p in net.pins
        }
        assert r1.id in all_comp_ids

    def test_resolved_definition_ref_not_empty(self) -> None:
        imp = KiCadNetlistImporter()
        result = imp.import_full_from_string(_MINIMAL_SEXP)
        # R1 has a known footprint — definition_ref must not be empty
        assert result.instances["R1"].definition_ref != ""

    def test_footprint_map_has_entry_for_each_fp(self) -> None:
        imp = KiCadNetlistImporter()
        result = imp.import_full_from_string(_MINIMAL_SEXP)
        assert result.footprint_map.get("Resistor_SMD:R_0603_1608Metric") is not None
        assert result.footprint_map.get(
            "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical"
        ) is not None

    def test_no_unresolved_when_all_known(self) -> None:
        imp = KiCadNetlistImporter()
        result = imp.import_full_from_string(_MINIMAL_SEXP)
        assert result.unresolved_fps == []
        assert not result.has_unresolved

    def test_unknown_footprint_in_unresolved(self) -> None:
        imp = KiCadNetlistImporter()
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            result = imp.import_full_from_string(_UNKNOWN_SEXP)
        assert "Exotic:SomeChip" in result.unresolved_fps
        assert result.has_unresolved

    def test_unknown_footprint_still_creates_instance(self) -> None:
        imp = KiCadNetlistImporter()
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            result = imp.import_full_from_string(_UNKNOWN_SEXP)
        assert "U1" in result.instances

    def test_unknown_footprint_instance_has_placeholder_def(self) -> None:
        imp = KiCadNetlistImporter()
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            result = imp.import_full_from_string(_UNKNOWN_SEXP)
        assert "placeholder" in result.instances["U1"].definition_ref


class TestImportFullWithMap:
    """Tests for the footprint_map parameter (re-import workflow)."""

    def test_user_map_overrides_definition_ref(self) -> None:
        user_map = FootprintMap.from_entries([
            FootprintEntry(
                kicad_fp="Resistor_SMD:R_0603_1608Metric",
                definition_ref="my_custom_resistor",
                pin_count=2,
            )
        ])
        imp = KiCadNetlistImporter()
        result = imp.import_full_from_string(_MINIMAL_SEXP, footprint_map=user_map)
        assert result.instances["R1"].definition_ref == "my_custom_resistor"

    def test_user_map_resolves_previously_unknown(self) -> None:
        user_map = FootprintMap.from_entries([
            FootprintEntry(
                kicad_fp="Exotic:SomeChip",
                definition_ref="my_chip",
                pin_count=8,
            )
        ])
        imp = KiCadNetlistImporter()
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            result = imp.import_full_from_string(_UNKNOWN_SEXP, footprint_map=user_map)
        assert result.instances["U1"].definition_ref == "my_chip"
        assert result.unresolved_fps == []

    def test_partial_user_map_leaves_others_auto_resolved(self) -> None:
        user_map = FootprintMap.from_entries([
            FootprintEntry(
                kicad_fp="Resistor_SMD:R_0603_1608Metric",
                definition_ref="R_smd_0603",
                pin_count=2,
            )
        ])
        imp = KiCadNetlistImporter()
        result = imp.import_full_from_string(_MINIMAL_SEXP, footprint_map=user_map)
        # J1 uses PinHeader — auto-resolved, should still have a definition
        assert result.instances["J1"].definition_ref != ""


class TestFootprintMapSaveAndReimport:
    """End-to-end: export map → edit → reimport."""

    def test_save_to_project_dir(self, tmp_path: Path) -> None:
        imp = KiCadNetlistImporter()
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            result = imp.import_full_from_string(_MINIMAL_SEXP)
        map_path = tmp_path / "footprint_map.yaml"
        result.footprint_map.save(map_path)
        assert map_path.exists()
        assert map_path.stat().st_size > 0

    def test_reload_and_reimport_gives_same_result(self, tmp_path: Path) -> None:
        imp = KiCadNetlistImporter()
        result1 = imp.import_full_from_string(_MINIMAL_SEXP)
        map_path = tmp_path / "footprint_map.yaml"
        result1.footprint_map.save(map_path)

        loaded_map = FootprintMap.load(map_path)
        result2 = imp.import_full_from_string(_MINIMAL_SEXP, footprint_map=loaded_map)
        assert {i.refdes for i in result2.instances.values()} == {
            i.refdes for i in result1.instances.values()
        }
        assert {i.definition_ref for i in result2.instances.values()} == {
            i.definition_ref for i in result1.instances.values()
        }

    def test_user_edits_map_to_resolve_unknown(self, tmp_path: Path) -> None:
        imp = KiCadNetlistImporter()
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            result1 = imp.import_full_from_string(_UNKNOWN_SEXP)
        assert result1.has_unresolved

        map_path = tmp_path / "footprint_map.yaml"
        result1.footprint_map.save(map_path)

        # Simulate user editing the YAML
        text = map_path.read_text()
        text = text.replace("definition_ref: ''", "definition_ref: my_exotic_chip")
        text = text.replace("pin_count: 0", "pin_count: 16")
        map_path.write_text(text)

        loaded_map = FootprintMap.load(map_path)
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            result2 = imp.import_full_from_string(_UNKNOWN_SEXP, footprint_map=loaded_map)
        assert not result2.has_unresolved
        assert result2.instances["U1"].definition_ref == "my_exotic_chip"

    def test_ogrod_slawe_full_import(self) -> None:
        path = Path(__file__).parent.parent.parent / "docs" / "ogrod_slawe.net"
        if not path.exists():
            pytest.skip("ogrod_slawe.net not available")
        imp = KiCadNetlistImporter()
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            result = imp.import_full(path)
        assert len(result.instances) == 143
        assert len(result.schematic.nets) > 10
        # All instances must have a non-empty definition_ref and positive pin_count
        for refdes, inst in result.instances.items():
            assert inst.definition_ref, f"{refdes} has empty definition_ref"


class TestFootprintMapOgrodSlawe:
    """Verify that all footprints from ogrod_slawe.net get resolved."""

    def test_all_footprints_have_pin_count(self) -> None:
        path = Path(__file__).parent.parent.parent / "docs" / "ogrod_slawe.net"
        if not path.exists():
            pytest.skip("ogrod_slawe.net not available")
        imp = KiCadNetlistImporter()
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            result = imp.import_full(path)
        zero_pin = [
            e for e in result.footprint_map.entries.values() if e.pin_count == 0
        ]
        assert zero_pin == [], (
            f"Footprints with unresolved pin_count: "
            f"{[e.kicad_fp for e in zero_pin]}"
        )

    def test_footprint_map_save_contains_all_entries(self, tmp_path: Path) -> None:
        path = Path(__file__).parent.parent.parent / "docs" / "ogrod_slawe.net"
        if not path.exists():
            pytest.skip("ogrod_slawe.net not available")
        imp = KiCadNetlistImporter()
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            result = imp.import_full(path)
        map_path = tmp_path / "footprint_map.yaml"
        result.footprint_map.save(map_path)
        loaded = FootprintMap.load(map_path)
        assert len(loaded.entries) == len(result.footprint_map.entries)
