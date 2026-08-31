# SPDX-License-Identifier: MIT
"""Cached registry of built-in package definitions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from protoboard.domain.components.package.kind_nodes import (
    KIND_NODES,
    register_kind_nodes,
)
from protoboard.paths import BUILTIN_ASSETS_DIR

_yaml = YAML(typ="safe")


def load_builtin_package_defs() -> dict[str, dict[str, Any]]:
    """Return generic package definitions shipped with the application."""
    cached = getattr(load_builtin_package_defs, "_cache", None)
    if cached is not None:
        if any(
            isinstance(d.get("kind"), str) and d["kind"] not in KIND_NODES
            for d in cached.values()
        ):
            register_kind_nodes(cached)
        return cached  # type: ignore[no-any-return]

    defs: dict[str, dict[str, Any]] = {}
    pkg_dir: Path = BUILTIN_ASSETS_DIR / "packages"
    if pkg_dir.is_dir():
        for yaml_path in sorted(pkg_dir.glob("*.yaml")):
            data = _yaml.load(yaml_path.read_text())
            for definition in (data or {}).get("packages", []):
                if isinstance(definition, dict) and isinstance(
                    definition.get("name"), str
                ):
                    defs[definition["name"]] = definition
    register_kind_nodes(defs)
    load_builtin_package_defs._cache = defs  # type: ignore[attr-defined]
    return defs
