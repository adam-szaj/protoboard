# SPDX-License-Identifier: MIT
"""Application-level settings persisted across sessions.

Stored in ~/.config/protoboard/app_settings.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from protoboard.paths import APP_SETTINGS_FILE as _CONFIG_PATH


@dataclass
class AppSettings:
    """Persistent application preferences."""

    default_projects_dir: Path | None = None
    component_library_dirs: list[Path] = field(default_factory=list)
    module_dirs: list[Path] = field(default_factory=list)


def load_app_settings() -> AppSettings:
    """Load AppSettings from YAML; return defaults on missing file or any error."""
    if not _CONFIG_PATH.exists():
        return AppSettings()
    try:
        from ruamel.yaml import YAML  # type: ignore[import-untyped]

        data = YAML().load(_CONFIG_PATH)
        if not isinstance(data, dict):
            return AppSettings()
        s = AppSettings()
        if raw := data.get("default_projects_dir"):
            s.default_projects_dir = Path(raw).expanduser()
        if dirs := data.get("component_library_dirs"):
            s.component_library_dirs = [Path(d).expanduser() for d in dirs if d]
        if mdirs := data.get("module_dirs"):
            s.module_dirs = [Path(d).expanduser() for d in mdirs if d]
        return s
    except Exception:  # noqa: BLE001
        return AppSettings()


def save_app_settings(settings: AppSettings) -> None:
    """Persist AppSettings to YAML."""
    try:
        from ruamel.yaml import YAML  # type: ignore[import-untyped]

        data: dict[str, object] = {}
        if settings.default_projects_dir is not None:
            data["default_projects_dir"] = str(settings.default_projects_dir)
        data["component_library_dirs"] = [str(p) for p in settings.component_library_dirs]
        data["module_dirs"] = [str(p) for p in settings.module_dirs]

        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        yaml = YAML()
        yaml.default_flow_style = False
        with _CONFIG_PATH.open("w", encoding="utf-8") as fh:
            yaml.dump(data, fh)
    except Exception:  # noqa: BLE001
        pass
