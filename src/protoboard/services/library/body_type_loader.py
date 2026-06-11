# SPDX-License-Identifier: MIT
"""Load body-type definitions from ``assets/body_types/*.yaml``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from protoboard.domain.components.body_type import BodyTypeSpec, BodyVariant

_yaml = YAML()
_yaml.preserve_quotes = True


from protoboard.paths import BUILTIN_ASSETS_DIR as _BUILTIN_ASSETS_DIR

_BUILTIN_DIR: Path = _BUILTIN_ASSETS_DIR / "body_types"


def load_body_types(directory: Path | None = None) -> dict[str, BodyTypeSpec]:
    """Load all ``*.yaml`` files from *directory* (default: builtin assets).

    Returns a dict keyed by ``family`` name.
    """
    d = directory or _BUILTIN_DIR
    result: dict[str, BodyTypeSpec] = {}
    if not d.is_dir():
        return result
    for path in sorted(d.glob("*.yaml")):
        try:
            spec = _load_one(path)
            result[spec.family] = spec
        except Exception:
            pass
    return result


def _load_one(path: Path) -> BodyTypeSpec:
    """Read one body-type YAML.

    Tolerates both legacy and ``TwoPinBodyFamily``-style YAML during the
    migration:

    - Legacy:  top-level ``shape`` and ``polar`` apply to every variant;
               each variant carries only physical dimensions.
    - New:     ``shape`` / ``polar`` live on each variant.  This loader
               falls back to the first variant's values when top-level
               keys are absent, so it can still serve the legacy
               ``BodyTypeSpec`` contract (single shape per family).
               Variants whose ``shape`` differs from the first are
               silently dropped — this loader only models cylindrical
               families.  Once the renderer consumes the new spec
               directly, this loader and ``BodyTypeSpec`` go away.
    """
    with path.open(encoding="utf-8") as fh:
        data: Any = _yaml.load(fh)

    family = str(data["family"])
    raw_variants = data.get("variants", []) or []
    if not raw_variants:
        raise ValueError(f"{path}: no variants defined")

    # Fall back to the first variant's ``shape`` / ``polar`` when
    # top-level keys are absent (TwoPinBodyFamily YAML format).
    first = raw_variants[0]
    shape = str(data.get("shape") or first.get("shape") or "cylinder")
    polar = bool(data.get("polar", first.get("polar", False)))

    keep: list[BodyVariant] = []
    for v in raw_variants:
        # Skip non-cylinder variants in mixed-shape families — legacy
        # BodyTypeSpec can only express one shape.
        v_shape = str(v.get("shape", shape))
        if v_shape != "cylinder":
            continue
        # Variants without the legacy dimensions are skipped too.
        if "diameter_mm" not in v or "length_mm" not in v:
            continue
        keep.append(BodyVariant(
            id=str(v["id"]),
            label=str(v.get("label", v["id"])),
            diameter_mm=float(v["diameter_mm"]),
            length_mm=float(v["length_mm"]),
            lead_diameter_mm=float(v.get("lead_diameter_mm", 0.5)),
        ))
    if not keep:
        raise ValueError(f"{path}: no cylindrical variants for legacy loader")

    return BodyTypeSpec(family=family, shape=shape, polar=polar, variants=tuple(keep))
