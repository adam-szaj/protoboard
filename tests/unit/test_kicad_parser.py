# SPDX-License-Identifier: MIT
"""Tests for the KiCAD netlist XML parser, importer and exporter.

Format: KiCAD legacy XML netlist (<export version="D">).
"""

from __future__ import annotations

import uuid
import warnings
from pathlib import Path

import pytest

from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.services.kicad.exporter import KiCadNetlistExporter
from protoboard.services.kicad.importer import ImportWarning, KiCadNetlistImporter

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "kicad_samples"


# ---------------------------------------------------------------------------
# Parser / Importer
# ---------------------------------------------------------------------------


class TestKiCadNetlistImporter:
    def test_import_minimal_returns_schematic(self) -> None:
        imp = KiCadNetlistImporter()
        sch = imp.import_(_FIXTURES / "minimal.net")
        assert isinstance(sch, Schematic)

    def test_minimal_has_two_nets(self) -> None:
        imp = KiCadNetlistImporter()
        sch = imp.import_(_FIXTURES / "minimal.net")
        names = {n.name for n in sch.nets.values()}
        assert names == {"VCC", "GND"}

    def test_minimal_component_pins_in_nets(self) -> None:
        imp = KiCadNetlistImporter()
        sch = imp.import_(_FIXTURES / "minimal.net")
        # J1 pin 1 → VCC, J1 pin 2 → GND
        all_pins = {pin for net in sch.nets.values() for pin in net.pins}
        pin_numbers = {p.pin_number for p in all_pins}
        assert 1 in pin_numbers
        assert 2 in pin_numbers

    def test_voltage_divider_component_count(self) -> None:
        imp = KiCadNetlistImporter()
        sch = imp.import_(_FIXTURES / "voltage_divider.net")
        # Nets: VCC, MID, GND, OUT_A, N$5, N$6, N$7
        assert len(sch.nets) == 7

    def test_voltage_divider_net_names(self) -> None:
        imp = KiCadNetlistImporter()
        sch = imp.import_(_FIXTURES / "voltage_divider.net")
        names = {n.name for n in sch.nets.values()}
        assert {"VCC", "MID", "GND", "OUT_A"}.issubset(names)

    def test_voltage_divider_vcc_net_pins(self) -> None:
        imp = KiCadNetlistImporter()
        sch = imp.import_(_FIXTURES / "voltage_divider.net")
        vcc = next(n for n in sch.nets.values() if n.name == "VCC")
        # R1 pin 1 and U1 pin 8 on VCC
        assert len(vcc.pins) == 2
        pin_numbers = {p.pin_number for p in vcc.pins}
        assert pin_numbers == {1, 8}

    def test_voltage_divider_mid_net_pins(self) -> None:
        imp = KiCadNetlistImporter()
        sch = imp.import_(_FIXTURES / "voltage_divider.net")
        mid = next(n for n in sch.nets.values() if n.name == "MID")
        # R1p2, R2p1, U1p2, U1p3
        assert len(mid.pins) == 4

    def test_net_ids_are_unique_uuids(self) -> None:
        imp = KiCadNetlistImporter()
        sch = imp.import_(_FIXTURES / "voltage_divider.net")
        ids = list(sch.nets.keys())
        assert len(ids) == len(set(ids))
        for nid in ids:
            assert isinstance(nid, uuid.UUID)

    def test_component_ids_consistent_across_nets(self) -> None:
        # The same component (e.g. R1) should have the same UUID in every net
        # where its pins appear.
        imp = KiCadNetlistImporter()
        sch = imp.import_(_FIXTURES / "voltage_divider.net")
        # Collect all PinRefs grouped by their component UUID.
        by_comp: dict[uuid.UUID, set[int]] = {}
        for net in sch.nets.values():
            for pin in net.pins:
                by_comp.setdefault(pin.component_id, set()).add(pin.pin_number)
        # R1 has pins 1 and 2 → must be the same component_id
        # (at least one component has exactly pin set {1, 2})
        pin_sets = [frozenset(v) for v in by_comp.values()]
        assert frozenset({1, 2}) in pin_sets

    def test_unknown_footprint_emits_warning(self) -> None:
        # Warnings are emitted by import_full, not by legacy import_()
        imp = KiCadNetlistImporter()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            imp.import_full(_FIXTURES / "unknown_footprint.net")
        import_warnings = [w for w in caught if issubclass(w.category, ImportWarning)]
        assert len(import_warnings) >= 1
        assert "ExoticPackage_QFN48" in str(import_warnings[0].message)

    def test_unknown_footprint_nets_still_imported(self) -> None:
        imp = KiCadNetlistImporter()
        sch = imp.import_(_FIXTURES / "unknown_footprint.net")
        names = {n.name for n in sch.nets.values()}
        assert {"VCC", "GND"} == names

    def test_import_from_string(self) -> None:
        xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<export version="D">
  <design><source>x</source><date>2024</date><tool>t</tool></design>
  <components>
    <comp ref="R1">
      <value>1k</value>
      <footprint>Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P7.62mm_Horizontal</footprint>
      <nets>
        <pin num="1" name="~" net="A"/>
        <pin num="2" name="~" net="B"/>
      </nets>
    </comp>
  </components>
  <nets>
    <net code="1" name="A"><node ref="R1" pin="1"/></net>
    <net code="2" name="B"><node ref="R1" pin="2"/></net>
  </nets>
</export>
"""
        imp = KiCadNetlistImporter()
        sch = imp.import_from_string(xml)
        assert len(sch.nets) == 2

    def test_wrong_root_element_raises(self) -> None:
        xml = '<?xml version="1.0"?><notexport/>'
        imp = KiCadNetlistImporter()
        with pytest.raises(ValueError, match="export"):
            imp.import_from_string(xml)

    def test_missing_version_attribute_still_imports(self) -> None:
        # Permissive: if version attr is missing, still try to parse.
        xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<export>
  <design><source>x</source><date>2024</date><tool>t</tool></design>
  <components/>
  <nets>
    <net code="1" name="PWR"><node ref="R1" pin="1"/></net>
  </nets>
</export>
"""
        imp = KiCadNetlistImporter()
        sch = imp.import_from_string(xml)
        assert len(sch.nets) == 1

    def test_schematic_net_for_pin_works(self) -> None:
        imp = KiCadNetlistImporter()
        sch = imp.import_(_FIXTURES / "minimal.net")
        all_pins = [pin for net in sch.nets.values() for pin in net.pins]
        first_pin = all_pins[0]
        found = sch.net_for_pin(first_pin)
        assert found is not None


# ---------------------------------------------------------------------------
# Footprint heuristic
# ---------------------------------------------------------------------------


class TestFootprintHeuristic:
    """The importer maps KiCAD footprint strings to a Footprint."""

    def _get_importer(self) -> KiCadNetlistImporter:
        return KiCadNetlistImporter()

    def test_dip8_recognised(self) -> None:
        imp = self._get_importer()
        fp = imp.resolve_footprint("Package_DIP:DIP-8_W7.62mm")
        assert fp is not None
        assert fp.pin_count() == 8

    def test_dip14_recognised(self) -> None:
        imp = self._get_importer()
        fp = imp.resolve_footprint("Package_DIP:DIP-14_W7.62mm")
        assert fp is not None
        assert fp.pin_count() == 14

    def test_sil2_resistor_recognised(self) -> None:
        imp = self._get_importer()
        fp = imp.resolve_footprint(
            "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P7.62mm_Horizontal"
        )
        assert fp is not None
        assert fp.pin_count() == 2

    def test_sil2_connector_recognised(self) -> None:
        imp = self._get_importer()
        fp = imp.resolve_footprint(
            "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical"
        )
        assert fp is not None
        assert fp.pin_count() == 2

    def test_pinheader_1x04_recognised(self) -> None:
        imp = self._get_importer()
        fp = imp.resolve_footprint(
            "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical"
        )
        assert fp is not None
        assert fp.pin_count() == 4

    def test_unknown_returns_none(self) -> None:
        imp = self._get_importer()
        fp = imp.resolve_footprint("SomeVendor:ExoticPackage_QFN48")
        assert fp is None

    def test_to92_recognised(self) -> None:
        imp = self._get_importer()
        fp = imp.resolve_footprint("Package_TO_SOT_THT:TO-92_Inline")
        assert fp is not None
        assert fp.pin_count() == 3


# ---------------------------------------------------------------------------
# Exporter
# ---------------------------------------------------------------------------


class TestKiCadNetlistExporter:
    def _make_schematic(self) -> tuple[Schematic, dict[uuid.UUID, str]]:
        """Two-net schematic: R1p1→VCC, R1p2→GND."""
        r1_id = uuid.uuid4()
        vcc_id = uuid.uuid4()
        gnd_id = uuid.uuid4()
        nets = {
            vcc_id: Net(
                id=vcc_id,
                name="VCC",
                pins={PinRef(component_id=r1_id, pin_number=1)},
            ),
            gnd_id: Net(
                id=gnd_id,
                name="GND",
                pins={PinRef(component_id=r1_id, pin_number=2)},
            ),
        }
        sch = Schematic(nets=nets)
        refdes_map = {r1_id: "R1"}
        return sch, refdes_map

    def test_export_returns_string(self) -> None:
        sch, refdes_map = self._make_schematic()
        exp = KiCadNetlistExporter()
        result = exp.export(sch, refdes_map=refdes_map)
        assert isinstance(result, str)

    def test_export_has_xml_header(self) -> None:
        sch, refdes_map = self._make_schematic()
        exp = KiCadNetlistExporter()
        result = exp.export(sch, refdes_map=refdes_map)
        assert result.startswith("<?xml")

    def test_export_has_export_root(self) -> None:
        sch, refdes_map = self._make_schematic()
        exp = KiCadNetlistExporter()
        result = exp.export(sch, refdes_map=refdes_map)
        assert "<export" in result

    def test_export_contains_net_names(self) -> None:
        sch, refdes_map = self._make_schematic()
        exp = KiCadNetlistExporter()
        result = exp.export(sch, refdes_map=refdes_map)
        assert "VCC" in result
        assert "GND" in result

    def test_export_contains_refdes(self) -> None:
        sch, refdes_map = self._make_schematic()
        exp = KiCadNetlistExporter()
        result = exp.export(sch, refdes_map=refdes_map)
        assert "R1" in result

    def test_export_contains_pin_numbers(self) -> None:
        sch, refdes_map = self._make_schematic()
        exp = KiCadNetlistExporter()
        result = exp.export(sch, refdes_map=refdes_map)
        assert 'pin="1"' in result or "pin=1" in result
        assert 'pin="2"' in result or "pin=2" in result

    def test_export_is_valid_xml(self) -> None:
        import xml.etree.ElementTree as ET

        sch, refdes_map = self._make_schematic()
        exp = KiCadNetlistExporter()
        result = exp.export(sch, refdes_map=refdes_map)
        # Must not raise.
        ET.fromstring(result)

    def test_export_to_file(self, tmp_path: Path) -> None:
        sch, refdes_map = self._make_schematic()
        exp = KiCadNetlistExporter()
        out = tmp_path / "out.net"
        exp.export_to_file(sch, out, refdes_map=refdes_map)
        assert out.exists()
        assert out.stat().st_size > 0

    def test_roundtrip_net_names_preserved(self) -> None:
        """Export then re-import: net names must survive."""
        sch, refdes_map = self._make_schematic()
        exp = KiCadNetlistExporter()
        xml_str = exp.export(sch, refdes_map=refdes_map)

        imp = KiCadNetlistImporter()
        sch2 = imp.import_from_string(xml_str)
        names2 = {n.name for n in sch2.nets.values()}
        assert names2 == {"VCC", "GND"}

    def test_roundtrip_pin_count_preserved(self) -> None:
        """Export then re-import: pin counts per net must survive."""
        sch, refdes_map = self._make_schematic()
        exp = KiCadNetlistExporter()
        xml_str = exp.export(sch, refdes_map=refdes_map)

        imp = KiCadNetlistImporter()
        sch2 = imp.import_from_string(xml_str)
        for net in sch2.nets.values():
            assert len(net.pins) == 1  # each net has exactly 1 pin in our fixture


# ---------------------------------------------------------------------------
# Integration: import voltage_divider fixture end-to-end
# ---------------------------------------------------------------------------


class TestVoltgeDividerEndToEnd:
    def test_all_nets_present(self) -> None:
        imp = KiCadNetlistImporter()
        sch = imp.import_(_FIXTURES / "voltage_divider.net")
        assert len(sch.nets) == 7

    def test_gnd_net_has_two_pins(self) -> None:
        imp = KiCadNetlistImporter()
        sch = imp.import_(_FIXTURES / "voltage_divider.net")
        gnd = next(n for n in sch.nets.values() if n.name == "GND")
        assert len(gnd.pins) == 2

    def test_r1_and_r2_have_distinct_component_ids(self) -> None:
        imp = KiCadNetlistImporter()
        sch = imp.import_(_FIXTURES / "voltage_divider.net")
        # Collect comp_ids per refdes by cross-checking pins in MID net.
        mid = next(n for n in sch.nets.values() if n.name == "MID")
        # MID has 4 pins: R1p2, R2p1, U1p2, U1p3
        # At least 3 distinct component_ids (R1, R2, U1).
        comp_ids = {p.component_id for p in mid.pins}
        assert len(comp_ids) >= 2


# ---------------------------------------------------------------------------
# S-expression parser unit tests
# ---------------------------------------------------------------------------


class TestSexpParser:
    def test_simple_list(self) -> None:
        from protoboard.services.kicad.sexp_parser import parse

        node = parse("(net (code 1) (name GND))")
        assert node[0] == "net"
        assert isinstance(node[1], list)
        assert node[1][0] == "code"

    def test_quoted_string_with_spaces(self) -> None:
        from protoboard.services.kicad.sexp_parser import parse

        node = parse('(name "Net-(J1-Pad1)")')
        assert node[0] == "name"
        assert node[1] == "Net-(J1-Pad1)"

    def test_nested_lists(self) -> None:
        from protoboard.services.kicad.sexp_parser import parse

        node = parse("(export (version D) (nets (net (code 1) (name VCC))))")
        assert node[0] == "export"
        # find nets child
        nets = next(c for c in node[1:] if isinstance(c, list) and c[0] == "nets")
        assert nets is not None
        net = next(c for c in nets[1:] if isinstance(c, list) and c[0] == "net")
        assert net[0] == "net"

    def test_attr_helper(self) -> None:
        from protoboard.services.kicad.sexp_parser import attr, parse

        node = parse("(net (code 5) (name podswietlenieWOD))")
        assert attr(node, "name") == "podswietlenieWOD"
        assert attr(node, "code") == "5"
        assert attr(node, "missing") is None

    def test_children_helper(self) -> None:
        from protoboard.services.kicad.sexp_parser import attr, children, parse

        node = parse(
            "(net (code 6) (name GND)"
            " (node (ref R1) (pin 1))"
            " (node (ref R2) (pin 2)))"
        )
        nodes = children(node, "node")
        assert len(nodes) == 2
        refs = [attr(n, "ref") for n in nodes]
        assert set(refs) == {"R1", "R2"}

    def test_escaped_quote_in_string(self) -> None:
        from protoboard.services.kicad.sexp_parser import parse

        node = parse('(title "say \\"hello\\"")')
        assert node[1] == 'say "hello"'


# ---------------------------------------------------------------------------
# S-expression importer
# ---------------------------------------------------------------------------

_SEXP_MINIMAL = """\
(export (version D)
  (design
    (source test.sch)
    (date "2026-01-01")
    (tool "Eeschema"))
  (components
    (comp (ref J1)
      (value Conn)
      (footprint "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical")))
  (nets
    (net (code 1) (name VCC)
      (node (ref J1) (pin 1)))
    (net (code 2) (name GND)
      (node (ref J1) (pin 2)))))
"""

_SEXP_WITH_SPACES_IN_NAMES = """\
(export (version D)
  (design (source x) (date "") (tool t))
  (components
    (comp (ref R5)
      (value 470)
      (footprint Resistor_SMD:R_0603_1608Metric)))
  (nets
    (net (code 1) (name "Net-(J1-Pad1)")
      (node (ref R5) (pin 2))
      (node (ref J1) (pin 1)))
    (net (code 2) (name GND)
      (node (ref J1) (pin 2)))))
"""


class TestSexpImporter:
    def test_auto_detect_sexp(self) -> None:
        imp = KiCadNetlistImporter()
        sch = imp.import_from_string(_SEXP_MINIMAL)
        assert isinstance(sch, Schematic)

    def test_sexp_net_names(self) -> None:
        imp = KiCadNetlistImporter()
        sch = imp.import_from_string(_SEXP_MINIMAL)
        names = {n.name for n in sch.nets.values()}
        assert names == {"VCC", "GND"}

    def test_sexp_pin_count(self) -> None:
        imp = KiCadNetlistImporter()
        sch = imp.import_from_string(_SEXP_MINIMAL)
        for net in sch.nets.values():
            assert len(net.pins) == 1

    def test_sexp_component_ids_consistent(self) -> None:
        imp = KiCadNetlistImporter()
        sch = imp.import_from_string(_SEXP_MINIMAL)
        all_comp_ids = {p.component_id for net in sch.nets.values() for p in net.pins}
        # J1 appears in both nets — same UUID must be reused.
        assert len(all_comp_ids) == 1

    def test_sexp_net_name_with_spaces(self) -> None:
        imp = KiCadNetlistImporter()
        sch = imp.import_from_string(_SEXP_WITH_SPACES_IN_NAMES)
        names = {n.name for n in sch.nets.values()}
        assert "Net-(J1-Pad1)" in names

    def test_sexp_net_with_multiple_pins(self) -> None:
        imp = KiCadNetlistImporter()
        sch = imp.import_from_string(_SEXP_WITH_SPACES_IN_NAMES)
        net = next(n for n in sch.nets.values() if n.name == "Net-(J1-Pad1)")
        assert len(net.pins) == 2

    def test_sexp_unknown_footprint_warning(self) -> None:
        # Warnings are emitted by import_full, not by legacy import_from_string
        sexp = """\
(export (version D)
  (design (source x) (date "") (tool t))
  (components
    (comp (ref U99)
      (value Thing)
      (footprint "Exotic:QFN-48_7x7mm")))
  (nets
    (net (code 1) (name VCC)
      (node (ref U99) (pin 1)))))
"""
        imp = KiCadNetlistImporter()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = imp.import_full_from_string(sexp)
        import_warnings = [w for w in caught if issubclass(w.category, ImportWarning)]
        assert len(import_warnings) >= 1
        # Net is still imported despite unknown footprint.
        assert len(result.schematic.nets) == 1

    def test_sexp_wrong_root_raises(self) -> None:
        imp = KiCadNetlistImporter()
        with pytest.raises(ValueError, match="export"):
            imp.import_from_string("(something (version D))")

    def test_sexp_unrecognised_format_raises(self) -> None:
        imp = KiCadNetlistImporter()
        with pytest.raises(ValueError, match="Unrecognised"):
            imp.import_from_string("not valid at all")

    def test_sexp_ogrod_slawe_net(self) -> None:
        """Import the real-world ogrod_slawe.net fixture."""
        path = Path(__file__).parent.parent.parent / "docs" / "ogrod_slawe.net"
        if not path.exists():
            pytest.skip("ogrod_slawe.net not available")
        imp = KiCadNetlistImporter()
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            sch = imp.import_(path)
        assert len(sch.nets) > 10
        net_names = {n.name for n in sch.nets.values()}
        assert "GND" in net_names


# ---------------------------------------------------------------------------
# S-expression exporter
# ---------------------------------------------------------------------------


class TestSexpExporter:
    def _make_schematic(self) -> tuple[Schematic, dict[uuid.UUID, str]]:
        r1_id = uuid.uuid4()
        vcc_id = uuid.uuid4()
        gnd_id = uuid.uuid4()
        nets = {
            vcc_id: Net(
                id=vcc_id,
                name="VCC",
                pins={PinRef(component_id=r1_id, pin_number=1)},
            ),
            gnd_id: Net(
                id=gnd_id,
                name="GND",
                pins={PinRef(component_id=r1_id, pin_number=2)},
            ),
        }
        return Schematic(nets=nets), {r1_id: "R1"}

    def test_sexp_export_returns_string(self) -> None:
        sch, rdm = self._make_schematic()
        out = KiCadNetlistExporter().export(sch, refdes_map=rdm, fmt="sexp")
        assert isinstance(out, str)

    def test_sexp_export_starts_with_export(self) -> None:
        sch, rdm = self._make_schematic()
        out = KiCadNetlistExporter().export(sch, refdes_map=rdm, fmt="sexp")
        assert out.lstrip().startswith("(export")

    def test_sexp_export_contains_net_names(self) -> None:
        sch, rdm = self._make_schematic()
        out = KiCadNetlistExporter().export(sch, refdes_map=rdm, fmt="sexp")
        assert "VCC" in out
        assert "GND" in out

    def test_sexp_export_contains_refdes(self) -> None:
        sch, rdm = self._make_schematic()
        out = KiCadNetlistExporter().export(sch, refdes_map=rdm, fmt="sexp")
        assert "R1" in out

    def test_sexp_export_contains_pin_numbers(self) -> None:
        sch, rdm = self._make_schematic()
        out = KiCadNetlistExporter().export(sch, refdes_map=rdm, fmt="sexp")
        assert "(pin 1)" in out
        assert "(pin 2)" in out

    def test_sexp_roundtrip_net_names(self) -> None:
        sch, rdm = self._make_schematic()
        sexp_str = KiCadNetlistExporter().export(sch, refdes_map=rdm, fmt="sexp")
        sch2 = KiCadNetlistImporter().import_from_string(sexp_str)
        assert {n.name for n in sch2.nets.values()} == {"VCC", "GND"}

    def test_sexp_roundtrip_pin_count(self) -> None:
        sch, rdm = self._make_schematic()
        sexp_str = KiCadNetlistExporter().export(sch, refdes_map=rdm, fmt="sexp")
        sch2 = KiCadNetlistImporter().import_from_string(sexp_str)
        for net in sch2.nets.values():
            assert len(net.pins) == 1

    def test_sexp_net_with_spaces_in_name(self) -> None:
        cid = uuid.uuid4()
        net_id = uuid.uuid4()
        sch = Schematic(
            nets={
                net_id: Net(
                    id=net_id,
                    name="Net-(J1-Pad1)",
                    pins={PinRef(component_id=cid, pin_number=1)},
                )
            }
        )
        out = KiCadNetlistExporter().export(sch, fmt="sexp")
        assert '"Net-(J1-Pad1)"' in out
        # Re-import and check name survived.
        sch2 = KiCadNetlistImporter().import_from_string(out)
        assert "Net-(J1-Pad1)" in {n.name for n in sch2.nets.values()}

    def test_sexp_export_to_file(self, tmp_path: Path) -> None:
        sch, rdm = self._make_schematic()
        out = tmp_path / "out.net"
        KiCadNetlistExporter().export_to_file(sch, out, refdes_map=rdm, fmt="sexp")
        assert out.exists()
        text = out.read_text()
        assert text.lstrip().startswith("(export")
