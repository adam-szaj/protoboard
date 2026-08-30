# SPDX-License-Identifier: MIT
"""FootprintMap — persistent mapping from KiCAD footprint strings to project
definition refs and pin counts.

Workflow
--------
1. ``FootprintMap.from_netlist(raw_comps)`` — build from parsed netlist data;
   auto-resolves known footprints, marks unknowns as unresolved.
2. ``FootprintMap.save(path)`` — write ``footprint_map.yaml`` to the project dir.
3. User edits the YAML, fills in missing ``definition_ref`` / ``pin_count``.
4. ``FootprintMap.load(path)`` — reload; all entries now resolved.
5. Pass to ``KiCadNetlistImporter.import_with_map()`` for full import.

YAML schema
-----------
::

    # footprint_map.yaml
    mappings:
      "Resistor_SMD:R_0603_1608Metric":
        definition_ref: R_generic_2pin   # arbitrary string; need not exist in library
        pin_count: 2
      "Module:Arduino_Nano_WithMountingHoles":
        definition_ref: Arduino_Nano     # leave empty ("") to create SIL placeholder
        pin_count: 30
      "TestPoint:TestPoint_Pad_2.0x2.0mm":
        definition_ref: ""               # empty → placeholder SIL(pin_count)
        pin_count: 1

All entries always have both fields.  ``definition_ref: ""`` means "no known
definition — use a generic SIL(pin_count) placeholder".
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

_yaml = YAML()
_yaml.default_flow_style = False
_yaml.width = 120


@dataclass
class FootprintEntry:
    """One row in the mapping table."""

    kicad_fp: str
    definition_ref: str  # empty string = use generic SIL placeholder
    pin_count: int  # number of pins; 0 = not yet determined


@dataclass
class FootprintMap:
    """Collection of KiCAD footprint → definition_ref mappings."""

    entries: dict[str, FootprintEntry] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_entries(cls, entries: list[FootprintEntry]) -> FootprintMap:
        return cls(entries={e.kicad_fp: e for e in entries})

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get(self, kicad_fp: str) -> FootprintEntry | None:
        return self.entries.get(kicad_fp)

    def unresolved(self) -> list[FootprintEntry]:
        """Entries where definition_ref is empty and pin_count is 0."""
        return [
            e
            for e in self.entries.values()
            if not e.definition_ref and e.pin_count == 0
        ]

    def all_resolved(self) -> bool:
        return all(e.pin_count > 0 for e in self.entries.values())

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: Path) -> None:
        """Write footprint_map.yaml to *path*."""
        data: dict[str, Any] = {"mappings": {}}
        for fp, entry in sorted(self.entries.items()):
            data["mappings"][fp] = {
                "definition_ref": entry.definition_ref,
                "pin_count": entry.pin_count,
            }
        buf = io.StringIO()
        _yaml.dump(data, buf)
        path.write_text(buf.getvalue(), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> FootprintMap:
        """Load footprint_map.yaml from *path*."""
        data = _yaml.load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{path}: expected a YAML mapping at top level")
        mappings = data.get("mappings") or {}
        entries: dict[str, FootprintEntry] = {}
        for fp, row in mappings.items():
            if not isinstance(row, dict):
                continue
            entries[fp] = FootprintEntry(
                kicad_fp=str(fp),
                definition_ref=str(row.get("definition_ref") or ""),
                pin_count=int(row.get("pin_count") or 0),
            )
        return cls(entries=entries)

    def merge(self, other: FootprintMap) -> FootprintMap:
        """Return a new map with *other*'s entries overlaid on top of self.

        Useful when re-importing: the user's edited map takes priority over the
        auto-generated one.
        """
        merged = dict(self.entries)
        merged.update(other.entries)
        return FootprintMap(entries=merged)
