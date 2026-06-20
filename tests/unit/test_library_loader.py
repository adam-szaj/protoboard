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


class TestRigidPackageFamilyRef:
    """A rigid package_family_ref (SMD) drives the footprint and drops bend;
    a bendable axial ref leaves the footprint and bend intact."""

    _LIB = """\
components:
  - name: R_SMD
    component_class: resistor
    footprint: {type: TwoPin}
    pin_names: {}
    package_family_ref: "SMD-0805/typ"
    bendable_pins:
      2: {min_reach: 1, max_reach: 23, default: {x: 0, y: 4}}
  - name: R_AXIAL
    component_class: resistor
    footprint: {type: TwoPin}
    pin_names: {}
    package_family_ref: "DIN-0207/typ"
    bendable_pins:
      2: {min_reach: 1, max_reach: 23, default: {x: 0, y: 4}}
"""

    def _lib(self, tmp_path: Path) -> ComponentLibrary:
        from protoboard.services.library.package_family_loader import (
            load_full_package_catalogue,
        )
        (tmp_path / "p.yaml").write_text(self._LIB)
        return load_library([tmp_path], load_full_package_catalogue())

    def test_rigid_smd_ref_overrides_footprint_and_clears_bend(
        self, tmp_path: Path,
    ) -> None:
        from protoboard.domain.components.package import PackageFootprint
        defn = self._lib(tmp_path).get("R_SMD")
        # The SMD body fixes the geometry: a PackageFootprint with rigid
        # 1-hole pin spacing, and no bendable leads.
        assert isinstance(defn.footprint, PackageFootprint)
        xs = sorted({round(p.x, 1) for p in defn.footprint.pin_positions()})
        assert xs == [0.0, 1.0]
        assert defn.bendable_pins == {}

    def test_bendable_axial_ref_keeps_footprint_and_bend(
        self, tmp_path: Path,
    ) -> None:
        from protoboard.domain.components.footprints import TwoPin
        defn = self._lib(tmp_path).get("R_AXIAL")
        # An axial cylinder is not rigid (it bends), so the footprint and the
        # bendable leads survive.
        assert isinstance(defn.footprint, TwoPin)
        assert defn.bendable_pins  # kept

    def test_no_catalogue_leaves_footprint_unchanged(
        self, tmp_path: Path,
    ) -> None:
        # Without a catalogue the loader can't resolve the ref → legacy
        # footprint-only behaviour (no rigid override).
        from protoboard.domain.components.footprints import TwoPin
        (tmp_path / "p.yaml").write_text(self._LIB)
        defn = load_library([tmp_path]).get("R_SMD")
        assert isinstance(defn.footprint, TwoPin)
        assert defn.bendable_pins  # kept


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


class TestAliasInheritance:
    """An alias is the parent definition under a different name — it inherits
    every field, including package_family_ref / default_value."""

    def test_alias_inherits_package_family_ref_and_value(
        self, tmp_path: Path,
    ) -> None:
        d = tmp_path / "lib"
        d.mkdir()
        (d / "parts.yaml").write_text("""
components:
  - name: R
    description: "Resistor"
    component_class: resistor
    footprint: {type: TwoPin}
    pin_names: {}
    package_family_ref: "DIN-0207/typ"
    default_value: "?"
    aliases:
      - R_Axial_DIN0207
      - R_0603_1608Metric
""")
        lib = load_library([d])
        parent = lib.get("R")
        for alias_name in ("R_Axial_DIN0207", "R_0603_1608Metric"):
            a = lib.get(alias_name)
            assert a.package_family_ref == parent.package_family_ref == "DIN-0207/typ"
            assert a.default_value == parent.default_value == "?"
            assert a.component_class == parent.component_class


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


class TestPackageInLibrary:
    """Components may declare a parametric ``package:`` instead of footprint."""

    def _write(self, tmp_path: Path, text: str) -> Path:
        d = tmp_path / "lib"
        d.mkdir()
        (d / "parts.yaml").write_text(text)
        return d

    def test_component_with_package_loads(self, tmp_path: Path) -> None:
        d = self._write(tmp_path, """
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
""")
        lib = load_library([d])
        defn = lib.get("C_POL")
        assert defn.package is not None          # package attached
        assert defn.pin_count == 2               # footprint derived from package
        # footprint pin positions come from the package
        assert defn.footprint.pin_positions()[0].x == 0.0

    def test_package_component_needs_no_footprint(self, tmp_path: Path) -> None:
        d = self._write(tmp_path, """
components:
  - name: R
    component_class: resistor
    pin_names: {}
    package:
      extends: cylinder_axial
      dims: {diameter: 2.5, length: 6.3}
      leads: {lead_r: 0.27, lead_length: 32.0}
""")
        lib = load_library([d])
        assert lib.get("R").pin_count == 2       # no footprint: key needed

    def test_malformed_package_skips_component_not_crash(self, tmp_path: Path) -> None:
        # Missing required dims (null) → package won't build → component skipped,
        # but the rest of the library still loads.
        d = self._write(tmp_path, """
components:
  - name: BadPart
    component_class: resistor
    pin_names: {}
    package:
      extends: cylinder_axial
      leads: {lead_r: 0.27, lead_length: 32.0}
  - name: GoodPart
    footprint: {type: SIL, pins: 2}
    pin_names: {}
""")
        lib = load_library([d])
        assert "BadPart" not in lib              # malformed package → skipped
        assert "GoodPart" in lib                 # rest loads fine
