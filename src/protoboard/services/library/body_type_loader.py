# SPDX-License-Identifier: MIT
"""Load body-type definitions from ``assets/body_types/*.yaml``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from protoboard.domain.components.body_type import BodyTypeSpec, BodyVariant

_yaml = YAML()
_yaml.preserve_quotes = True


def _find_body_types_dir() -> Path:
    candidate = Path(__file__).resolve()
    for _ in range(8):
        candidate = candidate.parent
        d = candidate / "assets" / "body_types"
        if d.is_dir():
            return d
    return Path(__file__).parent.parent.parent.parent.parent.parent / "assets" / "body_types"


_BUILTIN_DIR: Path = _find_body_types_dir()


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
    with path.open(encoding="utf-8") as fh:
        data: Any = _yaml.load(fh)

    family = str(data["family"])
    shape  = str(data.get("shape", "cylinder"))
    polar  = bool(data.get("polar", False))

    variants = tuple(
        BodyVariant(
            id=str(v["id"]),
            label=str(v.get("label", v["id"])),
            diameter_mm=float(v["diameter_mm"]),
            length_mm=float(v["length_mm"]),
            lead_diameter_mm=float(v.get("lead_diameter_mm", 0.5)),
        )
        for v in data.get("variants", [])
    )
    if not variants:
        raise ValueError(f"{path}: no variants defined")

    return BodyTypeSpec(family=family, shape=shape, polar=polar, variants=variants)
