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
        assert (
            SIL(n=2, pitch=1.5).pin_positions() == SIL(n=2, pitch=1.5).pin_positions()
        )
        assert SIL(n=2, pitch=1.5).pin_count() == 2

    # NOTE (footprint refactor): removed ``test_invalid_pitch_raises`` and
    # ``test_is_distinct_type_from_single_row`` — they asserted ``TwoPin``-only
    # pitch validation and the ``TwoPin``/``SingleRow`` isinstance distinction,
    # both of which no longer exist (footprints are pin count + spacing only).


class TestYamlGeometryDeclarations:
    """The removed ``footprint:`` field is rejected (component skipped with a
    migration hint); the same geometry is declared via ``package:`` — a kind
    for raster bodies, an explicit prism for non-raster pitches."""

    def _write(self, dir_: Path, content: str) -> Path:
        f = dir_ / "lib.yaml"
        f.write_text(content, encoding="utf-8")
        return f

    def test_removed_footprint_field_skips_part_with_hint(self, tmp_path: Path) -> None:
        from structlog.testing import capture_logs

        from protoboard.services.library.loader import load_library

        self._write(
            tmp_path,
            """\
components:
  - name: OLD_R
    component_class: resistor
    footprint: {type: TwoPin}
  - name: NEW_R
    component_class: resistor
    meta_pins: [P1, P2]
    package: {kind: sil}
""",
        )
        with capture_logs() as logs:
            lib = load_library([tmp_path], package_families={})
        # The legacy part is skipped (not a whole-library failure); the log
        # carries the migration hint.
        assert "OLD_R" not in lib
        assert "NEW_R" in lib
        assert any(
            e["event"] == "library.package_skipped"
            and "'footprint:' field was removed" in str(e.get("error"))
            for e in logs
        )

    def test_kind_sil_pins_2_geometry(self, tmp_path: Path) -> None:
        from protoboard.services.library.loader import load_library

        self._write(
            tmp_path,
            """\
components:
  - name: R
    description: "Resistor"
    component_class: resistor
    meta_pins: [P1, P2]
    package: {kind: sil}
""",
        )
        lib = load_library([tmp_path], package_families={})
        defn = lib.get("R")
        package = defn.default_footprint(lib.package_families)
        assert package.pin_count() == 2
        assert package.pin_positions() == [Point(0.0, 0.0), Point(0.0, 1.0)]

    def test_kind_sil_pins_3_geometry(self, tmp_path: Path) -> None:
        from protoboard.services.library.loader import load_library

        self._write(
            tmp_path,
            """\
components:
  - name: HDR3
    description: "3-pin SIL header"
    component_class: connector
    meta_pins: [P1, P2, P3]
    package: {kind: sil}
""",
        )
        lib = load_library([tmp_path], package_families={})
        assert lib.get("HDR3").default_footprint(lib.package_families).pin_count() == 3

    def test_explicit_prism_declares_non_raster_pitch(self, tmp_path: Path) -> None:
        # The old ``footprint: {type: TwoPin, pitch: 3.0}`` becomes an explicit
        # prism whose body_offset spells the 3-hole pitch out.
        from protoboard.services.library.loader import load_library

        self._write(
            tmp_path,
            """\
components:
  - name: R_wide
    description: "Wide resistor"
    component_class: resistor
    meta_pins: [P1, P2]
    package:
      shape: prism
      margin: 1.25
      leads:
        direction: [0, 0, -1]
        body_offset: {unit: 2.5, value: [[0, 3]]}
        board_offset: default
      bend: 0
""",
        )
        lib = load_library([tmp_path], package_families={})
        defn = lib.get("R_wide")
        assert defn.default_footprint(lib.package_families).pin_positions() == [
            Point(0.0, 0.0),
            Point(0.0, 3.0),
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
