# SPDX-License-Identifier: MIT
"""Two-pin footprint — geometry and library loader integration.

After the footprint refactor a ``Footprint`` is only pin count + spacing:
the concrete ``TwoPin`` / ``SingleRow`` types no longer exist, so the
type-identity assertions that used to live here are gone.  A two-pin part
is geometrically ``SIL(n=2)``; the axial-vs-radial distinction lives in the
body/package, not the footprint.  What survives is the geometry contract and
the loader's acceptance of the ``SIL`` / ``TwoPin`` catalogue names.
"""

from __future__ import annotations

from pathlib import Path

from protoboard.domain.components.footprints import SIL
from protoboard.geometry import Point


class TestTwoPinGeometry:
    def test_default_pitch_one(self) -> None:
        fp = SIL(n=2)
        assert fp.pin_count() == 2
        assert fp.pin_positions() == [Point(0.0, 0.0), Point(0.0, 1.0)]

    def test_custom_pitch(self) -> None:
        fp = SIL(n=2, pitch=2.5)
        positions = fp.pin_positions()
        assert positions == [Point(0.0, 0.0), Point(0.0, 2.5)]

    def test_matches_single_row_n2_geometry(self) -> None:
        """A two-pin footprint must equal SIL(n=2) of the same pitch so
        existing renderers and snapshot tests stay valid."""
        assert SIL(n=2, pitch=1.5).pin_positions() == \
            SIL(n=2, pitch=1.5).pin_positions()
        assert SIL(n=2, pitch=1.5).pin_count() == 2

    # NOTE (footprint refactor): removed ``test_invalid_pitch_raises`` and
    # ``test_is_distinct_type_from_single_row`` — they asserted ``TwoPin``-only
    # pitch validation and the ``TwoPin``/``SingleRow`` isinstance distinction,
    # both of which no longer exist (footprints are pin count + spacing only).


class TestLibraryLoaderRedirect:
    """The library YAML schema is unchanged: the loader accepts both
    ``type: SIL`` and ``type: TwoPin`` catalogue names and produces the
    pin geometry the part declares.  The old type-redirect (pins:2 → a
    distinct ``TwoPin`` type) is gone, so we assert pin geometry instead.
    """

    def _write(self, dir_: Path, content: str) -> Path:
        f = dir_ / "lib.yaml"
        f.write_text(content, encoding="utf-8")
        return f

    def test_sil_pins_2_is_two_pin_geometry(self, tmp_path: Path) -> None:
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
        assert defn.default_footprint().pin_count() == 2

    def test_sil_pins_3_geometry(self, tmp_path: Path) -> None:
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
        assert defn.default_footprint().pin_count() == 3

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
        assert defn.default_footprint().pin_count() == 2
        assert defn.default_footprint().pin_positions() == [
            Point(0.0, 0.0), Point(0.0, 1.0)
        ]

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
        assert defn.default_footprint().pin_count() == 2
        assert defn.default_footprint().pin_positions() == [
            Point(0.0, 0.0), Point(0.0, 3.0)
        ]


class TestSILHelperUnchanged:
    """The Python-level ``SIL()`` helper builds the requested pin geometry.
    Test fixtures that construct components directly with ``SIL(2)`` keep
    working."""

    def test_sil_n2_helper_geometry(self) -> None:
        fp = SIL(2)
        assert fp.pin_count() == 2
        assert fp.pin_positions() == SIL(n=2).pin_positions()

    def test_sil_n3_helper_geometry(self) -> None:
        fp = SIL(3)
        assert fp.pin_count() == 3
