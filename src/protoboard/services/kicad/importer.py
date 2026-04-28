# SPDX-License-Identifier: MIT
"""KiCAD netlist importer — legacy XML format (<export version="D">).

Parses KiCAD's exported netlist XML and converts it to a `Schematic`.
Unknown footprints are skipped with a warning; the net topology is always
preserved regardless.
"""

from __future__ import annotations

import re
import uuid
import warnings
import xml.etree.ElementTree as ET
from pathlib import Path

from protoboard.domain.components.footprints import (
    DualRow,
    FootprintTemplate,
    SingleRow,
)
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic


class ImportWarning(UserWarning):
    """Raised when a footprint cannot be resolved during import."""


class KiCadNetlistImporter:
    """Parse KiCAD legacy XML netlist into a `Schematic`."""

    def import_(self, path: Path) -> Schematic:
        return self.import_from_string(path.read_text(encoding="utf-8"))

    def import_from_string(self, xml: str) -> Schematic:
        root = ET.fromstring(xml)
        if root.tag != "export":
            raise ValueError(f"Expected root element <export>, got <{root.tag}>")

        # Map refdes → stable UUID so all nets referencing the same component
        # share the same component_id.
        refdes_to_id: dict[str, uuid.UUID] = {}

        def _comp_id(refdes: str) -> uuid.UUID:
            if refdes not in refdes_to_id:
                refdes_to_id[refdes] = uuid.uuid4()
            return refdes_to_id[refdes]

        # Validate footprints (emit warnings for unknowns).
        for comp_el in root.findall("./components/comp"):
            fp_el = comp_el.find("footprint")
            if fp_el is not None and fp_el.text:
                fp_str = fp_el.text.strip()
                if fp_str and self.resolve_footprint(fp_str) is None:
                    warnings.warn(
                        f"Unknown footprint '{fp_str}' for component "
                        f"'{comp_el.get('ref', '?')}' — skipping footprint mapping.",
                        ImportWarning,
                        stacklevel=2,
                    )

        # Build nets from <nets> section.
        nets: dict[uuid.UUID, Net] = {}
        for net_el in root.findall("./nets/net"):
            name = net_el.get("name", "")
            if not name:
                continue
            pins: set[PinRef] = set()
            for node in net_el.findall("node"):
                ref = node.get("ref", "")
                pin_str = node.get("pin", "")
                if not ref or not pin_str:
                    continue
                try:
                    pin_num = int(pin_str)
                except ValueError:
                    continue
                pins.add(PinRef(component_id=_comp_id(ref), pin_number=pin_num))
            net_id = uuid.uuid4()
            nets[net_id] = Net(id=net_id, name=name, pins=pins)

        return Schematic(nets=nets)

    def resolve_footprint(self, fp_string: str) -> FootprintTemplate | None:
        """Map a KiCAD footprint string to a `FootprintTemplate`, or None."""
        # Strip library prefix: "Package_DIP:DIP-8_W7.62mm" → "DIP-8_W7.62mm"
        name = fp_string.split(":")[-1] if ":" in fp_string else fp_string

        # DIP-N patterns: "DIP-8_*", "DIP-14_*", etc.
        m = re.match(r"DIP[-_](\d+)", name, re.IGNORECASE)
        if m:
            total = int(m.group(1))
            if total > 0 and total % 2 == 0:
                return DualRow(n_per_row=total // 2)

        # Resistor / axial 2-pin THT
        if re.search(r"R_Axial", name):
            return SingleRow(n=2)

        # TO-92 transistor (3 pins in a line)
        if re.search(r"TO-92", name, re.IGNORECASE):
            return SingleRow(n=3)

        # PinHeader_MxNN: e.g. PinHeader_1x02, PinHeader_2x08
        m2 = re.match(r"PinHeader_(\d+)x(\d+)", name, re.IGNORECASE)
        if m2:
            rows = int(m2.group(1))
            cols = int(m2.group(2))
            total = rows * cols
            if rows == 1:
                return SingleRow(n=total)
            if rows == 2 and total % 2 == 0:
                return DualRow(n_per_row=cols)

        return None
