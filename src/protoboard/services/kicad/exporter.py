# SPDX-License-Identifier: MIT
"""KiCAD netlist exporter — legacy XML format (<export version="D">).

Converts a `Schematic` to a KiCAD-compatible netlist XML file that can be
imported into KiCAD PCB editor (File → Import → Netlist).
"""

from __future__ import annotations

import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from protoboard.domain.schematic.schematic import Schematic


class KiCadNetlistExporter:
    """Serialise a `Schematic` to KiCAD legacy XML netlist format."""

    def export(
        self,
        schematic: Schematic,
        refdes_map: dict[uuid.UUID, str] | None = None,
    ) -> str:
        """Return the netlist as an XML string.

        `refdes_map` maps component UUID → refdes string (e.g. "R1").
        If omitted, component UUIDs are used as refdes identifiers.
        """
        refdes = refdes_map or {}

        root = ET.Element("export", version="D")

        # <design> — required by KiCAD parser, content is informational.
        design = ET.SubElement(root, "design")
        ET.SubElement(design, "source").text = "protoboard-designer"
        ET.SubElement(design, "date").text = ""
        ET.SubElement(design, "tool").text = "protoboard-designer"

        # <components> — one <comp> per unique component_id.
        comp_ids: set[uuid.UUID] = set()
        for net in schematic.nets.values():
            for pin in net.pins:
                comp_ids.add(pin.component_id)

        components_el = ET.SubElement(root, "components")
        for cid in sorted(comp_ids, key=lambda x: refdes.get(x, str(x))):
            ref = refdes.get(cid, str(cid))
            comp_el = ET.SubElement(components_el, "comp", ref=ref)
            ET.SubElement(comp_el, "value").text = ref
            ET.SubElement(comp_el, "footprint").text = ""

        # <nets>
        nets_el = ET.SubElement(root, "nets")
        for code, (_, net) in enumerate(
            sorted(schematic.nets.items(), key=lambda kv: kv[1].name), start=1
        ):
            net_el = ET.SubElement(nets_el, "net", code=str(code), name=net.name)
            for pin in sorted(
                net.pins,
                key=lambda p: (
                    refdes.get(p.component_id, str(p.component_id)),
                    p.pin_number,
                ),
            ):
                ref = refdes.get(pin.component_id, str(pin.component_id))
                ET.SubElement(net_el, "node", ref=ref, pin=str(pin.pin_number))

        ET.indent(root)
        xml_str = ET.tostring(root, encoding="unicode", xml_declaration=True)
        return xml_str

    def export_to_file(
        self,
        schematic: Schematic,
        path: Path,
        refdes_map: dict[uuid.UUID, str] | None = None,
    ) -> None:
        """Write the netlist XML to *path*."""
        path.write_text(self.export(schematic, refdes_map=refdes_map), encoding="utf-8")
