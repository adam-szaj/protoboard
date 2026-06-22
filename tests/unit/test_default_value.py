# SPDX-License-Identifier: MIT
"""``ComponentDefinition.default_value`` — initial instance value.

Convention:

* Concrete parts (BC547, 1N4148, NE555, …) leave ``default_value``
  empty and the loader falls back to ``defn.name``.  The overlay label
  composes ``"Q1 BC547"``.
* Generic templates (R, C, L, F, Pot, R_Net_4, …) carry
  ``default_value: "?"`` so a freshly placed instance prompts the user
  to fill in the actual value: ``"R23 ?"``.

PlaceTool wires this into ``ComponentInstance.new`` at placement time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from protoboard.domain.components.definition import (
    ComponentClass,
    ComponentDefinition,
)
from protoboard.domain.components.footprints import TwoPin


class TestDefaultValueField:
    def test_default_is_empty_string(self) -> None:
        defn = ComponentDefinition(
            name="BC547", footprint=TwoPin(),
            pin_names={}, component_class=ComponentClass.TRANSISTOR_NPN,
        )
        assert defn.default_value == ""

    def test_accepts_question_mark(self) -> None:
        defn = ComponentDefinition(
            name="R", footprint=TwoPin(),
            pin_names={}, component_class=ComponentClass.RESISTOR,
            default_value="?",
        )
        assert defn.default_value == "?"

    def test_accepts_arbitrary_string(self) -> None:
        defn = ComponentDefinition(
            name="BC547", footprint=TwoPin(),
            pin_names={}, component_class=ComponentClass.TRANSISTOR_NPN,
            default_value="BC547",
        )
        assert defn.default_value == "BC547"


class TestLibraryLoader:
    def _write(self, dir_: Path, content: str) -> Path:
        f = dir_ / "lib.yaml"
        f.write_text(content, encoding="utf-8")
        return f

    def test_yaml_default_value_parses(self, tmp_path: Path) -> None:
        from protoboard.services.library.loader import load_library
        self._write(tmp_path, """\
components:
  - name: R
    component_class: resistor
    footprint: {type: TwoPin}
    pin_names: {}
    default_value: "?"
""")
        lib = load_library([tmp_path])
        defn = lib.get("R")
        assert defn.default_value == "?"

    def test_yaml_omitted_field_defaults_to_empty(self, tmp_path: Path) -> None:
        from protoboard.services.library.loader import load_library
        self._write(tmp_path, """\
components:
  - name: BC547
    component_class: transistor_npn
    footprint: {type: TwoPin}
    pin_names: {}
""")
        lib = load_library([tmp_path])
        defn = lib.get("BC547")
        assert defn.default_value == ""

    def test_yaml_non_string_raises(self, tmp_path: Path) -> None:
        from protoboard.errors import SerializationError
        from protoboard.services.library.loader import load_library
        self._write(tmp_path, """\
components:
  - name: R
    component_class: resistor
    footprint: {type: TwoPin}
    pin_names: {}
    default_value: 4711
""")
        with pytest.raises(SerializationError, match="default_value"):
            load_library([tmp_path])


class TestBundledLibrary:
    """Smoke test: the shipped library labels generic vs concrete parts
    consistently."""

    def test_generic_passives_marked_question(self) -> None:
        from protoboard.services.library.loader import load_library
        from protoboard.services.library.package_family_loader import (
            load_full_package_catalogue,
        )
        lib = load_library([Path("library")], load_full_package_catalogue())
        # C_Disc / R_SMD_* are consolidated into the generic R / C (their bodies
        # are now footprint options), so only the generic passives remain.
        for name in (
            "R", "C", "C_POL", "L", "F", "Pot",
            "R_Net_4", "R_Net_8",
        ):
            assert lib.get(name).default_value == "?", (
                f"{name} should carry default_value '?'"
            )

    def test_concrete_parts_inherit_name(self) -> None:
        from protoboard.services.library.loader import load_library
        from protoboard.services.library.package_family_loader import (
            load_full_package_catalogue,
        )
        lib = load_library([Path("library")], load_full_package_catalogue())
        for name in (
            "BC547", "2N3904", "1N4148", "1N4007", "LED_Red",
            "NE555", "ATmega328P", "AT24C02", "IRF540_N",
            "XTAL_4MHz",
        ):
            assert lib.get(name).default_value == "", (
                f"{name} should fall back to defn.name (default_value '')"
            )
