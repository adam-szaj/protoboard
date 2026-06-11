# SPDX-License-Identifier: MIT
"""Tests for the component library loader.

The loader reads YAML files from one or more directories and provides
lookup of `ComponentDefinition` by name. When the same name appears in
multiple directories, the *first* directory wins (user overrides
built-in definitions).
"""

from pathlib import Path

import pytest

from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import (
    DualRow,
    SingleRow,
)
from protoboard.errors import SerializationError
from protoboard.services.library.loader import ComponentLibrary, load_library

FIXTURES = Path(__file__).parent.parent / "fixtures" / "library"


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
        assert isinstance(defn.footprint, DualRow)
        assert defn.pin_count == 8
        assert defn.pin_name(1) == "OUT_A"
        assert defn.pin_name(8) == "VCC"

    def test_get_resistor(self) -> None:
        from protoboard.domain.components.footprints import TwoPin
        lib = load_library([FIXTURES])
        defn = lib.get("R")
        # Two-pin components now use TwoPin (loader redirects SIL pins=2).
        assert isinstance(defn.footprint, TwoPin)
        assert defn.pin_count == 2
        # Pin names not declared → defaults.
        assert defn.pin_name(1) == "P1"
        assert defn.pin_name(2) == "P2"

    def test_get_to92_transistor(self) -> None:
        lib = load_library([FIXTURES])
        defn = lib.get("BC547")
        assert isinstance(defn.footprint, SingleRow)
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
    footprint:
      type: DIP
      pins: 8
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
    footprint:
      type: SIL
      pins: 4
    pin_names: {}
""")
        lib = load_library([FIXTURES, extra_dir])
        assert "NEW_PART" in lib
        assert "LM358" in lib  # still present from FIXTURES


class TestLibraryErrors:
    def test_invalid_yaml_raises(self, tmp_path: Path) -> None:
        bad_dir = tmp_path / "bad"
        bad_dir.mkdir()
        (bad_dir / "broken.yaml").write_text("this is: : not valid")
        with pytest.raises(SerializationError):
            load_library([bad_dir])

    def test_unknown_footprint_type_raises(self, tmp_path: Path) -> None:
        bad_dir = tmp_path / "bad"
        bad_dir.mkdir()
        (bad_dir / "x.yaml").write_text("""
components:
  - name: X
    footprint:
      type: NotAFootprint
      pins: 4
    pin_names: {}
""")
        with pytest.raises(SerializationError):
            load_library([bad_dir])

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

    def test_dip_odd_pins_raises(self, tmp_path: Path) -> None:
        bad_dir = tmp_path / "bad"
        bad_dir.mkdir()
        (bad_dir / "x.yaml").write_text("""
components:
  - name: X
    footprint:
      type: DIP
      pins: 7
    pin_names: {}
""")
        with pytest.raises(SerializationError):
            load_library([bad_dir])

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
        lib = self._make_lib(tmp_path, """
components:
  - name: _BASE
    component_class: transistor_npn
    footprint: {type: TO92}
    pin_names: {1: C, 2: B, 3: E}
  - name: MyNPN
    extends: _BASE
    description: "custom npn"
""")
        defn = lib.get("MyNPN")
        from protoboard.domain.components.definition import ComponentClass
        from protoboard.domain.components.footprints import SingleRow
        assert isinstance(defn.footprint, SingleRow)
        assert defn.pin_count == 3
        assert defn.pin_name(1) == "C"
        assert defn.component_class == ComponentClass.TRANSISTOR_NPN
        assert defn.description == "custom npn"

    def test_template_not_placeable(self, tmp_path: Path) -> None:
        lib = self._make_lib(tmp_path, """
components:
  - name: _TMPL
    component_class: transistor_npn
    footprint: {type: TO92}
    pin_names: {1: C, 2: B, 3: E}
  - name: RealPart
    extends: _TMPL
""")
        assert "_TMPL" not in lib
        assert "RealPart" in lib

    def test_child_overrides_pin_names(self, tmp_path: Path) -> None:
        lib = self._make_lib(tmp_path, """
components:
  - name: _BASE
    component_class: transistor_npn
    footprint: {type: TO92}
    pin_names: {1: C, 2: B, 3: E}
  - name: AltPinout
    extends: _BASE
    pin_names: {1: E, 2: B, 3: C}
""")
        defn = lib.get("AltPinout")
        assert defn.pin_name(1) == "E"
        assert defn.pin_name(3) == "C"

    def test_multilevel_extends(self, tmp_path: Path) -> None:
        lib = self._make_lib(tmp_path, """
components:
  - name: _NPN
    component_class: transistor_npn
    footprint: {type: TO92}
    pin_names: {}
  - name: _NPN_CBE
    extends: _NPN
    pin_names: {1: C, 2: B, 3: E}
  - name: BC547
    extends: _NPN_CBE
    description: "NPN 45V"
""")
        defn = lib.get("BC547")
        assert defn.pin_name(2) == "B"
        assert defn.description == "NPN 45V"

    def test_circular_extends_raises(self, tmp_path: Path) -> None:
        with pytest.raises(SerializationError, match="circular"):
            self._make_lib(tmp_path, """
components:
  - name: A
    extends: B
    footprint: {type: TO92}
    pin_names: {}
  - name: B
    extends: A
    footprint: {type: TO92}
    pin_names: {}
""")

    def test_unknown_extends_target_raises(self, tmp_path: Path) -> None:
        with pytest.raises(SerializationError, match="unknown template"):
            self._make_lib(tmp_path, """
components:
  - name: MyPart
    extends: _NONEXISTENT
    footprint: {type: TO92}
    pin_names: {}
""")

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
    def _make_lib(self, tmp_path: Path, yaml_text: str) -> object:
        (tmp_path / "parts.yaml").write_text(yaml_text)
        return load_library([tmp_path])

    def test_variants_parsed(self, tmp_path: Path) -> None:
        lib = self._make_lib(tmp_path, """
components:
  - name: Header4
    component_class: connector
    footprint: {type: SIL, pins: 4}
    pin_names: {1: "1", 2: "2", 3: "3", 4: "4"}
    variants:
      - id: i2c
        label: "I2C (VCC/GND/SDA/SCL)"
        pin_names: {1: VCC, 2: GND, 3: SDA, 4: SCL}
      - id: uart
        label: "UART"
        pin_names: {1: VCC, 2: GND, 3: TX, 4: RX}
""")
        defn = lib.get("Header4")
        assert len(defn.variants) == 2
        assert defn.variants[0].id == "i2c"
        assert defn.variants[0].label == "I2C (VCC/GND/SDA/SCL)"
        assert defn.variants[1].id == "uart"

    def test_no_variants_default_empty(self, tmp_path: Path) -> None:
        lib = self._make_lib(tmp_path, """
components:
  - name: Header4
    component_class: connector
    footprint: {type: SIL, pins: 4}
    pin_names: {1: "1", 2: "2", 3: "3", 4: "4"}
""")
        defn = lib.get("Header4")
        assert defn.variants == ()

    def test_effective_pin_names_default(self, tmp_path: Path) -> None:
        lib = self._make_lib(tmp_path, """
components:
  - name: Header4
    component_class: connector
    footprint: {type: SIL, pins: 4}
    pin_names: {1: "1", 2: "2", 3: "3", 4: "4"}
    variants:
      - id: i2c
        label: "I2C"
        pin_names: {1: VCC, 2: GND, 3: SDA, 4: SCL}
""")
        defn = lib.get("Header4")
        assert defn.effective_pin_names(None) == {1: "1", 2: "2", 3: "3", 4: "4"}

    def test_effective_pin_names_with_variant(self, tmp_path: Path) -> None:
        lib = self._make_lib(tmp_path, """
components:
  - name: Header4
    component_class: connector
    footprint: {type: SIL, pins: 4}
    pin_names: {1: "1", 2: "2", 3: "3", 4: "4"}
    variants:
      - id: i2c
        label: "I2C"
        pin_names: {1: VCC, 2: GND, 3: SDA, 4: SCL}
""")
        defn = lib.get("Header4")
        names = defn.effective_pin_names("i2c")
        assert names == {1: "VCC", 2: "GND", 3: "SDA", 4: "SCL"}

    def test_effective_pin_names_partial_override(self, tmp_path: Path) -> None:
        lib = self._make_lib(tmp_path, """
components:
  - name: Header4
    component_class: connector
    footprint: {type: SIL, pins: 4}
    pin_names: {1: "1", 2: "2", 3: "3", 4: "4"}
    variants:
      - id: partial
        label: "Partial"
        pin_names: {3: SDA, 4: SCL}
""")
        defn = lib.get("Header4")
        names = defn.effective_pin_names("partial")
        assert names[1] == "1"
        assert names[2] == "2"
        assert names[3] == "SDA"
        assert names[4] == "SCL"

    def test_effective_pin_names_unknown_variant_fallback(self, tmp_path: Path) -> None:
        lib = self._make_lib(tmp_path, """
components:
  - name: Header4
    component_class: connector
    footprint: {type: SIL, pins: 4}
    pin_names: {1: "1", 2: "2", 3: "3", 4: "4"}
    variants:
      - id: i2c
        label: "I2C"
        pin_names: {1: VCC, 2: GND, 3: SDA, 4: SCL}
""")
        defn = lib.get("Header4")
        names = defn.effective_pin_names("nonexistent")
        assert names == {1: "1", 2: "2", 3: "3", 4: "4"}

    def test_connectors_yaml_has_variants(self) -> None:
        lib = load_library([FIXTURES.parent.parent.parent / "library"])
        defn = lib.get("PinHeader_1x04")
        assert len(defn.variants) >= 4
        ids = [v.id for v in defn.variants]
        assert "i2c_vcc_gnd_sda_scl" in ids
        assert "uart_vcc_gnd_tx_rx" in ids
