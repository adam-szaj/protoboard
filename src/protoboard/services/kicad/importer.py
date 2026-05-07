# SPDX-License-Identifier: MIT
"""KiCAD netlist importer — supports two format variants of ``version D``:

* **XML** — ``<?xml …><export version="D">…</export>``
* **S-expression** — ``(export (version D) …)``

Both variants carry identical semantic content; format is auto-detected from
the first non-whitespace character.

Two import modes
----------------
* ``import_()`` / ``import_from_string()`` — legacy, returns only a
  ``Schematic`` (nets).  Kept for backward compatibility.
* ``import_full()`` / ``import_full_from_string()`` — full import that also
  produces ``ComponentInstance`` objects and a ``FootprintMap``.  This is the
  preferred entry point for the GUI import workflow.
"""

from __future__ import annotations

import re
import uuid
import warnings
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import structlog
from protoboard.domain.components.footprints import (
    DualRow,
    FootprintTemplate,
    SingleRow,
)
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.services.kicad.footprint_map import FootprintEntry, FootprintMap
from protoboard.services.kicad.import_result import NetlistImportResult
from protoboard.services.kicad.sexp_parser import attr, child, children, parse

_log = structlog.get_logger(__name__)


class ImportWarning(UserWarning):
    """Raised when a footprint cannot be resolved during import."""


# ---------------------------------------------------------------------------
# Raw component data — internal intermediate representation
# ---------------------------------------------------------------------------

@dataclass
class _RawComp:
    """Parsed component record from the netlist, before footprint resolution."""

    refdes: str
    value: str
    kicad_fp: str        # raw KiCAD footprint string; may be empty
    kicad_lib: str = ""  # libsource lib  (e.g. "Switch")
    kicad_part: str = "" # libsource part (e.g. "SW_SPDT")


# ---------------------------------------------------------------------------
# Importer
# ---------------------------------------------------------------------------

class KiCadNetlistImporter:
    """Parse KiCAD netlist files (XML or S-expression)."""

    # ------------------------------------------------------------------
    # Legacy API — returns only Schematic
    # ------------------------------------------------------------------

    def import_(self, path: Path) -> Schematic:
        _log.info("kicad.import_start", path=str(path))
        return self.import_from_string(path.read_text(encoding="utf-8"))

    def import_from_string(self, text: str) -> Schematic:
        return self._parse(text.lstrip()).schematic

    # ------------------------------------------------------------------
    # Full import API — returns NetlistImportResult
    # ------------------------------------------------------------------

    def import_full(
        self,
        path: Path,
        footprint_map: FootprintMap | None = None,
    ) -> NetlistImportResult:
        """Import *path* and return a full result including ComponentInstances.

        If *footprint_map* is provided (loaded from ``footprint_map.yaml``),
        it is merged with the auto-generated map — user entries take priority.
        """
        _log.info("kicad.import_full_start", path=str(path))
        return self.import_full_from_string(
            path.read_text(encoding="utf-8"),
            footprint_map=footprint_map,
        )

    def import_full_from_string(
        self,
        text: str,
        footprint_map: FootprintMap | None = None,
    ) -> NetlistImportResult:
        parsed = self._parse(text.lstrip())
        auto_map = self._build_footprint_map(parsed.raw_comps)
        effective_map = auto_map.merge(footprint_map) if footprint_map else auto_map

        instances, unresolved_fps = self._build_instances(
            parsed.raw_comps, parsed.refdes_to_id, effective_map
        )
        result = NetlistImportResult(
            schematic=parsed.schematic,
            instances=instances,
            footprint_map=effective_map,
            unresolved_fps=unresolved_fps,
        )
        _log.info(
            "kicad.import_full_complete",
            nets=len(parsed.schematic.nets),
            instances=len(instances),
            unresolved=len(unresolved_fps),
        )
        return result

    # ------------------------------------------------------------------
    # Footprint map generation
    # ------------------------------------------------------------------

    def _build_footprint_map(self, raw_comps: list[_RawComp]) -> FootprintMap:
        """Auto-generate a FootprintMap from the raw component list.

        Priority:
          1. libsource when it unambiguously identifies the component class
             (Switch, Crystal, Mechanical, …) — wins even over a filled
             footprint field, because KiCAD projects sometimes assign an
             arbitrary physical footprint (e.g. R_2816 pads) to a switch.
          2. KiCAD footprint string — the physical package.
          3. libsource fallback — for components with no footprint at all.
        """
        entries: dict[str, FootprintEntry] = {}
        for comp in raw_comps:
            fp = comp.kicad_fp

            # When libsource unambiguously identifies the component type, use a
            # libsource-derived key — even if a footprint string is present.
            # This prevents e.g. SW_SPDT and R from sharing the same R_2816 key.
            authoritative = _libsource_is_authoritative(
                comp.kicad_lib, comp.kicad_part
            )
            if authoritative and comp.kicad_part:
                map_key = f"__libsource__{comp.kicad_lib}__{comp.kicad_part}"
            else:
                map_key = fp or f"__libsource__{comp.kicad_lib}__{comp.kicad_part}"

            if map_key in entries:
                continue

            # Priority 1: libsource strong hint overrides the footprint string.
            libsource_resolved = self.resolve_by_libsource(
                comp.kicad_lib, comp.kicad_part
            )
            if libsource_resolved is not None and authoritative:
                resolved = libsource_resolved
                def_ref = _part_to_definition_ref(comp.kicad_part, resolved)
                entries[map_key] = FootprintEntry(
                    kicad_fp=map_key,
                    definition_ref=def_ref,
                    pin_count=resolved.pin_count(),
                )
                continue

            # Priority 2: footprint string.
            resolved = self.resolve_footprint(fp) if fp else None

            # Priority 3: libsource as fallback (no footprint or unknown fp).
            if resolved is None:
                resolved = libsource_resolved

            if resolved is not None:
                def_ref = (
                    _footprint_to_definition_ref(fp, resolved)
                    if fp
                    else _part_to_definition_ref(comp.kicad_part, resolved)
                )
                entries[map_key] = FootprintEntry(
                    kicad_fp=map_key,
                    definition_ref=def_ref,
                    pin_count=resolved.pin_count(),
                )
            else:
                pin_count = _infer_pin_count(fp) if fp else 0
                entries[map_key] = FootprintEntry(
                    kicad_fp=map_key,
                    definition_ref="",
                    pin_count=pin_count,
                )
                if fp:
                    warnings.warn(
                        f"Unknown footprint '{fp}' — added to footprint_map.yaml "
                        f"with pin_count={pin_count}. Fill in definition_ref and "
                        "re-import.",
                        ImportWarning,
                        stacklevel=4,
                    )
        return FootprintMap(entries=entries)

    # ------------------------------------------------------------------
    # Instance construction
    # ------------------------------------------------------------------

    def _build_instances(
        self,
        raw_comps: list[_RawComp],
        refdes_to_id: dict[str, uuid.UUID],
        fp_map: FootprintMap,
    ) -> tuple[dict[str, ComponentInstance], list[str]]:
        instances: dict[str, ComponentInstance] = {}
        unresolved_fps: list[str] = []
        seen_unresolved: set[str] = set()

        for comp in raw_comps:
            comp_id = refdes_to_id[comp.refdes]
            # Mirror the key logic from _build_footprint_map exactly.
            if _libsource_is_authoritative(comp.kicad_lib, comp.kicad_part) and comp.kicad_part:
                map_key = f"__libsource__{comp.kicad_lib}__{comp.kicad_part}"
            else:
                map_key = (
                    comp.kicad_fp
                    or f"__libsource__{comp.kicad_lib}__{comp.kicad_part}"
                )
            entry = fp_map.get(map_key)

            if entry is None:
                if not comp.kicad_fp:
                    # Component has no footprint in KiCAD (virtual/power symbol),
                    # and no libsource entry resolved it either — silent placeholder.
                    pin_count = 1
                    definition_ref = "placeholder_SIL_1"
                else:
                    # Genuinely unknown footprint.
                    pin_count = 1
                    definition_ref = f"placeholder_SIL_{pin_count}"
                    if comp.kicad_fp not in seen_unresolved:
                        unresolved_fps.append(comp.kicad_fp)
                        seen_unresolved.add(comp.kicad_fp)
            elif entry.pin_count == 0:
                # Entry exists but pin_count unknown — SIL(0) placeholder
                pin_count = 0
                definition_ref = entry.definition_ref or "placeholder_SIL_0"
                if comp.kicad_fp not in seen_unresolved:
                    unresolved_fps.append(comp.kicad_fp)
                    seen_unresolved.add(comp.kicad_fp)
            else:
                pin_count = entry.pin_count
                definition_ref = (
                    entry.definition_ref
                    or f"placeholder_SIL_{pin_count}"
                )

            instances[comp.refdes] = ComponentInstance(
                id=comp_id,
                definition_ref=definition_ref,
                refdes=comp.refdes,
                value=comp.value,
            )

        return instances, unresolved_fps

    # ------------------------------------------------------------------
    # Footprint resolution heuristic
    # ------------------------------------------------------------------

    def resolve_footprint(self, fp_string: str) -> FootprintTemplate | None:
        """Map a KiCAD footprint string to a FootprintTemplate, or None."""
        name = fp_string.split(":")[-1] if ":" in fp_string else fp_string

        # DIP-N
        m = re.match(r"DIP[-_](\d+)", name, re.IGNORECASE)
        if m:
            total = int(m.group(1))
            if total > 0 and total % 2 == 0:
                return DualRow(n_per_row=total // 2)

        # Resistors / axial 2-pin
        if re.search(r"R_Axial|R_\d{4}_\d{4}", name):
            return SingleRow(n=2)

        # Capacitors — all 2-pin
        if re.search(r"C_\d{4}_\d{4}|CP_\d{4}", name):
            return SingleRow(n=2)

        # Diodes — all 2-pin
        if re.search(r"D_DO-|D_SOD|D_SMA|D_SMB|D_SMC", name, re.IGNORECASE):
            return SingleRow(n=2)

        # SOT-23: 3 pins
        if re.search(r"SOT-23", name, re.IGNORECASE):
            return SingleRow(n=3)

        # TO-92: 3 pins (inline or not)
        if re.search(r"TO-92", name, re.IGNORECASE):
            return SingleRow(n=3)

        # TO-126: 3 pins
        if re.search(r"TO-126", name, re.IGNORECASE):
            return SingleRow(n=3)

        # TO-220: 3 pins
        if re.search(r"TO-220", name, re.IGNORECASE):
            return SingleRow(n=3)

        # TSOP-6 / SOT-23-6 / similar 6-pin small packages
        if re.search(r"TSOP-6|SOT-23-6|SC-70-6", name, re.IGNORECASE):
            return SingleRow(n=6)

        # Test points: 1 pin
        if re.search(r"TestPoint", name, re.IGNORECASE):
            return SingleRow(n=1)

        # PinHeader_MxNN
        m2 = re.match(r"PinHeader_(\d+)x(\d+)", name, re.IGNORECASE)
        if m2:
            rows, cols = int(m2.group(1)), int(m2.group(2))
            total = rows * cols
            if rows == 1:
                return SingleRow(n=total)
            if rows == 2 and total % 2 == 0:
                return DualRow(n_per_row=cols)

        # Arduino Nano module: treat as 30-pin SIL
        if re.search(r"Arduino_Nano", name, re.IGNORECASE):
            return SingleRow(n=30)

        return None

    def resolve_by_libsource(
        self, kicad_lib: str, kicad_part: str
    ) -> FootprintTemplate | None:
        """Resolve a FootprintTemplate from KiCAD libsource (lib, part) pair.

        Used as a fallback when the component has no footprint string.
        Covers KiCAD standard library part names.
        """
        part = kicad_part.strip()
        if not part:
            return None

        # Switches — map by pin count encoded in part name.
        _SW_PINS: dict[str, int] = {
            "SW_SPST": 2,
            "SW_SPDT": 3,
            "SW_DPST": 4,
            "SW_DPDT": 6,
            "SW_Push": 2,
            "SW_DIP_x2": 4,
            "SW_DIP_x4": 8,
            "SW_DIP_x8": 16,
        }
        if part in _SW_PINS:
            return SingleRow(n=_SW_PINS[part])

        # Generic switch fallback: SW_* with trailing digits (e.g. SW_MEC_5E → 3 pins).
        if re.match(r"SW_", part):
            m = re.search(r"(\d+)[Pp]ole", part)
            if m:
                return SingleRow(n=int(m.group(1)) * 2)
            return SingleRow(n=3)  # most common: SPDT

        # Test points.
        m_tp = re.match(r"TestPoint_(\d+)[Pp]ole", part)
        if m_tp:
            return SingleRow(n=int(m_tp.group(1)))
        if re.match(r"TestPoint", part):
            return SingleRow(n=1)

        # Connectors by part name pattern: Conn_01x02, Conn_02x08 etc.
        m_conn = re.match(r"Conn_0*(\d+)x0*(\d+)", part)
        if m_conn:
            rows, cols = int(m_conn.group(1)), int(m_conn.group(2))
            total = rows * cols
            if rows == 1:
                return SingleRow(n=total)
            if rows == 2 and total % 2 == 0:
                return DualRow(n_per_row=cols)

        # Resistor / capacitor / inductor / diode — all 2-pin passives.
        if part in ("R", "R_Small", "R_US", "C", "C_Small", "L", "L_Small", "D", "D_Zener"):
            return SingleRow(n=2)

        # Transistors — 3 pins.
        if re.match(r"(Q_|NPN|PNP|NMOS|PMOS|BJT|FET|JFET|MOSFET)", part):
            return SingleRow(n=3)

        return None

    # ------------------------------------------------------------------
    # Parsing (shared between legacy and full-import paths)
    # ------------------------------------------------------------------

    @dataclass
    class _Parsed:
        schematic: Schematic
        raw_comps: list[_RawComp]
        refdes_to_id: dict[str, uuid.UUID]

    def _parse(self, text: str) -> _Parsed:
        if text.startswith("<") or text.startswith("<?"):
            return self._parse_xml(text)
        elif text.startswith("("):
            return self._parse_sexp(text)
        else:
            raise ValueError(
                "Unrecognised KiCAD netlist format — "
                "expected XML (<export …>) or S-expression ((export …))"
            )

    def _parse_xml(self, xml: str) -> _Parsed:
        root = ET.fromstring(xml)
        if root.tag != "export":
            raise ValueError(f"Expected root element <export>, got <{root.tag}>")

        refdes_to_id: dict[str, uuid.UUID] = {}

        def _comp_id(refdes: str) -> uuid.UUID:
            if refdes not in refdes_to_id:
                refdes_to_id[refdes] = uuid.uuid4()
            return refdes_to_id[refdes]

        raw_comps: list[_RawComp] = []
        for comp_el in root.findall("./components/comp"):
            ref = comp_el.get("ref", "")
            if not ref:
                continue
            val_el = comp_el.find("value")
            val = (val_el.text or "").strip() if val_el is not None else ""
            fp_el = comp_el.find("footprint")
            fp = (fp_el.text or "").strip() if fp_el is not None else ""
            lib_el = comp_el.find("libsource")
            kicad_lib = lib_el.get("lib", "") if lib_el is not None else ""
            kicad_part = lib_el.get("part", "") if lib_el is not None else ""
            _comp_id(ref)
            raw_comps.append(_RawComp(
                refdes=ref, value=val, kicad_fp=fp,
                kicad_lib=kicad_lib, kicad_part=kicad_part,
            ))

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

        _log.info("kicad.parse_complete", nets=len(nets), components=len(refdes_to_id))
        return self._Parsed(
            schematic=Schematic(nets=nets),
            raw_comps=raw_comps,
            refdes_to_id=refdes_to_id,
        )

    def _parse_sexp(self, text: str) -> _Parsed:
        root = parse(text)
        if not root or root[0] != "export":
            raise ValueError(
                f"Expected top-level 'export' S-expression, got '{root[0] if root else '?'}'"
            )

        refdes_to_id: dict[str, uuid.UUID] = {}

        def _comp_id(refdes: str) -> uuid.UUID:
            if refdes not in refdes_to_id:
                refdes_to_id[refdes] = uuid.uuid4()
            return refdes_to_id[refdes]

        raw_comps: list[_RawComp] = []
        _components_node = child(root, "components")
        for comp_node in (children(_components_node, "comp") if _components_node else []):
            ref = attr(comp_node, "ref") or ""
            if not ref:
                continue
            val = attr(comp_node, "value") or ""
            fp = attr(comp_node, "footprint") or ""
            ls = child(comp_node, "libsource")
            kicad_lib = attr(ls, "lib") or "" if ls is not None else ""
            kicad_part = attr(ls, "part") or "" if ls is not None else ""
            _comp_id(ref)
            raw_comps.append(_RawComp(
                refdes=ref, value=val, kicad_fp=fp,
                kicad_lib=kicad_lib, kicad_part=kicad_part,
            ))

        nets_section = child(root, "nets")
        nets: dict[uuid.UUID, Net] = {}
        if nets_section is not None:
            for net_node in children(nets_section, "net"):
                name = attr(net_node, "name") or ""
                if not name:
                    continue
                pins: set[PinRef] = set()
                for node in children(net_node, "node"):
                    ref = attr(node, "ref") or ""
                    pin_str = attr(node, "pin") or ""
                    if not ref or not pin_str:
                        continue
                    try:
                        pin_num = int(pin_str)
                    except ValueError:
                        continue
                    pins.add(PinRef(component_id=_comp_id(ref), pin_number=pin_num))
                net_id = uuid.uuid4()
                nets[net_id] = Net(id=net_id, name=name, pins=pins)

        _log.info("kicad.parse_complete", nets=len(nets), components=len(refdes_to_id))
        return self._Parsed(
            schematic=Schematic(nets=nets),
            raw_comps=raw_comps,
            refdes_to_id=refdes_to_id,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# KiCAD library names whose `part` field unambiguously identifies the
# component's function — regardless of the physical footprint assigned.
# When a component belongs to one of these libs, libsource wins over the
# footprint string (which may be a placeholder like R_2816 on a switch).
_AUTHORITATIVE_LIBS: frozenset[str] = frozenset({
    "Switch",
    "Crystal",
    "Mechanical",
    "Relay",
    "Transformer",
    "Fuse",
    "Display",
    "RF",
    "Sensor",
    "Motor",
})


def _libsource_is_authoritative(kicad_lib: str, kicad_part: str) -> bool:
    """True when the KiCAD library name unambiguously identifies the component
    function, making it more trustworthy than the footprint string."""
    return kicad_lib in _AUTHORITATIVE_LIBS


def _part_to_definition_ref(kicad_part: str, template: FootprintTemplate) -> str:
    """Generate a definition_ref from a KiCAD library part name (used when no footprint)."""
    name = re.sub(r"[^\w]", "_", kicad_part)
    name = re.sub(r"_+", "_", name).strip("_")
    return name or f"generic_{template.pin_count()}pin"


def _footprint_to_definition_ref(kicad_fp: str, template: FootprintTemplate) -> str:
    """Generate a stable, human-readable definition_ref from a KiCAD footprint string."""
    name = kicad_fp.split(":")[-1] if ":" in kicad_fp else kicad_fp
    # Trim long suffixes like _P2.54mm_Vertical, _DIN0207_L6.3mm_...
    name = re.sub(r"_P\d[\d.]*mm.*", "", name)
    name = re.sub(r"_\w+\d[\d.]*mm.*", "", name)
    # Replace spaces and special chars with underscores
    name = re.sub(r"[^\w]", "_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name or f"generic_{template.pin_count()}pin"


def _infer_pin_count(kicad_fp: str) -> int:
    """Best-effort pin count from footprint string when heuristic resolution fails."""
    name = kicad_fp.split(":")[-1] if ":" in kicad_fp else kicad_fp
    # e.g. SOIC-8, QFN-48, TSSOP-14
    m = re.search(r"[-_](\d+)[_\s]", name + "_")
    if m:
        n = int(m.group(1))
        if 1 <= n <= 256:
            return n
    return 0
