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
    cumulative_positions,
    expand_direction,
    expand_offsets,
    scale_offsets,
)
from protoboard.domain.components.package.body import (
    DiscProfile,
    EdgeStyle,
    LeadArrangement,
    MountStyle,
)
from protoboard.domain.components.package.shapes import (
    BOARD_DEFAULT,
    BOARD_SOLVE,
    SHAPE_REGISTRY,
    Cylinder,
    Disc,
    Leads,
    Package,
    Prism,
)
from protoboard.domain.components.package.solver import RASTER_MM
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


def _board_steps_in_holes(
    board_offset: tuple[Point, ...] | str, body_mm: list[Point]
) -> list[Point]:
    """Default board layout pin→pin step vectors, in holes (grid domain).

    ``default`` reads body_offset in the grid domain (mm / raster); an explicit
    list is already in holes. ``solve`` has no a-priori layout — the caller
    rejects ``body_axis_pos`` for that case before reaching here.
    """
    if board_offset == BOARD_DEFAULT:
        return [Point(v.x / RASTER_MM, v.y / RASTER_MM) for v in body_mm]
    if isinstance(board_offset, str):  # BOARD_SOLVE
        raise PackageDefError("board_offset 'solve' has no default layout")
    return list(board_offset)


def _geo_mid_holes(board_offset: tuple[Point, ...] | str, body_mm: list[Point]) -> Point:
    """Geometric centre of the default on-board lead axis, in holes (pin1=[0,0])."""
    positions = cumulative_positions(_board_steps_in_holes(board_offset, body_mm))
    n = len(positions)
    return Point(
        sum(p.x for p in positions) / n,
        sum(p.y for p in positions) / n,
    )


def _as_point(raw: object, field_name: str, pkg: str) -> Point:
    """Coerce a scalar (sugar → ``(s, 0)``) or a 2-vector into a Point."""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return Point(float(raw), 0.0)
    if (
        isinstance(raw, (list, tuple))
        and len(raw) == 2
        and all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in raw)
    ):
        return Point(float(raw[0]), float(raw[1]))
    raise PackageDefError(f"{pkg}: {field_name!r} must be a number or [x, y], got {raw!r}")


def _resolve_eccentricity(
    leads_raw: PackageDict,
    board_offset: tuple[Point, ...] | str,
    body_mm: list[Point],
    pkg: str,
) -> Point:
    """Lead-exit eccentricity vs body axis as a 2D vector (mm).

    Accepts either the relative ``body_eccentricity`` (scalar sugar or vector,
    already in mm) or the absolute ``body_axis_pos`` (position of the body axis
    vs origin/pin-1, in the grid domain). The two are mutually exclusive. From
    the absolute form: ``ecc = (body_axis_pos − geo_mid) · raster``, where
    ``geo_mid`` is the centre of the default on-board lead axis (holes).
    """
    ecc_raw = leads_raw.get("body_eccentricity")
    axis_raw = leads_raw.get("body_axis_pos")

    if ecc_raw is not None and axis_raw is not None:
        raise PackageDefError(
            f"{pkg}: give either 'body_eccentricity' or 'body_axis_pos', not both"
        )
    if axis_raw is not None:
        if board_offset == BOARD_SOLVE:
            raise PackageDefError(
                f"{pkg}: 'body_axis_pos' needs a default board layout; "
                f"board_offset 'solve' has none — use 'body_eccentricity'"
            )
        axis_pos = _as_point(axis_raw, "body_axis_pos", pkg)
        geo_mid = _geo_mid_holes(board_offset, body_mm)
        return Point(
            (axis_pos.x - geo_mid.x) * RASTER_MM,
            (axis_pos.y - geo_mid.y) * RASTER_MM,
        )
    if ecc_raw is not None:
        return _as_point(ecc_raw, "body_eccentricity", pkg)
    return Point(0.0, 0.0)


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

    board_offset = _board_offset(leads_raw.get("board_offset", BOARD_DEFAULT))

    return Leads(
        direction=tuple(direction),
        body_offset_mm=tuple(body_mm),
        board_offset=board_offset,
        lead_length_mm=_opt_float(leads_raw.get("lead_length")),
        lead_r_mm=_opt_float(leads_raw.get("lead_r")),
        body_eccentricity_mm=_resolve_eccentricity(leads_raw, board_offset, body_mm, pkg),
    )


def _opt_float(value: object) -> float | None:
    return None if value is None else float(value)  # type: ignore[arg-type]


def _flat_normal(raw: object, pkg: str) -> Point:
    """Parse a ``dims.flat_normal`` ``[x, y]`` list into a Point.

    None → (0, 0) (no flat side). Any other shape is a package error.
    """
    if raw is None:
        return Point(0.0, 0.0)
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise PackageDefError(
            f"{pkg}: flat_normal must be a 2-element [x, y] list, got {raw!r}"
        )
    return Point(float(raw[0]), float(raw[1]))


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


def _arrangement(raw: object, default: LeadArrangement, pkg: str) -> LeadArrangement:
    if raw is None:
        return default
    try:
        return LeadArrangement(str(raw))
    except ValueError:
        raise PackageDefError(f"{pkg}: unknown lead arrangement {raw!r}")


def _edge(raw: object, default: EdgeStyle, pkg: str) -> EdgeStyle:
    if raw is None:
        return default
    try:
        return EdgeStyle(str(raw))
    except ValueError:
        raise PackageDefError(f"{pkg}: unknown edge style {raw!r}")


def _disc_profile(raw: object, pkg: str) -> DiscProfile:
    if raw is None:
        return DiscProfile.FLAT
    try:
        return DiscProfile(str(raw))
    except ValueError:
        raise PackageDefError(f"{pkg}: unknown disc profile {raw!r}")


def _mount_style(raw: object, pkg: str) -> MountStyle:
    if raw is None:
        return MountStyle.THROUGH_HOLE
    try:
        return MountStyle(str(raw))
    except ValueError:
        raise PackageDefError(f"{pkg}: unknown mount style {raw!r}")


def _mount_points(raw: object, pkg: str) -> tuple[Point, ...]:
    """Pin-2 mount-point offsets (holes) — a list of [dx, dy] vectors, or empty.

    Empty means the surface-mount default (8 king-move neighbours); a package
    may list its own subset (e.g. orthogonal-only)."""
    if raw is None:
        return ()
    try:
        return tuple(expand_offsets(raw))  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise PackageDefError(f"{pkg}: invalid mount_points: {exc}") from exc


def _color(raw: object) -> str | None:
    return None if raw is None else str(raw)


@traced("build_package")
def build_package(merged: PackageDict, pkg: str) -> Package:
    shape = merged.get("shape")
    if shape not in SHAPE_REGISTRY:
        raise PackageDefError(f"{pkg}: unknown or missing shape {shape!r}")
    leads = build_leads(merged, pkg)
    dims = merged.get("dims") or {}
    bend = float(merged.get("bend", 0.0))
    color = _color(merged.get("color"))
    edge_raw = merged.get("edge")

    if shape == "cylinder":
        flat_r = dims.get("flat_radius", dims.get("flat_r"))
        return Cylinder(
            diameter_mm=_require_float(dims.get("diameter"), "dims.diameter", pkg),
            length_mm=_require_float(dims.get("length"), "dims.length", pkg),
            leads=leads,
            bend_holes=bend,
            edge_radius_mm=float(dims.get("edge_radius", 0.0)),
            arrangement=_arrangement(
                merged.get("arrangement"), LeadArrangement.AXIAL, pkg,
            ),
            edge=_edge(edge_raw, EdgeStyle.ROUNDED, pkg),
            color=color,
            flat_radius_mm=_opt_float(flat_r),
            flat_normal=_flat_normal(dims.get("flat_normal"), pkg),
        )
    if shape == "disc":
        return Disc(
            diameter_mm=_require_float(dims.get("diameter"), "dims.diameter", pkg),
            thickness_mm=_require_float(dims.get("thickness"), "dims.thickness", pkg),
            leads=leads,
            bend_holes=bend,
            profile=_disc_profile(merged.get("disc_profile"), pkg),
            arrangement=_arrangement(
                merged.get("arrangement"), LeadArrangement.RADIAL, pkg,
            ),
            edge=_edge(edge_raw, EdgeStyle.ROUNDED, pkg),
            color=color,
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
        arrangement=_arrangement(
            merged.get("arrangement"), LeadArrangement.AXIAL, pkg,
        ),
        edge=_edge(edge_raw, EdgeStyle.SHARP, pkg),
        color=color,
        mount=_mount_style(merged.get("mount"), pkg),
        mount_points=_mount_points(merged.get("mount_points"), pkg),
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
