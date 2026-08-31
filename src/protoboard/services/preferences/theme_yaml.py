# SPDX-License-Identifier: MIT
"""YAML persistence for visual theme preferences."""

from __future__ import annotations

from pathlib import Path

from protoboard.controller.view_options.theme import DEFAULT_THEME, Theme


def save_theme(theme: Theme, path: Path | None = None) -> None:
    """Persist *theme* to YAML, writing only fields that differ from defaults."""
    from protoboard.paths import THEME_FILE

    if path is None:
        path = THEME_FILE
    try:
        from ruamel.yaml import YAML

        defaults = Theme()
        data: dict[str, object] = {}
        for name in theme.__dataclass_fields__:
            if name == "package_families":
                continue
            value = getattr(theme, name)
            if value != getattr(defaults, name):
                data[name] = value

        path.parent.mkdir(parents=True, exist_ok=True)
        yaml = YAML()
        yaml.default_flow_style = False
        with path.open("w", encoding="utf-8") as stream:
            yaml.dump(data, stream)
    except Exception:  # noqa: BLE001
        pass


def load_theme(path: Path | None = None) -> Theme:
    """Load a theme from YAML, falling back to defaults on any error."""
    from protoboard.paths import THEME_FILE

    if path is None:
        path = THEME_FILE
    if not path.exists():
        return DEFAULT_THEME
    try:
        from ruamel.yaml import YAML

        data = YAML().load(path)
        if not isinstance(data, dict):
            return DEFAULT_THEME
        theme = Theme()
        for key, value in data.items():
            if hasattr(theme, key):
                setattr(theme, key, value)
        return theme
    except Exception:  # noqa: BLE001
        return DEFAULT_THEME
