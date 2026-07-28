# SPDX-License-Identifier: MIT
"""Package definition loader — YAML mappings → Cylinder/Prism instances.

Pure data transformation (no file I/O here; callers read YAML into dicts). Handles
``extends`` (dict merge with monotonic closing), ``null`` = required, the three
``direction`` forms, run-length offsets, and ``{unit, value}`` body offsets.
Dispatch to a shape class is by the ``shape`` field (registry), NOT by the
``extends`` chain (see ``docs/footprints/package-model.md``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import structlog

from protoboard.domain.components.package.body import (
    DiscProfile,
    EdgeStyle,
    LeadArrangement,
    MountStyle,
)
from protoboard.domain.components.package.offsets import (
    cumulative_positions,
    expand_direction,
    expand_offsets,
    scale_offsets,
)
from protoboard.domain.components.package.schema import (
    AUTO,
    ParamSchema,
    SchemaError,
    parse_schema,
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
from protoboard.geometry import Insets, Point, Vector
from protoboard.services.debug_trace import traced

_log = structlog.get_logger(__name__)

PackageDict = dict[str, Any]


class PackageDefError(ValueError):
    """Raised when a package definition is malformed or incompletely closed."""


# --------------------------------------------------------------------------- #
# extends resolution (data merge)
# --------------------------------------------------------------------------- #


def deep_merge(base: PackageDict, override: PackageDict) -> PackageDict:
    """Merge *override* onto *base*. Nested dicts merge; scalars/lists replace.

    Monotonic closing: an override may fill a ``null`` (required) but the merge
    itself does not forbid re-setting — that policy is documented; here we just
    let the child value win.
    """
    out: PackageDict = dict(base)
    for key, val in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(val, dict):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def resolve_extends(name: str, registry: dict[str,
                                              PackageDict]) -> PackageDict:
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
        merged = deep_merge(parent, node)
        merged.pop("extends", None)
        return merged

    return _resolve(name)


def extends_chain(name: str, registry: dict[str, PackageDict]) -> tuple[str, ...]:
    """The ``extends`` ancestry of *name*, root-first (root, …, name).

    Mirrors the walk in :func:`resolve_extends` (including its cycle guard) but
    collects the chain of *names* instead of merging values. Feeds the package
    ``lineage`` used by renderer dispatch.
    """
    chain: list[str] = []
    seen: set[str] = set()
    n: str | None = name
    while n is not None:
        if n in seen:
            raise PackageDefError(f"cyclic extends involving {n!r}")
        seen.add(n)
        if n not in registry:
            raise PackageDefError(f"unknown package {n!r} in extends")
        chain.append(n)
        parent = registry[n].get("extends")
        n = None if parent is None else str(parent)
    return tuple(reversed(chain))


# --------------------------------------------------------------------------- #
# Leads construction
# --------------------------------------------------------------------------- #


def _require(value: object, field_name: str, pkg: str) -> object:
    if value is None:
        raise PackageDefError(
            f"{pkg}: required field {field_name!r} left null")
    return value


def _require_float(value: object, field_name: str, pkg: str) -> float:
    if value is None:
        raise PackageDefError(
            f"{pkg}: required field {field_name!r} left null")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PackageDefError(
            f"{pkg}: {field_name!r} must be a number, got {value!r}")
    return float(value)


def _body_offset_to_mm(raw: object, pkg: str) -> list[Vector]:
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


def _board_offset(raw: object) -> tuple[Vector, ...] | str:
    if isinstance(raw, str):
        if raw not in (BOARD_DEFAULT, BOARD_SOLVE):
            raise PackageDefError(f"unknown board_offset sentinel {raw!r}")
        return raw
    return tuple(expand_offsets(raw))  # type: ignore[arg-type]


def _board_steps_in_holes(board_offset: tuple[Vector, ...] | str,
                          body_mm: list[Vector]) -> list[Vector]:
    """Default board layout pin→pin step vectors, in holes (grid domain).

    ``default`` reads body_offset in the grid domain (mm / raster); an explicit
    list is already in holes. ``solve`` has no a-priori layout — the caller
    rejects ``body_axis_pos`` for that case before reaching here.
    """
    if board_offset == BOARD_DEFAULT:
        return [Vector(v.dx / RASTER_MM, v.dy / RASTER_MM) for v in body_mm]
    if isinstance(board_offset, str):  # BOARD_SOLVE
        raise PackageDefError("board_offset 'solve' has no default layout")
    return list(board_offset)


def _geo_mid_holes(board_offset: tuple[Vector, ...] | str,
                   body_mm: list[Vector]) -> Point:
    """Geometric centre of the default on-board lead axis, in holes (pin1=[0,0])."""
    positions = cumulative_positions(
        _board_steps_in_holes(board_offset, body_mm))
    n = len(positions)
    return Point(
        sum(p.x for p in positions) / n,
        sum(p.y for p in positions) / n,
    )


def _as_vector(raw: object, field_name: str, pkg: str) -> Vector:
    """Coerce a scalar (sugar → ``(s, 0)``) or a 2-vector into a Vector."""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return Vector(float(raw), 0.0)
    if (isinstance(raw, (list, tuple)) and len(raw) == 2 and all(
            isinstance(c, (int, float)) and not isinstance(c, bool)
            for c in raw)):
        return Vector(float(raw[0]), float(raw[1]))
    raise PackageDefError(
        f"{pkg}: {field_name!r} must be a number or [x, y], got {raw!r}")


def _resolve_eccentricity(
    leads_raw: PackageDict,
    board_offset: tuple[Vector, ...] | str,
    body_mm: list[Vector],
    pkg: str,
) -> Vector:
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
                f"board_offset 'solve' has none — use 'body_eccentricity'")
        axis_pos = _as_vector(axis_raw, "body_axis_pos", pkg)
        geo_mid = _geo_mid_holes(board_offset, body_mm)
        return Vector(
            (axis_pos.dx - geo_mid.x) * RASTER_MM,
            (axis_pos.dy - geo_mid.y) * RASTER_MM,
        )
    if ecc_raw is not None:
        return _as_vector(ecc_raw, "body_eccentricity", pkg)
    return Vector(0.0, 0.0)


def build_leads(merged: PackageDict, pkg: str) -> Leads:
    leads_raw = merged.get("leads")
    if not isinstance(leads_raw, dict):
        raise PackageDefError(f"{pkg}: missing 'leads' block")

    body_raw = _require(leads_raw.get("body_offset"), "leads.body_offset", pkg)
    body_mm = _body_offset_to_mm(body_raw, pkg)
    pin_count = len(body_mm) + 1

    direction = expand_direction(
        _require(leads_raw.get("direction"), "leads.direction", pkg),
        pin_count)

    board_offset = _board_offset(leads_raw.get("board_offset", BOARD_DEFAULT))

    return Leads(
        direction=tuple(direction),
        body_offset_mm=tuple(body_mm),
        board_offset=board_offset,
        lead_length_mm=_opt_float(leads_raw.get("lead_length")),
        lead_r_mm=_opt_float(leads_raw.get("lead_r")),
        body_eccentricity_mm=_resolve_eccentricity(leads_raw, board_offset,
                                                   body_mm, pkg),
    )


def _opt_float(value: object) -> float | None:
    return None if value is None else float(value)  # type: ignore[arg-type]


def _flat_normal(raw: object, pkg: str) -> Vector:
    """Parse a ``dims.flat_normal`` ``[x, y]`` list into a Vector.

    None → (0, 0) (no flat side). Any other shape is a package error.
    """
    if raw is None:
        return Vector(0.0, 0.0)
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise PackageDefError(
            f"{pkg}: flat_normal must be a 2-element [x, y] list, got {raw!r}")
    return Vector(float(raw[0]), float(raw[1]))


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


def _arrangement(raw: object, default: LeadArrangement,
                 pkg: str) -> LeadArrangement:
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
        return tuple(
            Point(v.dx, v.dy)
            for v in expand_offsets(raw))  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise PackageDefError(f"{pkg}: invalid mount_points: {exc}") from exc


def _color(raw: object) -> str | None:
    return None if raw is None else str(raw)


def _normalise_body_offset(filled: PackageDict, pkg: str) -> None:
    """Unwrap a ``{unit, value}`` ``leads.body_offset`` to a bare mm step-list.

    The leads schema (proposal §4) declares ``body_offset`` as a scalar or a
    bare offset list — it has no ``{unit, value}`` branch (§5b: keep the matcher
    shape-only). So before :meth:`ParamSchema.validate` sees the value, fold the
    wrapper away here: scale by ``unit`` into mm exactly as ``build_leads`` would
    (via the same :func:`_body_offset_to_mm`), and write the resulting bare list
    back. ``build_leads`` then reads it with the default ``unit = 1.0`` and
    arrives at the identical mm — the unwrap is value-preserving. Mutates
    *filled* in place; a no-op unless ``leads.body_offset`` is a mapping.
    """
    leads = filled.get("leads")
    if not isinstance(leads, dict):
        return
    body_raw = leads.get("body_offset")
    if not isinstance(body_raw, dict):
        return
    body_mm = _body_offset_to_mm(body_raw, pkg)
    leads["body_offset"] = [[v.dx, v.dy] for v in body_mm]


def _check_no_auto(tree: PackageDict, pkg: str) -> None:
    """Reject any ``auto`` slot still unresolved after the generator step.

    ``auto`` means "a kind generator computes this" (package-model.md §5.4); a
    value tree entering shape build with one left is a definition error — the
    node is bound to no generator, or the generator does not fill that slot.
    The dotted paths make the message actionable.
    """
    stuck: list[str] = []

    def _walk(node: object, path: str) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                _walk(val, f"{path}.{key}" if path else str(key))
        elif node == AUTO:
            stuck.append(path)

    _walk({k: v for k, v in tree.items() if k != "schema"}, "")
    if stuck:
        raise PackageDefError(
            f"{pkg}: 'auto' left unresolved at {', '.join(sorted(stuck))} — "
            f"a kind generator must fill these before the body can build")


# A kind generator's value transform: fills ``auto`` slots in the (defaults-
# filled) value tree with parameter-derived values. Plain values-in/values-out
# so the loader stays generator-agnostic — ``kind.py`` builds the closure.
ResolveAuto = Callable[[PackageDict], PackageDict]


def _apply_schema(
    merged: PackageDict,
    pkg: str,
    resolve_auto: ResolveAuto | None = None,
) -> tuple[PackageDict, ParamSchema | None, dict[str, Any]]:
    """Fill defaults, check required, and type-check declared fields.

    Reads the ``schema:`` block (path-mirrored field declarations), applies it to
    the value tree, and returns ``(filled_values, schema, raw_values)`` — the
    value tree with defaults filled and the ``schema`` key dropped, the parsed
    :class:`ParamSchema` (or ``None`` when there is no ``schema:``), and the
    *original* resolved value tree for the UI (before the body_offset unwrap, so
    the Properties panel keeps the authored ``{unit, value}`` form). The shape
    build stamps schema + raw_values onto the body so the resolved definition
    survives for the UI. Validation is loose: a value with no schema leaf is
    untouched. The ``auto`` sentinel satisfies ``required`` and is skipped by the
    type check; *resolve_auto* (a kind generator's value transform, when the
    caller has one) runs between the required check and the real type check to
    replace every ``auto`` with a concrete value — ``raw_values`` are captured
    earlier, so the UI keeps seeing ``auto`` (read-only, per §5.3/§5.4). Any
    ``auto`` still left afterwards is a loud :class:`PackageDefError`.
    """
    raw_schema = merged.get("schema")
    if raw_schema is None:
        if resolve_auto is not None:
            merged = resolve_auto(merged)
        _check_no_auto(merged, pkg)
        return merged, None, _resolved_values(merged)
    try:
        schema = parse_schema(raw_schema)
        filled = schema.apply_to(merged)
        raw_values = _resolved_values(filled)  # before the body_offset unwrap
        schema.check_required(filled)
        if resolve_auto is not None:
            filled = resolve_auto(filled)      # §5.4: generator fills ``auto``
        _check_no_auto(filled, pkg)
        _normalise_body_offset(filled, pkg)    # §5b: fold {unit,value} → mm list
        schema.validate(filled)
    except SchemaError as exc:
        raise PackageDefError(f"{pkg}: {exc}") from exc
    filled.pop("schema", None)
    return filled, schema, raw_values


def _resolved_values(merged: PackageDict) -> dict[str, Any]:
    """The resolved value tree the schema describes (``dims`` + ``leads``).

    Paired with a body's ``schema``, ``schema.fields_view(raw_values)`` drives the
    Properties panel. Geometry never reads this; it is metadata for the UI."""
    out: dict[str, Any] = {}
    for key in ("dims", "leads"):
        val = merged.get(key)
        if isinstance(val, dict):
            out[key] = dict(val)
    return out


#: Recognized ``dims:`` keys per shape — the typo-catcher's vocabulary.
_KNOWN_DIMS_KEYS: dict[str, frozenset[str]] = {
    "cylinder": frozenset({
        "diameter", "length", "edge_radius", "flat_radius", "flat_r",
        "flat_normal",
    }),
    "disc": frozenset({"diameter", "thickness"}),
    "prism": frozenset({"width", "length", "height", "corner_radius"}),
}


def _validate_dims(
    dims: dict[str, Any],
    shape: str,
    pkg: str,
    schema: ParamSchema | None,
) -> None:
    """Typo-catcher for an authored ``dims:`` block.

    An unknown key WARNS (dims is an open data bag — never a gate; a family
    schema may declare extra ``dims.*`` fields, those count as known). A
    prism giving exactly ONE of width/length ERRORS: the half pair can never
    derive a body, so the part would silently degrade to the pin-hug
    rectangle at render — the ``langth``-typo class of bug, better refused
    loudly at load (the component is skipped with this message as the hint).
    A dims-less prism stays legal: no dims at all (or an empty block) means
    "derive from pins/margin" — the generators build exactly that.
    """
    known = _KNOWN_DIMS_KEYS.get(shape, frozenset())
    if schema is not None:
        known = known | {
            path.split(".", 1)[1]
            for path in schema.fields
            if path.startswith("dims.")
        }
    for key in dims:
        if key not in known:
            _log.warning(
                "package.unknown_dims_key",
                package=pkg,
                shape=shape,
                key=key,
                known=sorted(known),
            )
    if shape == "prism":
        has_width = dims.get("width") is not None
        has_length = dims.get("length") is not None
        if has_width != has_length:
            missing = "length" if has_width else "width"
            raise PackageDefError(
                f"{pkg}: prism dims gives only one extent — missing "
                f"dims.{missing} (a half pair cannot derive the body and "
                f"would silently degrade to the pin-hug rectangle)")


@traced()
def build_package(
    merged: PackageDict,
    pkg: str,
    *,
    lineage: tuple[str, ...] = (),
    resolve_auto: ResolveAuto | None = None,
) -> Package:
    merged, schema, raw_values = _apply_schema(merged, pkg, resolve_auto)
    shape = merged.get("shape")
    if shape not in SHAPE_REGISTRY:
        raise PackageDefError(f"{pkg}: unknown or missing shape {shape!r}")
    # Lineage is root-first and always contains the shape name, so renderer
    # dispatch can fall back to the per-shape base renderer.  Chains rooted at
    # the abstract ``package`` base already pass through the shape generic
    # (e.g. ``package → prism → prism_radial``) — only prepend when absent.
    if shape not in lineage:
        lineage = (str(shape), *lineage)
    leads = build_leads(merged, pkg)
    dims = merged.get("dims") or {}
    _validate_dims(dims, str(shape), pkg, schema)
    bend = float(merged.get("bend", 0.0))
    color = _color(merged.get("color"))
    edge_raw = merged.get("edge")

    if shape == "cylinder":
        flat_r = dims.get("flat_radius", dims.get("flat_r"))
        return Cylinder(
            diameter_mm=_require_float(dims.get("diameter"), "dims.diameter",
                                       pkg),
            length_mm=_require_float(dims.get("length"), "dims.length", pkg),
            leads=leads,
            bend_holes=bend,
            edge_radius_mm=float(dims.get("edge_radius", 0.0)),
            arrangement=_arrangement(
                merged.get("arrangement"),
                LeadArrangement.AXIAL,
                pkg,
            ),
            edge=_edge(edge_raw, EdgeStyle.ROUNDED, pkg),
            color=color,
            flat_radius_mm=_opt_float(flat_r),
            flat_normal=_flat_normal(dims.get("flat_normal"), pkg),
            schema=schema,
            raw_values=raw_values,
            lineage=lineage,
        )
    if shape == "disc":
        return Disc(
            diameter_mm=_require_float(dims.get("diameter"), "dims.diameter",
                                       pkg),
            thickness_mm=_require_float(dims.get("thickness"),
                                        "dims.thickness", pkg),
            leads=leads,
            bend_holes=bend,
            profile=_disc_profile(merged.get("disc_profile"), pkg),
            arrangement=_arrangement(
                merged.get("arrangement"),
                LeadArrangement.RADIAL,
                pkg,
            ),
            edge=_edge(edge_raw, EdgeStyle.ROUNDED, pkg),
            color=color,
            schema=schema,
            raw_values=raw_values,
            lineage=lineage,
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
            merged.get("arrangement"),
            LeadArrangement.AXIAL,
            pkg,
        ),
        edge=_edge(edge_raw, EdgeStyle.SHARP, pkg),
        color=color,
        mount=_mount_style(merged.get("mount"), pkg),
        mount_points=_mount_points(merged.get("mount_points"), pkg),
        schema=schema,
        raw_values=raw_values,
        lineage=lineage,
    )


@traced()
def load_packages(raw_list: list[PackageDict]) -> dict[str, Package]:
    """Build all non-abstract packages from a list of raw definitions."""
    registry = {str(d["name"]): d for d in raw_list if "name" in d}
    out: dict[str, Package] = {}
    for name, node in registry.items():
        # A ``kind:``-bearing node is a declarative kind (its ``auto`` slots
        # need the generator) — bound via kind_nodes, never built eagerly here.
        if node.get("abstract") or node.get("kind"):
            continue
        merged = resolve_extends(name, registry)
        out[name] = build_package(
            merged, name, lineage=extends_chain(name, registry))
    return out
