# SPDX-License-Identifier: MIT
"""Component library loader.

Reads YAML files from one or more directories and exposes a name-keyed
component dictionary. When the same name appears in multiple
directories, the *first* directory wins (so users can override built-ins).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from protoboard.domain.components.definition import ComponentClass, ComponentDefinition
from protoboard.domain.components.footprints import (
    DIP,
    SIL,
    TO92,
    TO220,
    DualRow,
    FootprintTemplate,
    GridFootprint,
    SingleRow,
)
from protoboard.errors import SerializationError

_yaml = YAML(typ="safe")


@dataclass
class ComponentLibrary:
    _by_name: dict[str, ComponentDefinition] = field(default_factory=dict)

    def names(self) -> Iterable[str]:
        return self._by_name.keys()

    def get(self, name: str) -> ComponentDefinition:
        return self._by_name[name]

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._by_name


def load_library(directories: Iterable[Path]) -> ComponentLibrary:
    """Load a library from the given directories. Earlier directories win."""
    lib = ComponentLibrary()
    for directory in directories:
        if not directory.exists():
            raise FileNotFoundError(f"Library directory not found: {directory}")
        for yaml_path in sorted(directory.glob("*.yaml")):
            for defn in _load_file(yaml_path):
                # First directory wins → only set if not already present.
                lib._by_name.setdefault(defn.name, defn)
    return lib


def _load_file(path: Path) -> list[ComponentDefinition]:
    try:
        data = _yaml.load(path.read_text())
    except YAMLError as exc:
        raise SerializationError(f"YAML parse error in {path}: {exc}") from exc
    if data is None:
        return []
    if not isinstance(data, dict):
        raise SerializationError(f"{path}: top-level must be a mapping")
    components_raw = data.get("components", [])
    if not isinstance(components_raw, list):
        raise SerializationError(f"{path}: 'components' must be a list")

    # Build a name→raw-dict index for extends-resolution (templates included).
    raw_by_name: dict[str, Any] = {}
    for raw in components_raw:
        if isinstance(raw, dict) and isinstance(raw.get("name"), str):
            raw_by_name[raw["name"]] = raw

    result: list[ComponentDefinition] = []
    for raw in components_raw:
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
            raise SerializationError(
                f"{path}: each component must be a mapping with 'name'"
            )
        name: str = raw["name"]
        if name.startswith("_"):
            continue  # template — not placeable, skip
        resolved = _resolve_extends(raw, raw_by_name, source=path, chain=[])
        result.append(_component_from_dict(resolved, source=path))
    return result


def _resolve_extends(
    raw: dict[str, Any],
    index: dict[str, Any],
    source: Path,
    chain: list[str],
) -> dict[str, Any]:
    """Return a raw dict with all 'extends' fields merged (child overrides parent)."""
    name = raw.get("name", "<unknown>")
    if name in chain:
        raise SerializationError(
            f"{source}: circular extends detected: {' -> '.join(chain + [name])}"
        )
    parent_name = raw.get("extends")
    if parent_name is None:
        return raw
    if not isinstance(parent_name, str):
        raise SerializationError(f"{source}: 'extends' must be a string in {name!r}")
    parent_raw = index.get(parent_name)
    if parent_raw is None:
        raise SerializationError(
            f"{source}: {name!r} extends unknown template {parent_name!r}"
        )
    # Recursively resolve the parent first.
    resolved_parent = _resolve_extends(parent_raw, index, source, chain + [name])
    # Child fields override parent; 'extends' key is dropped.
    merged: dict[str, Any] = {**resolved_parent, **raw}
    merged.pop("extends", None)
    # pin_names: child overrides individual entries (not whole dict replacement).
    parent_pins: dict[str, Any] = resolved_parent.get("pin_names") or {}
    child_pins: dict[str, Any] = raw.get("pin_names") or {}
    if parent_pins or child_pins:
        merged["pin_names"] = {**parent_pins, **child_pins}
    return merged


def _component_from_dict(data: Any, source: Path) -> ComponentDefinition:
    if not isinstance(data, dict):
        raise SerializationError(f"{source}: each component must be a mapping")
    if "name" not in data or not isinstance(data["name"], str):
        raise SerializationError(f"{source}: component requires string 'name'")
    if "footprint" not in data:
        raise SerializationError(
            f"{source}: component {data.get('name')!r} requires 'footprint'"
        )
    try:
        footprint = _footprint_from_dict(data["footprint"])
    except (TypeError, ValueError) as exc:
        raise SerializationError(
            f"{source}: invalid footprint for {data['name']!r}: {exc}"
        ) from exc

    pin_names_raw = data.get("pin_names") or {}
    if not isinstance(pin_names_raw, dict):
        raise SerializationError(
            f"{source}: pin_names for {data['name']!r} must be a mapping"
        )
    pin_names: dict[int, str] = {}
    for k, v in pin_names_raw.items():
        try:
            pin_names[int(k)] = str(v)
        except (TypeError, ValueError) as exc:
            raise SerializationError(
                f"{source}: pin_names key {k!r} is not an integer"
            ) from exc

    metadata_raw = data.get("metadata") or {}
    if not isinstance(metadata_raw, dict):
        raise SerializationError(
            f"{source}: metadata for {data['name']!r} must be a mapping"
        )

    raw_class = str(data.get("component_class", "other"))
    try:
        comp_class = ComponentClass(raw_class)
    except ValueError:
        raise SerializationError(
            f"{source}: unknown component_class {raw_class!r} for {data['name']!r}"
        )

    # bendable_pins: {pin_number: [[dx, dy], ...]}
    from protoboard.geometry import Point as _Point
    bendable_raw = data.get("bendable_pins") or {}
    bendable_pins: dict[int, list[Any]] = {}
    if isinstance(bendable_raw, dict):
        for k, offsets in bendable_raw.items():
            try:
                pin_num = int(k)
            except (TypeError, ValueError) as exc:
                raise SerializationError(
                    f"{source}: bendable_pins key {k!r} is not an integer"
                ) from exc
            if not isinstance(offsets, list):
                raise SerializationError(
                    f"{source}: bendable_pins[{pin_num}] must be a list"
                )
            parsed: list[Any] = []
            for off in offsets:
                if isinstance(off, dict):
                    parsed.append(_Point(float(off["x"]), float(off["y"])))
                elif isinstance(off, (list, tuple)) and len(off) == 2:
                    parsed.append(_Point(float(off[0]), float(off[1])))
                else:
                    raise SerializationError(
                        f"{source}: invalid offset {off!r} in bendable_pins[{pin_num}]"
                    )
            bendable_pins[pin_num] = parsed

    try:
        return ComponentDefinition(
            name=data["name"],
            footprint=footprint,
            pin_names=pin_names,
            description=str(data.get("description", "")),
            component_class=comp_class,
            metadata=dict(metadata_raw),
            bendable_pins=bendable_pins,
        )
    except ValueError as exc:
        raise SerializationError(
            f"{source}: invalid component {data['name']!r}: {exc}"
        ) from exc


def _footprint_from_dict(data: Any) -> FootprintTemplate:
    if not isinstance(data, dict):
        raise SerializationError("footprint must be a mapping")
    kind = data.get("type")
    if not isinstance(kind, str):
        raise SerializationError("footprint requires string 'type'")
    builder = _FOOTPRINT_BUILDERS.get(kind)
    if builder is None:
        raise SerializationError(f"unknown footprint type: {kind}")
    return builder(data)


def _build_dip(data: dict[str, Any]) -> FootprintTemplate:
    if "pins" not in data:
        raise SerializationError("DIP footprint requires 'pins'")
    return DIP(
        n=int(data["pins"]),
        pitch=float(data.get("pitch", 1.0)),
        rows_spacing=float(data.get("rows_spacing", 3.0)),
    )


def _build_sil(data: dict[str, Any]) -> FootprintTemplate:
    if "pins" not in data:
        raise SerializationError("SIL footprint requires 'pins'")
    return SIL(n=int(data["pins"]), pitch=float(data.get("pitch", 1.0)))


def _build_dual_row(data: dict[str, Any]) -> FootprintTemplate:
    if "n_per_row" not in data:
        raise SerializationError("DualRow requires 'n_per_row'")
    return DualRow(
        n_per_row=int(data["n_per_row"]),
        pitch=float(data.get("pitch", 1.0)),
        rows_spacing=float(data.get("rows_spacing", 3.0)),
    )


def _build_single_row(data: dict[str, Any]) -> FootprintTemplate:
    if "n" not in data:
        raise SerializationError("SingleRow requires 'n'")
    return SingleRow(n=int(data["n"]), pitch=float(data.get("pitch", 1.0)))


def _build_grid(data: dict[str, Any]) -> FootprintTemplate:
    if "rows" not in data or "cols" not in data:
        raise SerializationError("GridFootprint requires 'rows' and 'cols'")
    return GridFootprint(
        rows=int(data["rows"]),
        cols=int(data["cols"]),
        pitch=float(data.get("pitch", 1.0)),
    )


def _build_to92(_: dict[str, Any]) -> FootprintTemplate:
    return TO92


def _build_to220(_: dict[str, Any]) -> FootprintTemplate:
    return TO220


_FOOTPRINT_BUILDERS: dict[str, Callable[[dict[str, Any]], FootprintTemplate]] = {
    "DIP": _build_dip,
    "SIL": _build_sil,
    "DualRow": _build_dual_row,
    "SingleRow": _build_single_row,
    "Grid": _build_grid,
    "GridFootprint": _build_grid,
    "TO92": _build_to92,
    "TO220": _build_to220,
}


__all__ = ["ComponentLibrary", "load_library"]
