# SPDX-License-Identifier: MIT
"""KiCAD netlist exporter — supports two format variants of ``version D``:

* **XML** (default) — ``<export version="D">…</export>``
* **S-expression** — ``(export (version D) …)``

Use ``fmt="xml"`` (default) or ``fmt="sexp"`` to select the output format.
"""

from __future__ import annotations

import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import structlog

from protoboard.domain.schematic.net import PinRef
from protoboard.domain.schematic.schematic import Schematic

_log = structlog.get_logger(__name__)


def _component_ids(schematic: Schematic) -> set[uuid.UUID]:
    """Return component IDs referenced by the schematic's nets."""
    return {pin.component_id for net in schematic.nets.values() for pin in net.pins}


def _sorted_pins(
    pins: set[PinRef],
    refdes: dict[uuid.UUID, str],
) -> list[PinRef]:
    """Sort pins in the stable order used by both KiCad encodings."""
    return sorted(
        pins,
        key=lambda pin: (
            refdes.get(pin.component_id, str(pin.component_id)),
            pin.pin_number,
        ),
    )


class KiCadNetlistExporter:
    """Serialise a `Schematic` to KiCAD legacy netlist format (XML or S-expr)."""

    def export(
        self,
        schematic: Schematic,
        refdes_map: dict[uuid.UUID, str] | None = None,
        fmt: str = "xml",
    ) -> str:
        """Return the netlist as a string.

        Args:
            schematic: The schematic to serialise.
            refdes_map: Maps component UUID → refdes string (e.g. "R1").
                If omitted, component UUIDs are used as identifiers.
            fmt: ``"xml"`` (default) or ``"sexp"``.
        """
        if fmt == "sexp":
            return self._export_sexp(schematic, refdes_map)
        return self._export_xml(schematic, refdes_map)

    def export_to_file(
        self,
        schematic: Schematic,
        path: Path,
        refdes_map: dict[uuid.UUID, str] | None = None,
        fmt: str = "xml",
    ) -> None:
        """Write the netlist to *path*."""
        _log.info("kicad.export_start", path=str(path), fmt=fmt)
        path.write_text(
            self.export(schematic, refdes_map=refdes_map, fmt=fmt),
            encoding="utf-8",
        )
        _log.info("kicad.export_done", path=str(path), nets=len(schematic.nets))

    # ------------------------------------------------------------------
    # XML variant
    # ------------------------------------------------------------------

    def _export_xml(
        self,
        schematic: Schematic,
        refdes_map: dict[uuid.UUID, str] | None,
    ) -> str:
        refdes = refdes_map or {}

        root = ET.Element("export", version="D")

        design = ET.SubElement(root, "design")
        ET.SubElement(design, "source").text = "protoboard-designer"
        ET.SubElement(design, "date").text = ""
        ET.SubElement(design, "tool").text = "protoboard-designer"

        comp_ids = _component_ids(schematic)

        components_el = ET.SubElement(root, "components")
        for cid in sorted(comp_ids, key=lambda x: refdes.get(x, str(x))):
            ref = refdes.get(cid, str(cid))
            comp_el = ET.SubElement(components_el, "comp", ref=ref)
            ET.SubElement(comp_el, "value").text = ref
            ET.SubElement(comp_el, "footprint").text = ""

        nets_el = ET.SubElement(root, "nets")
        for code, (_, net) in enumerate(
            sorted(schematic.nets.items(), key=lambda kv: kv[1].name), start=1
        ):
            net_el = ET.SubElement(nets_el, "net", code=str(code), name=net.name)
            for pin in _sorted_pins(net.pins, refdes):
                ref = refdes.get(pin.component_id, str(pin.component_id))
                ET.SubElement(net_el, "node", ref=ref, pin=str(pin.pin_number))

        ET.indent(root)
        return ET.tostring(root, encoding="unicode", xml_declaration=True)

    # ------------------------------------------------------------------
    # S-expression variant
    # ------------------------------------------------------------------

    def _export_sexp(
        self,
        schematic: Schematic,
        refdes_map: dict[uuid.UUID, str] | None,
    ) -> str:
        refdes = refdes_map or {}
        lines: list[str] = []

        lines.append("(export (version D)")
        lines.append("  (design")
        lines.append("    (source protoboard-designer)")
        lines.append('    (date "")')
        lines.append('    (tool "protoboard-designer"))')

        # (components ...)
        comp_ids = _component_ids(schematic)

        lines.append("  (components")
        for cid in sorted(comp_ids, key=lambda x: refdes.get(x, str(x))):
            ref = _sexp_str(refdes.get(cid, str(cid)))
            lines.append(f"    (comp (ref {ref})")
            lines.append(f"      (value {ref})")
            lines.append('      (footprint ""))')
        lines.append("  )")

        # (nets ...)
        lines.append("  (nets")
        sorted_nets = sorted(schematic.nets.values(), key=lambda n: n.name)
        for code, net in enumerate(sorted_nets, start=1):
            lines.append(f"    (net (code {code}) (name {_sexp_str(net.name)})")
            for pin in _sorted_pins(net.pins, refdes):
                ref = _sexp_str(refdes.get(pin.component_id, str(pin.component_id)))
                lines.append(f"      (node (ref {ref}) (pin {pin.pin_number}))")
            lines.append("    )")
        lines.append("  )")

        lines.append(")")
        return "\n".join(lines) + "\n"


def _sexp_str(value: str) -> str:
    """Return *value* as a properly quoted S-expression string atom if needed."""
    # Quote if contains spaces, parens, or special chars.
    if value and not any(c in value for c in ' \t\n\r()"\\'):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
