# SPDX-License-Identifier: MIT
"""Load ``TwoPinBodyFamily`` definitions from ``assets/body_types/*.yaml``.

Sibling to the legacy :mod:`body_type_loader` while migration is in
progress.  Once the renderer and footprint layers consume only
``TwoPinBodyFamily``, the legacy loader and its YAML schema disappear.

YAML format (one file per family, file stem matches the ``family`` key):

.. code-block:: yaml

    family: electrolytic_capacitors
    variants:
      - id: 4mm_axial
        label: "4 mm axial"
        lead_arrangement: axial
        shape: cylinder
        edge: rounded
        polar: true
        length_mm: 7.0
        diameter_mm: 4.0
      - id: 10mm_radial
        label: "10 mm radial"
        lead_arrangement: radial
        shape: disc
        disc_profile: candy
        polar: true
        diameter_mm: 10.0
        thickness_mm: 5.0

Each variant is independent: ``lead_arrangement``, ``shape``,
``disc_profile`` and dimensions live on the variant so one family may
hold mixed shapes (e.g. a capacitor family with both axial cylinders
and radial discs).
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

import structlog
from ruamel.yaml import YAML

from protoboard.domain.components.two_pin_body import (
    BodyShape,
    DiscProfile,
    EdgeStyle,
    LeadArrangement,
    TwoPinBodyFamily,
    TwoPinBodySpec,
)

_E = TypeVar("_E", bound=Enum)
from protoboard.paths import BUILTIN_ASSETS_DIR as _BUILTIN_ASSETS_DIR

_log = structlog.get_logger(__name__)

_yaml = YAML()
_yaml.preserve_quotes = True

_BUILTIN_DIR: Path = _BUILTIN_ASSETS_DIR / "body_types"


def load_two_pin_body_families(
    directory: Path | None = None,
) -> dict[str, TwoPinBodyFamily]:
    """Load all ``*.yaml`` files from *directory* (default: builtin assets).

    Returns a dict keyed by ``family`` name.  Files that fail to parse
    or validate are logged and skipped; the rest of the catalogue still
    loads so a single malformed asset does not break startup.
    """
    d = directory or _BUILTIN_DIR
    result: dict[str, TwoPinBodyFamily] = {}
    if not d.is_dir():
        return result
    for path in sorted(d.glob("*.yaml")):
        try:
            family = _load_one(path)
        except Exception as exc:
            _log.warning(
                "body_type.load_failed",
                path=str(path),
                error=str(exc),
            )
            continue
        if family.family in result:
            _log.warning(
                "body_type.duplicate_family",
                family=family.family,
                path=str(path),
            )
            continue
        result[family.family] = family
    return result


def _load_one(path: Path) -> TwoPinBodyFamily:
    with path.open(encoding="utf-8") as fh:
        data: Any = _yaml.load(fh)

    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level YAML must be a mapping")

    family_name = str(data["family"])
    raw_variants = data.get("variants") or []
    if not raw_variants:
        raise ValueError(f"{path}: no variants defined")

    variants: list[TwoPinBodySpec] = []
    for entry in raw_variants:
        variants.append(_parse_variant(family_name, entry, path))
    return TwoPinBodyFamily(family=family_name, variants=tuple(variants))


def _parse_variant(
    family_name: str,
    entry: dict[str, Any],
    path: Path,
) -> TwoPinBodySpec:
    variant_id = str(entry["id"])
    label = str(entry.get("label", variant_id))

    lead_arr = _parse_enum(
        entry.get("lead_arrangement"), LeadArrangement,
        ctx=f"{path}:{variant_id}.lead_arrangement",
    )
    shape = _parse_enum(
        entry.get("shape"), BodyShape,
        ctx=f"{path}:{variant_id}.shape",
    )
    edge = _parse_enum(
        entry.get("edge", "rounded"), EdgeStyle,
        ctx=f"{path}:{variant_id}.edge",
    )
    disc_profile: DiscProfile | None = None
    if "disc_profile" in entry:
        disc_profile = _parse_enum(
            entry["disc_profile"], DiscProfile,
            ctx=f"{path}:{variant_id}.disc_profile",
        )

    # Optional physical dimensions — validation handled in
    # TwoPinBodySpec.__post_init__ based on shape.
    return TwoPinBodySpec(
        family=family_name,
        variant_id=variant_id,
        label=label,
        lead_arrangement=lead_arr,
        shape=shape,
        edge=edge,
        polar=bool(entry.get("polar", False)),
        length_mm=_opt_float(entry.get("length_mm")),
        diameter_mm=_opt_float(entry.get("diameter_mm")),
        width_mm=_opt_float(entry.get("width_mm")),
        height_mm=_opt_float(entry.get("height_mm")),
        thickness_mm=_opt_float(entry.get("thickness_mm")),
        disc_profile=disc_profile,
        lead_diameter_mm=float(entry.get("lead_diameter_mm", 0.5)),
    )


def _opt_float(v: Any) -> float | None:
    return None if v is None else float(v)


def _parse_enum(value: Any, enum_cls: type[_E], *, ctx: str) -> _E:
    """Coerce *value* to a member of *enum_cls* by ``.value`` string."""
    if value is None:
        raise ValueError(f"{ctx}: required, got null")
    if isinstance(value, enum_cls):
        return value
    s = str(value).lower()
    for member in enum_cls:
        if member.value == s:
            return member
    valid = ", ".join(repr(m.value) for m in enum_cls)
    raise ValueError(f"{ctx}: {value!r} is not one of {valid}")
