# SPDX-License-Identifier: MIT
"""Load ``TwoPinBodyPackage`` definitions from ``assets/body_types/*.yaml``.

One YAML file may define multiple **packages** that share a common
shape family (e.g. ``axial_glass.yaml`` carries DO-35 in S/typ/L
dimensional variants; ``radial_disc.yaml`` carries Ceramic-Disc-D5 /
D7 / D10).  Each package's variants are dimensional small/typ/large
samples that bracket the datasheet tolerance window.

YAML schema:

.. code-block:: yaml

    packages:
      - id: DO-35
        label: "DO-35 (subminiature axial glass)"
        material: glass
        lead_arrangement: axial
        shape: cylinder
        edge: rounded
        variants:
          - id: S
            label: "Small (1.5 × 3.0 mm)"
            diameter_mm: 1.5
            length_mm: 3.0
            lead_diameter_mm: 0.4
          - id: typ
            label: "Typical (1.8 × 3.7 mm)"
            diameter_mm: 1.8
            length_mm: 3.7
          - id: L
            label: "Large (2.0 × 4.5 mm)"
            diameter_mm: 2.0
            length_mm: 4.5

Package-level keys (``material``, ``lead_arrangement``, ``shape``,
``edge``, ``disc_profile``) supply the common defaults shared by every
variant; variants only override what differs.
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
    Material,
    TwoPinBodyPackage,
    TwoPinBodySpec,
)
from protoboard.paths import BUILTIN_ASSETS_DIR as _BUILTIN_ASSETS_DIR

_E = TypeVar("_E", bound=Enum)

_log = structlog.get_logger(__name__)

_yaml = YAML()
_yaml.preserve_quotes = True

_BUILTIN_DIR: Path = _BUILTIN_ASSETS_DIR / "body_types"


def load_two_pin_body_families(
    directory: Path | None = None,
) -> dict[str, TwoPinBodyPackage]:
    """Load all ``*.yaml`` files from *directory* (default: builtin assets).

    Returns a dict keyed by ``package_id``.  Files that fail to parse
    or validate are logged and skipped; the rest of the catalogue still
    loads so a single malformed asset does not break startup.

    .. note::
        The function name still uses ``families`` for backwards
        compatibility with the older naming.  The returned mapping now
        keys per *package* (industry standard like DO-35) — the legacy
        family / package distinction has collapsed.
    """
    d = directory or _BUILTIN_DIR
    result: dict[str, TwoPinBodyPackage] = {}
    if not d.is_dir():
        return result
    for path in sorted(d.glob("*.yaml")):
        try:
            packages = _load_one_file(path)
        except Exception as exc:
            _log.warning(
                "body_type.load_failed",
                path=str(path),
                error=str(exc),
            )
            continue
        for pkg in packages:
            if pkg.package_id in result:
                _log.warning(
                    "body_type.duplicate_package",
                    package_id=pkg.package_id,
                    path=str(path),
                )
                continue
            result[pkg.package_id] = pkg
    return result


def _load_one_file(path: Path) -> list[TwoPinBodyPackage]:
    """Parse one YAML file into one or more :class:`TwoPinBodyPackage`."""
    with path.open(encoding="utf-8") as fh:
        data: Any = _yaml.load(fh)

    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level YAML must be a mapping")

    raw_packages = data.get("packages")
    if not raw_packages or not isinstance(raw_packages, list):
        raise ValueError(f"{path}: 'packages:' must be a non-empty list")

    out: list[TwoPinBodyPackage] = []
    for entry in raw_packages:
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: every package must be a mapping")
        out.append(_parse_package(entry, path))
    return out


def _parse_package(entry: dict[str, Any], path: Path) -> TwoPinBodyPackage:
    package_id = str(entry["id"])
    label = str(entry.get("label", package_id))

    # Package-level defaults — shared by every variant unless overridden.
    pkg_lead = _parse_enum(
        entry.get("lead_arrangement"), LeadArrangement,
        ctx=f"{path}:{package_id}.lead_arrangement",
    )
    pkg_shape = _parse_enum(
        entry.get("shape"), BodyShape,
        ctx=f"{path}:{package_id}.shape",
    )
    pkg_edge = _parse_enum(
        entry.get("edge", "rounded"), EdgeStyle,
        ctx=f"{path}:{package_id}.edge",
    )
    pkg_material = _parse_enum(
        entry.get("material", "plastic"), Material,
        ctx=f"{path}:{package_id}.material",
    )
    pkg_disc_profile: DiscProfile | None = None
    if "disc_profile" in entry:
        pkg_disc_profile = _parse_enum(
            entry["disc_profile"], DiscProfile,
            ctx=f"{path}:{package_id}.disc_profile",
        )
    pkg_lead_diameter = float(entry.get("lead_diameter_mm", 0.5))

    raw_variants = entry.get("variants") or []
    if not raw_variants:
        raise ValueError(
            f"{path}: package {package_id!r} has no variants"
        )

    variants: list[TwoPinBodySpec] = []
    for v in raw_variants:
        if not isinstance(v, dict):
            raise ValueError(
                f"{path}:{package_id}: every variant must be a mapping"
            )
        variants.append(_parse_variant(
            v, path, package_id,
            default_lead=pkg_lead, default_shape=pkg_shape,
            default_edge=pkg_edge, default_material=pkg_material,
            default_disc_profile=pkg_disc_profile,
            default_lead_diameter=pkg_lead_diameter,
        ))
    return TwoPinBodyPackage(
        package_id=package_id, label=label, variants=tuple(variants),
    )


def _parse_variant(
    entry: dict[str, Any],
    path: Path,
    package_id: str,
    *,
    default_lead: LeadArrangement,
    default_shape: BodyShape,
    default_edge: EdgeStyle,
    default_material: Material,
    default_disc_profile: DiscProfile | None,
    default_lead_diameter: float,
) -> TwoPinBodySpec:
    variant_id = str(entry["id"])
    label = str(entry.get("label", variant_id))

    # Variants may override any of the package-level defaults.
    lead_arr = (
        _parse_enum(entry["lead_arrangement"], LeadArrangement,
                    ctx=f"{path}:{package_id}/{variant_id}.lead_arrangement")
        if "lead_arrangement" in entry else default_lead
    )
    shape = (
        _parse_enum(entry["shape"], BodyShape,
                    ctx=f"{path}:{package_id}/{variant_id}.shape")
        if "shape" in entry else default_shape
    )
    edge = (
        _parse_enum(entry["edge"], EdgeStyle,
                    ctx=f"{path}:{package_id}/{variant_id}.edge")
        if "edge" in entry else default_edge
    )
    material = (
        _parse_enum(entry["material"], Material,
                    ctx=f"{path}:{package_id}/{variant_id}.material")
        if "material" in entry else default_material
    )
    disc_profile = (
        _parse_enum(entry["disc_profile"], DiscProfile,
                    ctx=f"{path}:{package_id}/{variant_id}.disc_profile")
        if "disc_profile" in entry else default_disc_profile
    )
    lead_diameter = float(entry.get("lead_diameter_mm", default_lead_diameter))

    return TwoPinBodySpec(
        package_id=package_id,
        variant_id=variant_id,
        label=label,
        lead_arrangement=lead_arr,
        shape=shape,
        edge=edge,
        material=material,
        length_mm=_opt_float(entry.get("length_mm")),
        diameter_mm=_opt_float(entry.get("diameter_mm")),
        width_mm=_opt_float(entry.get("width_mm")),
        height_mm=_opt_float(entry.get("height_mm")),
        thickness_mm=_opt_float(entry.get("thickness_mm")),
        disc_profile=disc_profile,
        lead_diameter_mm=lead_diameter,
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
