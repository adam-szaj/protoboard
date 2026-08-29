# SPDX-License-Identifier: MIT
"""Package parameter *schema* — field declarations separated from values.

An abstract base package declares its fields under a top-level ``schema:`` key
whose inner structure mirrors the value tree (``schema.dims.lx`` describes the
value at ``dims.lx``). A field declaration is a leaf dict ``{type, default,
required}``; anything without a ``type`` key is a *section* (a branch grouping
more fields).

This module is the model + parser + validator for that declaration. It does not
touch ``leads`` geometry or build shapes — the loader applies a parsed
:class:`ParamSchema` to the value tree (fill defaults → check required →
validate types) before handing values to ``build_package``.

Validation is **loose**: a value with no matching schema leaf passes through
untouched (see ``docs/footprints/package-model.md`` §5.2). The ``auto`` sentinel
marks a value computed later by a kind generator — it satisfies ``required`` and
is skipped by type validation (the generator replaces it before the type check
that matters runs in the loader).

Pure domain: no Qt, no I/O.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from protoboard.errors import ProtoboardError

# Sentinel written in a value slot to mean "a kind generator computes this".
# Present ⇒ satisfies ``required``; type validation skips it (the generator
# resolves it to a concrete value before the loader's real type check).
AUTO = "auto"

# A type spec is either a single *shape* or a list of allowed shapes (a union).
# A shape is a primitive name (``"float"``, ``"int"``, ``"str"``, ``"bool"``) or
# a tuple/list of shapes describing a fixed-length vector (``[float, float]`` is a
# 2-vector; ``[float, [float, float]]`` is a pair of a scalar and a 2-vector).
# ``type: [float, [float, float], [float, [float, float]]]`` whose *elements are
# themselves shapes* is a union of those three shapes — disambiguated by the rule
# below (a list whose every element is a known shape-or-shape-list is a union).
#
# Four *dict* constructors extend the grammar for the richer ``leads`` fields
# (see ``docs/footprints/package-model.md`` §5.5). A dict is never a plain
# shape, so these compose with the rules above without ambiguity:
#
#   * ``{list: <shape>}``            — a variable-length list whose every element
#     ``{list: <shape>, len: N}``      matches ``<shape>``; an optional integer
#                                      ``len`` also pins the exact length.
#   * ``{seq: [<shape>, ...]}``      — a fixed-length *positional* tuple (element
#                                      *i* matches shape *i*). The unambiguous way
#                                      to write a pair of differing members, e.g.
#                                      run-length ``{seq: [int, [float, float]]}``
#                                      — which as a bare list would be read as the
#                                      union "int OR 2-vector".
#   * ``{oneof: [<spec>, ...]}``     — matches if the value matches any branch;
#                                      the *named* form of the implicit union, so
#                                      a ``list`` element can itself be a union.
#   * ``{const: <value>}``           — matches iff the value equals ``<value>``
#                                      (lets sentinels like ``"default"`` / ``"solve"``
#                                      be declared).
TypeSpec = Any  # structurally recursive; validated, not statically typed.

_PRIMITIVES: dict[str, type | tuple[type, ...]] = {
    # ``bool`` is excluded from ``float``/``int`` on purpose (a YAML ``true`` is
    # not a number) — mirrors the loader's existing ``isinstance(.., bool)`` guards.
    "float": (int, float),
    "int": int,
    "str": str,
    "bool": bool,
}


class SchemaError(ProtoboardError):
    """Raised when a ``schema:`` block or a value validated against it is bad."""


# --------------------------------------------------------------------------- #
# Field declaration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One declared field: its allowed type, default, and required-ness.

    ``type`` is a :data:`TypeSpec` (a shape or a union of shapes). ``default`` is
    used when the value tree omits the field (``None`` here means "no default" —
    distinct from a default *of* ``None``, which the YAML cannot express). A
    field with ``required=True`` and no default must be supplied by the instance
    (the ``auto`` sentinel counts as supplied).
    """

    type: TypeSpec
    default: Any = None
    has_default: bool = False
    required: bool = False

    def validate(self, value: Any, path: str) -> None:
        """Raise :class:`SchemaError` unless *value* matches ``type``.

        The ``auto`` sentinel is always accepted (a generator resolves it later).
        """
        if value == AUTO:
            return
        if not _matches(self.type, value):
            raise SchemaError(
                f"{path}: value {value!r} does not match type {self.type!r}")


# --------------------------------------------------------------------------- #
# Field view (editability derived from the resolved value)
# --------------------------------------------------------------------------- #


class FieldStatus(enum.Enum):
    """Why a field is editable or not, read off the resolved value tree.

    The read-only rule (decided 2026-06-27, package-model.md §5.3): a field is
    **editable iff its resolved value is null** — an authored hole the user must
    fill. Any concrete value (set in YAML, or a filled ``default``) and the
    ``auto`` sentinel (a generator computes it) are read-only. To make a
    defaulted field editable, the package author writes ``null`` for it
    explicitly.
    """

    REQUIRED_EMPTY = "required_empty"  # value is null → RW (a hole to fill)
    SET = "set"                        # a concrete value (incl. default) → RO
    AUTO = "auto"                      # the ``auto`` sentinel → RO (generated)

    @property
    def editable(self) -> bool:
        return self is FieldStatus.REQUIRED_EMPTY


@dataclass(frozen=True, slots=True)
class FieldView:
    """One field as the UI sees it: its declaration, resolved value, editability.

    The shared, schema-derived row that both the Properties panel and the
    place-tool package picker render (one ``FieldView`` per ``schema`` leaf). The
    panel picks an editor from ``spec.type`` and disables it when not
    ``editable``. Pure data — no Qt.
    """

    path: str
    spec: FieldSpec
    value: Any
    status: FieldStatus

    @property
    def editable(self) -> bool:
        return self.status.editable


def _field_status(value: Any) -> FieldStatus:
    """Classify a resolved field value into its editability status."""
    if value is _MISSING or value is None:
        return FieldStatus.REQUIRED_EMPTY
    if value == AUTO:
        return FieldStatus.AUTO
    return FieldStatus.SET


# --------------------------------------------------------------------------- #
# Type matching (union-aware)
# --------------------------------------------------------------------------- #


def _is_bool(v: object) -> bool:
    return isinstance(v, bool)


def _matches_primitive(name: str, value: object) -> bool:
    py = _PRIMITIVES.get(name)
    if py is None:
        raise SchemaError(f"unknown primitive type {name!r}")
    if name in ("float", "int") and _is_bool(value):
        return False
    return isinstance(value, py)


def _matches_shape(spec: TypeSpec, value: object) -> bool:
    """Match a single shape (primitive name or fixed-length vector).

    Vector elements delegate to :func:`_matches`, so a fixed-length shape may
    carry a dict constructor at any position (e.g. the grouped-``direction``
    shape ``[{list: int}, [float, float, float]]`` — an index list paired with a
    3-vector).
    """
    if isinstance(spec, Mapping):
        return _matches(spec, value)
    if isinstance(spec, str):
        return _matches_primitive(spec, value)
    if isinstance(spec, (list, tuple)):
        if not isinstance(value, (list, tuple)) or len(value) != len(spec):
            return False
        return all(_matches(s, v) for s, v in zip(spec, value))
    raise SchemaError(f"malformed type shape {spec!r}")


def _matches_list(spec: Mapping[str, Any], value: object) -> bool:
    """Match the ``{list: <shape>, len?: N}`` variadic-list constructor."""
    inner = spec["list"]
    if not isinstance(value, (list, tuple)):
        return False
    declared_len = spec.get("len")
    if declared_len is not None:
        if not isinstance(declared_len, int) or isinstance(declared_len, bool):
            raise SchemaError(f"'len' must be an int, got {declared_len!r}")
        if len(value) != declared_len:
            return False
    return all(_matches(inner, el) for el in value)


def _matches_seq(spec: Mapping[str, Any], value: object) -> bool:
    """Match the ``{seq: [<shape>, ...]}`` fixed-length heterogeneous tuple.

    Unlike a bare nested list (which the union heuristic reads as "any branch"),
    ``seq`` is an *ordered, positional* match — element *i* of the value matches
    element *i* of the shape. This is the only way to express a pair whose
    members differ, e.g. run-length ``[count, [x, y]]`` as
    ``{seq: [int, [float, float]]}`` (without it ``[int, [float, float]]`` is
    read as the union "int OR 2-vector").
    """
    shapes = spec["seq"]
    if not isinstance(shapes, (list, tuple)):
        raise SchemaError(f"'seq' must be a list, got {shapes!r}")
    if not isinstance(value, (list, tuple)) or len(value) != len(shapes):
        return False
    return all(_matches(s, v) for s, v in zip(shapes, value))


def _matches(spec: TypeSpec, value: object) -> bool:
    """Match *value* against a :data:`TypeSpec` (single shape, union, or a
    ``list`` / ``seq`` / ``oneof`` / ``const`` dict constructor).

    Disambiguation: a list is a *union* when at least one element is itself a
    shape-list (e.g. ``[float, [float, float]]`` — a scalar OR a 2-vector); a
    list whose every element is a *primitive name* is a single vector shape
    (``[float, float]`` — a 2-vector, not a "float-or-float" union). A *dict*
    is one of the named constructors and is never a plain shape.
    """
    if isinstance(spec, Mapping):
        if "list" in spec:
            return _matches_list(spec, value)
        if "seq" in spec:
            return _matches_seq(spec, value)
        if "oneof" in spec:
            branches = spec["oneof"]
            if not isinstance(branches, (list, tuple)):
                raise SchemaError(f"'oneof' must be a list, got {branches!r}")
            return any(_matches(b, value) for b in branches)
        if "const" in spec:
            return bool(value == spec["const"])
        raise SchemaError(
            f"type dict must carry 'list', 'seq', 'oneof', or 'const', "
            f"got {spec!r}")
    if isinstance(spec, str):
        return _matches_primitive(spec, value)
    if isinstance(spec, (list, tuple)):
        has_nested = any(isinstance(s, (list, tuple, Mapping)) for s in spec)
        if has_nested:
            # union of shapes
            return any(_matches_shape(s, value) for s in spec)
        # flat list of primitive names → one vector shape
        return _matches_shape(spec, value)
    raise SchemaError(f"malformed type spec {spec!r}")


# --------------------------------------------------------------------------- #
# Parsing a schema: block
# --------------------------------------------------------------------------- #


def _is_field_leaf(node: object) -> bool:
    """A node is a field declaration iff it is a dict carrying a ``type`` key."""
    return isinstance(node, Mapping) and "type" in node


def _parse_leaf(node: Mapping[str, Any], path: str) -> FieldSpec:
    if "type" not in node:
        raise SchemaError(f"{path}: field declaration needs a 'type'")
    has_default = "default" in node
    return FieldSpec(
        type=node["type"],
        default=node.get("default"),
        has_default=has_default,
        required=bool(node.get("required", False)),
    )


@dataclass(frozen=True, slots=True)
class ParamSchema:
    """Parsed package schema: a path-mirrored map of field declarations.

    Keys are dotted value-tree paths (``"dims.lx"``, ``"leads.direction"``).
    Built from a raw ``schema:`` mapping by :func:`parse_schema`. The schema only
    *describes* fields; applying it to a value tree (defaults / required / type)
    is :meth:`apply_to`.
    """

    fields: Mapping[str, FieldSpec] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not self.fields

    def apply_to(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """Return a copy of the *values* tree with defaults filled.

        Walks each declared field's mirrored path: when the path is absent and
        the field ``has_default``, the default is written in. Required fields
        (present check) and types are checked by :meth:`validate` — kept separate
        so the loader can run the generator step *between* default-fill and the
        real type check (where ``auto`` has been resolved). Loose: untouched
        paths in *values* (no schema leaf) are copied through verbatim.
        """
        out = _deep_copy_tree(values)
        for path, spec in self.fields.items():
            if not spec.has_default:
                continue
            keys = path.split(".")
            if _get_path(out, keys) is _MISSING:
                _set_path(out, keys, spec.default)
        return out

    def check_required(self, values: Mapping[str, Any]) -> None:
        """Raise :class:`SchemaError` for any required field absent from values.

        A concrete package (the body of a placed part) must have every required
        field filled. Holes (unfilled parameters) live in a ``kind``, not a
        concrete package, and are resolved by the kind generator / place-tool
        picker *before* the package is built — so by the time this runs the
        values are complete. The ``auto`` sentinel counts as present (a generator
        fills it later).
        """
        for path, spec in self.fields.items():
            if not spec.required:
                continue
            val = _get_path(values, path.split("."))
            if val is _MISSING or val is None:
                raise SchemaError(f"required field {path!r} is missing")

    def validate(self, values: Mapping[str, Any]) -> None:
        """Type-check every declared field that is present (loose; ``auto`` ok)."""
        for path, spec in self.fields.items():
            val = _get_path(values, path.split("."))
            if val is _MISSING or val is None:
                continue
            spec.validate(val, path)

    def fields_view(self, values: Mapping[str, Any]) -> list[FieldView]:
        """One :class:`FieldView` per declared field, for the UI.

        Reads each field's *resolved* value (call :meth:`apply_to` first so
        defaults are filled) and classifies its editability: null ⇒ editable
        (``REQUIRED_EMPTY``), a concrete value ⇒ ``SET`` (read-only), ``auto`` ⇒
        ``AUTO`` (read-only). Order follows the schema's declaration order. This
        is the single read-only source of truth — there is no stored flag; the
        answer is a pure function of the value tree (package-model.md §5.3).
        """
        out: list[FieldView] = []
        for path, spec in self.fields.items():
            val = _get_path(values, path.split("."))
            status = _field_status(val)
            shown = None if val is _MISSING else val
            out.append(FieldView(path=path, spec=spec, value=shown,
                                 status=status))
        return out


def parse_schema(raw: object) -> ParamSchema:
    """Parse a raw ``schema:`` mapping into a :class:`ParamSchema`.

    The tree is walked with the single leaf-vs-section rule (``type`` key ⇒
    leaf). Section keys join with ``.`` to form the mirrored value-tree path.
    ``None`` / missing ``schema`` → an empty schema (validates nothing).
    """
    if raw is None:
        return ParamSchema({})
    if not isinstance(raw, Mapping):
        raise SchemaError(f"schema must be a mapping, got {type(raw).__name__}")
    fields: dict[str, FieldSpec] = {}

    def _walk(node: Mapping[str, Any], prefix: str) -> None:
        for key, child in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if _is_field_leaf(child):
                fields[path] = _parse_leaf(child, path)
            elif isinstance(child, Mapping):
                _walk(child, path)
            else:
                raise SchemaError(
                    f"{path}: schema node must be a field (with 'type') or a "
                    f"section (mapping), got {child!r}")

    _walk(raw, "")
    return ParamSchema(fields)


# --------------------------------------------------------------------------- #
# Nested-path helpers (value tree is dict-of-dicts mirroring schema paths)
# --------------------------------------------------------------------------- #


class _Missing:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "<MISSING>"


_MISSING = _Missing()


def _get_path(tree: Mapping[str, Any], keys: Sequence[str]) -> Any:
    node: Any = tree
    for k in keys:
        if not isinstance(node, Mapping) or k not in node:
            return _MISSING
        node = node[k]
    return node


def _set_path(tree: dict[str, Any], keys: Sequence[str], value: Any) -> None:
    node = tree
    for k in keys[:-1]:
        nxt = node.get(k)
        if not isinstance(nxt, dict):
            nxt = {}
            node[k] = nxt
        node = nxt
    node[keys[-1]] = value


def _deep_copy_tree(values: Mapping[str, Any]) -> dict[str, Any]:
    """Shallow-recursive copy of the dict-of-dicts value tree (leaves shared)."""
    out: dict[str, Any] = {}
    for k, v in values.items():
        out[k] = _deep_copy_tree(v) if isinstance(v, Mapping) else v
    return out


__all__ = [
    "AUTO",
    "FieldSpec",
    "FieldStatus",
    "FieldView",
    "ParamSchema",
    "SchemaError",
    "parse_schema",
]
