# SPDX-License-Identifier: MIT
"""Package definition loader — YAML mappings → Cylinder/Prism instances.

Pure data transformation (no file I/O here; callers read YAML into dicts). Handles
``extends`` (dict merge with monotonic closing), ``null`` = required, the three
``direction`` forms, run-length offsets, and ``{unit, value}`` body offsets.
Dispatch to a shape class is by the ``shape`` field (registry), NOT by the
``extends`` chain (see ``docs/footprints/spec.md``).
"""

from __future__ import annotations

from typing import Any

from protoboard.domain.components.package.offsets import (
    expand_direction,
    expand_offsets,
    scale_offsets,
)
from protoboard.domain.components.package.shapes import (
    BOARD_DEFAULT,
    BOARD_SOLVE,
    SHAPE_REGISTRY,
    Cylinder,
    Leads,
    Package,
    Prism,
)
from protoboard.geometry import Insets, Point
from protoboard.services.debug_trace import traced

PackageDict = dict[str, Any]


class PackageDefError(ValueError):
    """Raised when a package definition is malformed or incompletely closed."""


# --------------------------------------------------------------------------- #
# extends resolution (data merge)
# --------------------------------------------------------------------------- #

def _deep_merge(base: PackageDict, override: PackageDict) -> PackageDict:
    """Merge *override* onto *base*. Nested dicts merge; scalars/lists replace.

    Monotonic closing: an override may fill a ``null`` (required) but the merge
    itself does not forbid re-setting — that policy is documented; here we just
    let the child value win.
    """
    out: PackageDict = dict(base)
    for key, val in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(val, dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def resolve_extends(name: str, registry: dict[str, PackageDict]) -> PackageDict:
    """Flatten a package's ``extends`` chain into a single merged dict."""
    seen: set[str] = set()

    def _resolve(n: str) -> PackageDict:
        if n in seen:
            raise PackageDefError(f"cyclic extends involving {n!r}")
        seen.add(n)
        if n not in registry:
            raise PackageDefError(f"unknown package {n!r} in extends")
        node = registry[n]
        parent_name = node.get("extends")
        if parent_name is None:
            return dict(node)
        parent = _resolve(str(parent_name))
        merged = _deep_merge(parent, node)
        merged.pop("extends", None)
        return merged

    return _resolve(name)


# --------------------------------------------------------------------------- #
# Leads construction
# --------------------------------------------------------------------------- #

def _require(value: object, field_name: str, pkg: str) -> object:
    if value is None:
        raise PackageDefError(f"{pkg}: required field {field_name!r} left null")
    return value


def _require_float(value: object, field_name: str, pkg: str) -> float:
    if value is None:
        raise PackageDefError(f"{pkg}: required field {field_name!r} left null")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PackageDefError(f"{pkg}: {field_name!r} must be a number, got {value!r}")
    return float(value)


def _body_offset_to_mm(raw: object, pkg: str) -> list[Point]:
    """Return body_offset step vectors scaled to mm.

    Accepts ``{unit: U, value: [...]}`` or a bare list (unit defaults to 1.0)
    or a scalar/single-vector sugar handled by expand_offsets.
    """
    unit = 1.0
    value: object = raw
    if isinstance(raw, dict):
        unit = float(raw.get("unit", 1.0))
        value = _require(raw.get("value"), "body_offset.value", pkg)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # scalar s → single pair [[s, 0]]
        steps = expand_offsets([[float(value), 0]])
    else:
        steps = expand_offsets(value)  # type: ignore[arg-type]
    return scale_offsets(steps, unit)


def _board_offset(raw: object) -> tuple[Point, ...] | str:
    if isinstance(raw, str):
        if raw not in (BOARD_DEFAULT, BOARD_SOLVE):
            raise PackageDefError(f"unknown board_offset sentinel {raw!r}")
        return raw
    return tuple(expand_offsets(raw))  # type: ignore[arg-type]


def build_leads(merged: PackageDict, pkg: str) -> Leads:
    leads_raw = merged.get("leads")
    if not isinstance(leads_raw, dict):
        raise PackageDefError(f"{pkg}: missing 'leads' block")

    body_raw = _require(leads_raw.get("body_offset"), "leads.body_offset", pkg)
    body_mm = _body_offset_to_mm(body_raw, pkg)
    pin_count = len(body_mm) + 1

    direction = expand_direction(
        _require(leads_raw.get("direction"), "leads.direction", pkg), pin_count
    )

    board_raw = leads_raw.get("board_offset", BOARD_DEFAULT)

    return Leads(
        direction=tuple(direction),
        body_offset_mm=tuple(body_mm),
        board_offset=_board_offset(board_raw),
        lead_length_mm=_opt_float(leads_raw.get("lead_length")),
        lead_r_mm=_opt_float(leads_raw.get("lead_r")),
        body_eccentricity_mm=float(leads_raw.get("body_eccentricity", 0.0)),
    )


def _opt_float(value: object) -> float | None:
    return None if value is None else float(value)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Shape instantiation (dispatch by `shape`)
# --------------------------------------------------------------------------- #

def _margin_insets(raw: object) -> Insets | None:
    """CSS-style margin: 1 | 2 (V H) | 3 (T H B) | 4 (T R B L) values (mm)."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        v = float(raw)
        return Insets(v, v, v, v)
    if not isinstance(raw, (list, tuple)):
        raise PackageDefError(f"margin must be a number or list, got {raw!r}")
    vals = [float(x) for x in raw]
    if len(vals) == 1:
        return Insets.all(vals[0])
    if len(vals) == 2:
        return Insets(vals[0], vals[1], vals[0], vals[1])
    if len(vals) == 3:
        return Insets(vals[0], vals[1], vals[2], vals[1])
    if len(vals) == 4:
        return Insets(vals[0], vals[1], vals[2], vals[3])
    raise PackageDefError(f"margin needs 1-4 values, got {len(vals)}")


@traced("build_package")
def build_package(merged: PackageDict, pkg: str) -> Package:
    shape = merged.get("shape")
    if shape not in SHAPE_REGISTRY:
        raise PackageDefError(f"{pkg}: unknown or missing shape {shape!r}")
    leads = build_leads(merged, pkg)
    dims = merged.get("dims") or {}
    bend = float(merged.get("bend", 0.0))

    if shape == "cylinder":
        return Cylinder(
            diameter_mm=_require_float(dims.get("diameter"), "dims.diameter", pkg),
            length_mm=_require_float(dims.get("length"), "dims.length", pkg),
            leads=leads,
            bend_holes=bend,
            edge_radius_mm=float(dims.get("edge_radius", 0.0)),
        )
    # prism
    margin = _margin_insets(merged.get("margin"))
    return Prism(
        leads=leads,
        width_mm=_opt_float(dims.get("width")),
        length_mm=_opt_float(dims.get("length")),
        height_mm=_opt_float(dims.get("height")),
        bend_holes=bend,
        corner_radius_mm=float(dims.get("corner_radius", 0.0)),
        margin_mm=margin,
    )


@traced("load_packages")
def load_packages(raw_list: list[PackageDict]) -> dict[str, Package]:
    """Build all non-abstract packages from a list of raw definitions."""
    registry = {str(d["name"]): d for d in raw_list if "name" in d}
    out: dict[str, Package] = {}
    for name, node in registry.items():
        if node.get("abstract"):
            continue
        merged = resolve_extends(name, registry)
        out[name] = build_package(merged, name)
    return out
