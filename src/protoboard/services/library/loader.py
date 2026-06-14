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

from protoboard.domain.components.definition import (
    ComponentClass,
    ComponentDefinition,
    PinBendSpec,
    PinVariant,
)
from protoboard.domain.components.footprints import (
    DIP,
    SIL,
    TO92,
    TO220,
    DualRow,
    FootprintTemplate,
    GridFootprint,
    QuadRow,
    SingleRow,
    TwoPin,
)
from protoboard.domain.components.package import (
    Package,
    PackageDefError,
    PackageFootprint,
    build_package,
    resolve_extends,
)
from protoboard.errors import SerializationError
from protoboard.paths import BUILTIN_ASSETS_DIR
from protoboard.services.debug_trace import traced

import structlog

_log = structlog.get_logger(__name__)
_yaml = YAML(typ="safe")


def _load_builtin_package_defs() -> dict[str, dict[str, Any]]:
    """Raw builtin package definitions (name → dict) from assets/packages/*.yaml.

    These are the generics/named bases a component's inline ``package:`` may
    ``extends``. Cached after first load.
    """
    cached = getattr(_load_builtin_package_defs, "_cache", None)
    if cached is not None:
        return cached  # type: ignore[no-any-return]
    defs: dict[str, dict[str, Any]] = {}
    pkg_dir = BUILTIN_ASSETS_DIR / "packages"
    if pkg_dir.is_dir():
        for yaml_path in sorted(pkg_dir.glob("*.yaml")):
            data = _yaml.load(yaml_path.read_text())
            for d in (data or {}).get("packages", []):
                if isinstance(d, dict) and isinstance(d.get("name"), str):
                    defs[d["name"]] = d
    _load_builtin_package_defs._cache = defs  # type: ignore[attr-defined]
    return defs


@traced("_build_component_package")
def _build_component_package(name: str, raw: dict[str, Any]) -> Package:
    """Build a Package from a component's inline ``package:`` block.

    The block may ``extends`` a builtin generic; we register it as a temporary
    named package alongside the builtins, resolve the extends chain, and build.
    Raises ``PackageDefError`` on a malformed / incompletely-closed definition.
    """
    registry = dict(_load_builtin_package_defs())
    node = {**raw, "name": name}
    registry[name] = node
    merged = resolve_extends(name, registry)
    return build_package(merged, name)


@dataclass
class ComponentLibrary:
    _by_name: dict[str, ComponentDefinition] = field(default_factory=dict)
    _source: dict[str, Path] = field(default_factory=dict)

    def names(self) -> Iterable[str]:
        return self._by_name.keys()

    def get(self, name: str) -> ComponentDefinition:
        return self._by_name[name]

    def source_path(self, name: str) -> Path | None:
        """Return the YAML file that defines *name*, or None if unknown."""
        return self._source.get(name)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._by_name


def load_library(directories: Iterable[Path]) -> ComponentLibrary:
    """Load a library from the given directories. Earlier directories win."""
    lib = ComponentLibrary()
    for directory in directories:
        if not directory.exists():
            raise FileNotFoundError(f"Library directory not found: {directory}")
        for yaml_path in sorted(directory.glob("**/*.yaml")):
            for defn in _load_file(yaml_path):
                # First directory wins → only set if not already present.
                if lib._by_name.setdefault(defn.name, defn) is defn:
                    lib._source[defn.name] = yaml_path
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
        try:
            defn = _component_from_dict(resolved, source=path)
        except PackageDefError as exc:
            # A malformed parametric package must not sink the whole library.
            # Skip this component; the user re-places it with a fresh definition.
            _log.warning(
                "library.package_skipped", component=name, path=str(path), error=str(exc)
            )
            continue
        result.append(defn)

        # aliases: list of extra names that resolve to the same definition.
        for alias in resolved.get("aliases") or []:
            if isinstance(alias, str) and alias:
                alias_defn = ComponentDefinition(
                    name=alias,
                    footprint=defn.footprint,
                    pin_names=defn.pin_names,
                    description=defn.description,
                    component_class=defn.component_class,
                    metadata=defn.metadata,
                    bendable_pins=defn.bendable_pins,
                )
                result.append(alias_defn)

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

    # Parametric package (docs/footprints/spec.md). When present, it is the
    # source of geometry: the footprint is derived via PackageFootprint, and an
    # explicit ``footprint:`` is optional. A malformed package raises
    # PackageDefError, which the caller turns into a skipped component (the user
    # re-places it with a fresh definition) rather than failing the whole load.
    package_obj: Package | None = None
    package_raw = data.get("package")
    if isinstance(package_raw, dict):
        package_obj = _build_component_package(data["name"], package_raw)

    if package_obj is not None:
        footprint: FootprintTemplate = PackageFootprint(package_obj)
    else:
        if "footprint" not in data:
            raise SerializationError(
                f"{source}: component {data.get('name')!r} requires 'footprint' "
                f"or 'package'"
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

    from protoboard.geometry import Point as _Point

    def _parse_point(raw: Any, ctx: str) -> _Point:
        if isinstance(raw, dict):
            return _Point(float(raw["x"]), float(raw["y"]))
        if isinstance(raw, (list, tuple)) and len(raw) == 2:
            return _Point(float(raw[0]), float(raw[1]))
        raise SerializationError(f"{source}: invalid Point {raw!r} in {ctx}")

    bendable_raw = data.get("bendable_pins") or {}
    bendable_pins: dict[int, PinBendSpec] = {}
    if isinstance(bendable_raw, dict):
        for k, spec_raw in bendable_raw.items():
            try:
                pin_num = int(k)
            except (TypeError, ValueError) as exc:
                raise SerializationError(
                    f"{source}: bendable_pins key {k!r} is not an integer"
                ) from exc

            ctx = f"bendable_pins[{pin_num}]"
            if isinstance(spec_raw, list):
                # Format A — lista dyskretnych punktów (TO-92 itp.)
                pts = tuple(_parse_point(off, ctx) for off in spec_raw)
                default = pts[0] if pts else _Point(0.0, 0.0)
                bendable_pins[pin_num] = PinBendSpec(
                    discrete=pts, default_offset=default
                )
            elif isinstance(spec_raw, dict):
                if "min_reach" in spec_raw or "max_reach" in spec_raw:
                    # Format C — zakres 2D swobodny (nowy)
                    import math as _math
                    bendable_pins[pin_num] = PinBendSpec(
                        min_reach=float(spec_raw.get("min_reach", 0.0)),
                        max_reach=float(spec_raw.get("max_reach", 0.0)),
                        default_offset=_parse_point(
                            spec_raw["default"], ctx + ".default"
                        ) if "default" in spec_raw else _Point(0.0, 0.0),
                    )
                elif "min" in spec_raw and "max" in spec_raw:
                    # Format B — stary zakres kierunkowy; konwertuj do reach
                    import math as _math
                    min_pt = _parse_point(spec_raw["min"], ctx + ".min")
                    max_pt = _parse_point(spec_raw["max"], ctx + ".max")
                    bendable_pins[pin_num] = PinBendSpec(
                        min_reach=_math.hypot(min_pt.x, min_pt.y),
                        max_reach=_math.hypot(max_pt.x, max_pt.y),
                        default_offset=_parse_point(
                            spec_raw["default"], ctx + ".default"
                        ) if "default" in spec_raw else _Point(0.0, 0.0),
                    )
                elif "discrete" in spec_raw:
                    pts = tuple(
                        _parse_point(off, ctx) for off in spec_raw["discrete"]
                    )
                    default = (
                        _parse_point(spec_raw["default"], ctx + ".default")
                        if "default" in spec_raw
                        else (pts[0] if pts else _Point(0.0, 0.0))
                    )
                    bendable_pins[pin_num] = PinBendSpec(
                        discrete=pts, default_offset=default
                    )
                else:
                    raise SerializationError(
                        f"{source}: {ctx} dict must have 'min_reach'/'max_reach',"
                        " 'min'+'max', or 'discrete'"
                    )
            else:
                raise SerializationError(
                    f"{source}: {ctx} must be a list or dict,"
                    f" got {type(spec_raw).__name__}"
                )

    variants_raw = data.get("variants") or []
    variants: list[PinVariant] = []
    if isinstance(variants_raw, list):
        for v in variants_raw:
            if not isinstance(v, dict):
                raise SerializationError(
                    f"{source}: each variant for {data['name']!r} must be a mapping"
                )
            if "id" not in v:
                raise SerializationError(
                    f"{source}: variant for {data['name']!r} requires 'id'"
                )
            v_pins: dict[int, str] = {}
            for k, val in (v.get("pin_names") or {}).items():
                try:
                    v_pins[int(k)] = str(val)
                except (TypeError, ValueError) as exc:
                    raise SerializationError(
                        f"{source}: variant pin_names key {k!r} is not an integer"
                    ) from exc
            variants.append(PinVariant(
                id=str(v["id"]),
                label=str(v.get("label", v["id"])),
                pin_names=v_pins,
            ))

    # ``body_variant`` is the YAML-facing key; we accept the longer
    # ``default_body_variant`` as a synonym since that is the field name
    # on ComponentDefinition.
    default_body_variant_raw = data.get("body_variant") or data.get("default_body_variant")
    default_body_variant: str | None = None
    if default_body_variant_raw is not None:
        if not isinstance(default_body_variant_raw, str):
            raise SerializationError(
                f"{source}: body_variant for {data['name']!r} must be a string "
                f"'family/variant_id'; got {default_body_variant_raw!r}"
            )
        default_body_variant = default_body_variant_raw

    # ``package_family_ref`` — package-model successor to ``body_variant``,
    # references the package-family catalogue as ``"family/variant_id"``.
    package_family_ref_raw = data.get("package_family_ref")
    package_family_ref: str | None = None
    if package_family_ref_raw is not None:
        if not isinstance(package_family_ref_raw, str):
            raise SerializationError(
                f"{source}: package_family_ref for {data['name']!r} must be a "
                f"string 'family/variant_id'; got {package_family_ref_raw!r}"
            )
        package_family_ref = package_family_ref_raw

    default_value_raw = data.get("default_value")
    default_value: str = ""
    if default_value_raw is not None:
        if not isinstance(default_value_raw, str):
            raise SerializationError(
                f"{source}: default_value for {data['name']!r} must be a "
                f"string; got {default_value_raw!r}"
            )
        default_value = default_value_raw

    try:
        return ComponentDefinition(
            name=data["name"],
            footprint=footprint,
            pin_names=pin_names,
            description=str(data.get("description", "")),
            component_class=comp_class,
            metadata=dict(metadata_raw),
            bendable_pins=bendable_pins,
            variants=tuple(variants),
            default_body_variant=default_body_variant,
            default_value=default_value,
            package=package_obj,
            package_family_ref=package_family_ref,
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
    n = int(data["pins"])
    pitch = float(data.get("pitch", 1.0))
    # Two-pin SIL is a TwoPin: SingleRow with n=2 carried no axial-vs-
    # radial semantics and lumped resistors, caps, LEDs into the same
    # "two pads in a line" footprint.  Redirect old library entries so
    # the migration is transparent.
    if n == 2:
        return TwoPin(pitch=pitch)
    return SIL(n=n, pitch=pitch)


def _build_two_pin(data: dict[str, Any]) -> FootprintTemplate:
    return TwoPin(pitch=float(data.get("pitch", 1.0)))


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


def _build_quad_row(data: dict[str, Any]) -> FootprintTemplate:
    if "n_per_col" not in data:
        raise SerializationError("QuadRow footprint requires 'n_per_col'")
    raw_spacings = data.get("col_spacings", [1.0, 8.0, 1.0])
    if not isinstance(raw_spacings, (list, tuple)) or len(raw_spacings) != 3:
        raise SerializationError("QuadRow 'col_spacings' must be a list of 3 values")
    return QuadRow(
        n_per_col=int(data["n_per_col"]),
        col_spacings=(float(raw_spacings[0]), float(raw_spacings[1]), float(raw_spacings[2])),
        pitch=float(data.get("pitch", 1.0)),
    )


_FOOTPRINT_BUILDERS: dict[str, Callable[[dict[str, Any]], FootprintTemplate]] = {
    "DIP": _build_dip,
    "SIL": _build_sil,
    "TwoPin": _build_two_pin,
    "DualRow": _build_dual_row,
    "SingleRow": _build_single_row,
    "Grid": _build_grid,
    "GridFootprint": _build_grid,
    "QuadRow": _build_quad_row,
    "TO92": _build_to92,
    "TO220": _build_to220,
}


__all__ = ["ComponentLibrary", "load_library"]
