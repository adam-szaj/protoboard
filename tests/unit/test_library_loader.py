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
        # Three files * (2 + 1 + 1) = 4 components total.
        names = sorted(lib.names())
        assert names == ["BC547", "C", "LM358", "R"]

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
        lib = load_library([FIXTURES])
        defn = lib.get("R")
        assert isinstance(defn.footprint, SingleRow)
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
