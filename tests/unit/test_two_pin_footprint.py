# SPDX-License-Identifier: MIT
"""TwoPin footprint — geometry, validation, library loader integration."""

from __future__ import annotations

from pathlib import Path

import pytest

from protoboard.domain.components.footprints import (
    SIL,
    SingleRow,
    TwoPin,
)
from protoboard.geometry import Point


class TestTwoPinGeometry:
    def test_default_pitch_one(self) -> None:
        fp = TwoPin()
        assert fp.pin_count() == 2
        assert fp.pin_positions() == [Point(0.0, 0.0), Point(0.0, 1.0)]

    def test_custom_pitch(self) -> None:
        fp = TwoPin(pitch=2.5)
        positions = fp.pin_positions()
        assert positions == [Point(0.0, 0.0), Point(0.0, 2.5)]

    def test_invalid_pitch_raises(self) -> None:
        with pytest.raises(ValueError, match="pitch"):
            TwoPin(pitch=0.0)
        with pytest.raises(ValueError, match="pitch"):
            TwoPin(pitch=-1.0)

    def test_matches_single_row_n2_geometry(self) -> None:
        """TwoPin pin positions must equal SingleRow(n=2) of same pitch
        so existing renderers and snapshot tests stay valid during the
        migration window."""
        assert TwoPin(pitch=1.5).pin_positions() == \
            SingleRow(n=2, pitch=1.5).pin_positions()

    def test_is_distinct_type_from_single_row(self) -> None:
        """``isinstance`` discriminator: TwoPin is NOT SingleRow.

        The renderer dispatcher will eventually switch on this — a 2-pin
        component must dispatch to body-spec rendering, not to the SIL
        header renderer.
        """
        fp = TwoPin()
        assert not isinstance(fp, SingleRow)


class TestLibraryLoaderRedirect:
    """The library YAML schema doesn't change for the migration: the
    loader transparently redirects ``type: SIL, pins: 2`` to TwoPin.
    Three-or-more-pin SIL entries continue to resolve to SingleRow.
    """

    def _write(self, dir_: Path, content: str) -> Path:
        f = dir_ / "lib.yaml"
        f.write_text(content, encoding="utf-8")
        return f

    def test_sil_pins_2_becomes_two_pin(self, tmp_path: Path) -> None:
        from protoboard.services.library.loader import load_library
        self._write(tmp_path, """\
components:
  - name: R
    description: "Resistor"
    component_class: resistor
    footprint: {type: SIL, pins: 2}
    pin_names: {}
""")
        lib = load_library([tmp_path])
        defn = lib.get("R")
        assert isinstance(defn.footprint, TwoPin)

    def test_sil_pins_3_stays_single_row(self, tmp_path: Path) -> None:
        from protoboard.services.library.loader import load_library
        self._write(tmp_path, """\
components:
  - name: HDR3
    description: "3-pin SIL header"
    component_class: connector
    footprint: {type: SIL, pins: 3}
    pin_names: {}
""")
        lib = load_library([tmp_path])
        defn = lib.get("HDR3")
        assert isinstance(defn.footprint, SingleRow)
        assert not isinstance(defn.footprint, TwoPin)

    def test_explicit_two_pin_type_in_yaml(self, tmp_path: Path) -> None:
        from protoboard.services.library.loader import load_library
        self._write(tmp_path, """\
components:
  - name: R_new
    description: "Resistor (TwoPin)"
    component_class: resistor
    footprint: {type: TwoPin}
    pin_names: {}
""")
        lib = load_library([tmp_path])
        defn = lib.get("R_new")
        assert isinstance(defn.footprint, TwoPin)
        assert defn.footprint.pitch == 1.0

    def test_explicit_two_pin_with_pitch(self, tmp_path: Path) -> None:
        from protoboard.services.library.loader import load_library
        self._write(tmp_path, """\
components:
  - name: R_wide
    description: "Wide resistor"
    component_class: resistor
    footprint: {type: TwoPin, pitch: 3.0}
    pin_names: {}
""")
        lib = load_library([tmp_path])
        defn = lib.get("R_wide")
        assert isinstance(defn.footprint, TwoPin)
        assert defn.footprint.pitch == 3.0


class TestSILHelperUnchanged:
    """The Python-level SIL() helper still returns SingleRow — only the
    YAML loader applies the n=2 redirect.  Test fixtures that construct
    components directly with SIL(2) keep working."""

    def test_sil_n2_helper_returns_single_row(self) -> None:
        fp = SIL(2)
        assert isinstance(fp, SingleRow)
        # Geometry parity is what matters downstream.
        assert fp.pin_positions() == TwoPin().pin_positions()

    def test_sil_n3_helper_returns_single_row(self) -> None:
        fp = SIL(3)
        assert isinstance(fp, SingleRow)
        assert fp.pin_count() == 3
