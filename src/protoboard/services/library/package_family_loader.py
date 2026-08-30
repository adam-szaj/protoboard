# SPDX-License-Identifier: MIT
"""Load :class:`PackageFamily` catalogues from ``assets/package_families/*.yaml``.

This replaces ``two_pin_body_loader`` (which built the legacy
``TwoPinBodyPackage`` catalogue) with one that builds real packages.  Each
family entry names a generic to ``extends`` (supplying lead direction and
board-offset mode) plus family-level presentation (``color``, ``arrangement``,
``edge``); each variant supplies its ``dims`` and lead geometry.  A variant is
built into a full :class:`Package` via the package loader, so the picker offers
geometrically-real bodies, not render-only silhouettes.

YAML schema::

    families:
      - family: DO-35
        extends: cylinder_axial      # generic: direction + board_offset mode
        label: "DO-35 (subminiature axial glass)"
        color: "#f4ece0"
        default: typ                 # optional; first variant otherwise
        variants:
          - id: typ
            label: "Typical (1.8 x 3.7 mm)"
            dims: {diameter: 1.8, length: 3.7}
            leads: {lead_r: 0.2, lead_length: 5.6}

Family-level keys other than ``family`` / ``label`` / ``default`` / ``variants``
are inherited by every variant (a variant may override them).  Files that fail
to parse are logged and skipped so one bad asset does not break startup.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import structlog
from ruamel.yaml import YAML

from protoboard.domain.components.package.family import (
    PackageFamily,
    PackageVariant,
)
from protoboard.domain.components.package.loader import (
    PackageDefError,
    build_package,
    resolve_extends,
)
from protoboard.paths import BUILTIN_ASSETS_DIR as _BUILTIN_ASSETS_DIR
from protoboard.services.library.loader import _load_builtin_package_defs

_log = structlog.get_logger(__name__)

_yaml = YAML()
_yaml.preserve_quotes = True

_BUILTIN_DIR: Path = _BUILTIN_ASSETS_DIR / "package_families"

# Keys consumed structurally by the family loader — everything else on a family
# entry is inherited presentation/geometry merged into each variant.
_FAMILY_STRUCTURAL = frozenset({"family", "label", "default", "variants"})


def validate_catalogue_addresses(
    families: dict[str, PackageFamily],
) -> None:
    """Reject a family id that is also another family's variant address."""
    family_ids = set(families)
    for family in families.values():
        for variant in family.variants:
            address = family.address(variant.variant_id)
            if address in family_ids:
                raise ValueError(
                    f"ambiguous package address {address!r}: both a family "
                    "and a variant"
                )


def load_package_families(
    directory: Path | None = None,
) -> dict[str, PackageFamily]:
    """Load every ``*.yaml`` family file from *directory* (default: builtin).

    Returns a dict keyed by ``family_id``.  Malformed files are logged and
    skipped; the rest of the catalogue still loads.
    """
    d = directory or _BUILTIN_DIR
    out: dict[str, PackageFamily] = {}
    if not d.is_dir():
        return out
    generics = _load_builtin_package_defs()
    for path in sorted(d.glob("*.yaml")):
        try:
            families = _load_one_file(path, generics)
        except Exception as exc:  # noqa: BLE001 — one bad asset must not break startup
            _log.warning("package_family.load_failed", path=str(path), error=str(exc))
            continue
        for fam in families:
            if fam.family_id in out:
                _log.warning(
                    "package_family.duplicate",
                    family_id=fam.family_id,
                    path=str(path),
                )
                continue
            out[fam.family_id] = fam
    validate_catalogue_addresses(out)
    return out


def load_full_package_catalogue(
    directory: Path | None = None,
) -> dict[str, PackageFamily]:
    """The complete body catalogue: generated in-line families + authored YAML.

    Generated ``dip-N`` / ``wdip-N`` / ``sil-N`` bodies (from
    :func:`generate_inline_families`) plus the authored families loaded from
    *directory*; an authored family wins on an id clash.  This is the single
    source of truth for both the session's picker catalogue and the library
    loader's rigid-package resolution, so they never diverge.
    """
    from protoboard.domain.components.package.generated_families import (
        generate_inline_families,
    )

    catalogue = {
        **generate_inline_families(),
        **load_package_families(directory),
    }
    validate_catalogue_addresses(catalogue)
    return catalogue


def _load_one_file(
    path: Path, generics: dict[str, dict[str, Any]]
) -> list[PackageFamily]:
    with path.open(encoding="utf-8") as fh:
        data: Any = _yaml.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level YAML must be a mapping")
    raw = data.get("families")
    if not raw or not isinstance(raw, list):
        raise ValueError(f"{path}: 'families:' must be a non-empty list")
    return [parse_family(entry, path, generics) for entry in raw]


def parse_family(
    entry: dict[str, Any], path: Path, generics: dict[str, dict[str, Any]]
) -> PackageFamily:
    """Parse one raw ``families:`` entry into a :class:`PackageFamily`.

    *generics* is the registry the entry's ``extends`` may resolve against.
    Also used by the component-library loader for ``families:`` sections
    embedded in library files. Raises ``ValueError`` on a malformed entry.
    """
    if not isinstance(entry, dict) or "family" not in entry:
        raise ValueError(f"{path}: every family needs a 'family' id")
    family_id = str(entry["family"])
    label = str(entry.get("label", family_id))
    default_id = entry.get("default")
    raw_variants = entry.get("variants")
    if not raw_variants or not isinstance(raw_variants, list):
        raise ValueError(f"{path}: family {family_id!r} has no variants")

    # Family-level fields inherited by every variant (presentation + extends).
    inherited = {k: v for k, v in entry.items() if k not in _FAMILY_STRUCTURAL}

    variants: list[PackageVariant] = []
    for v in raw_variants:
        if not isinstance(v, dict) or "id" not in v:
            raise ValueError(f"{path}:{family_id}: every variant needs an 'id'")
        variants.append(_build_variant(family_id, inherited, v, generics, path))
    return PackageFamily(
        family_id=family_id,
        label=label,
        variants=tuple(variants),
        default_id=None if default_id is None else str(default_id),
    )


def _build_variant(
    family_id: str,
    inherited: dict[str, Any],
    variant: dict[str, Any],
    generics: dict[str, dict[str, Any]],
    path: Path,
) -> PackageVariant:
    variant_id = str(variant["id"])
    label = str(variant.get("label", variant_id))

    # Compose the package definition: family-inherited fields, then the
    # variant's own (dims / leads deep-merged so a variant tops up the family).
    node: dict[str, Any] = copy.deepcopy(inherited)
    for key, val in variant.items():
        if key in ("id", "label"):
            continue
        if isinstance(val, dict) and isinstance(node.get(key), dict):
            node[key] = {**node[key], **val}
        else:
            node[key] = val

    # Resolve the extends chain against the builtin generics, then build.
    extends = node.pop("extends", None)
    synthetic_name = f"{family_id}/{variant_id}"
    registry = dict(generics)
    node["name"] = synthetic_name
    if extends is not None:
        node["extends"] = str(extends)
    registry[synthetic_name] = node
    try:
        merged = resolve_extends(synthetic_name, registry)
        from protoboard.domain.components.package.loader import extends_chain

        # Lineage: extends ancestry, with the family id spliced in before the
        # variant leaf — a renderer registered for the family (e.g. "TO-220")
        # covers every variant unless a more specific one claims the leaf.
        chain = extends_chain(synthetic_name, registry)
        lineage = (*chain[:-1], family_id, synthetic_name)
        package = build_package(merged, synthetic_name, lineage=lineage)
    except PackageDefError as exc:
        raise ValueError(f"{path}:{synthetic_name}: {exc}") from exc
    return PackageVariant(variant_id=variant_id, label=label, package=package)
