# SPDX-License-Identifier: MIT
"""Canonical filesystem paths for the application.

All runtime paths are derived from XDG-style conventions:
  - Config:  ~/.config/protoboard/
  - Data:    ~/.local/share/protoboard/

Built-in read-only assets (library/, assets/) are located relative to the
installed package using importlib.resources / walk-up fallback so they work
both in-source and after ``pip install``.
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# App name
# ---------------------------------------------------------------------------

APP_NAME = "protoboard"

# ---------------------------------------------------------------------------
# User config directory  (~/.config/protoboard/)
# ---------------------------------------------------------------------------

CONFIG_DIR: Path = Path.home() / ".config" / APP_NAME

# Individual config files
KEYBINDINGS_FILE: Path = CONFIG_DIR / "keybindings.yaml"
# Namespaced, category-grouped keybindings (Stage 1 of the keybindings
# redesign — lives alongside the legacy flat file until the migration stage).
KEYBINDINGS_V2_FILE: Path = CONFIG_DIR / "keybindings_v2.yaml"
THEME_FILE: Path        = CONFIG_DIR / "theme.yaml"
WINDOW_STATE_FILE: Path = CONFIG_DIR / "window_state.yaml"
APP_SETTINGS_FILE: Path = CONFIG_DIR / "app_settings.yaml"
RECENT_PROJECTS_FILE: Path = CONFIG_DIR / "recent.json"
RECENT_FOOTPRINTS_FILE: Path = CONFIG_DIR / "recent_footprints.json"
RECENT_KIND_VARIANTS_FILE: Path = CONFIG_DIR / "recent_kind_variants.json"
TOOLBARS_FILE: Path = CONFIG_DIR / "toolbars.yaml"

# ---------------------------------------------------------------------------
# User data directory  (~/.local/share/protoboard/)
# ---------------------------------------------------------------------------

DATA_DIR: Path = Path.home() / ".local" / "share" / APP_NAME

# User-installed component libraries land here by convention
USER_LIBRARY_DIR: Path = DATA_DIR / "library"

# User-installed module definitions land here
USER_MODULES_DIR: Path = DATA_DIR / "modules"

# ---------------------------------------------------------------------------
# Built-in read-only assets (shipped with the package)
# ---------------------------------------------------------------------------

def _find_package_root() -> Path:
    """Locate the directory that hosts built-in read-only assets.

    Two scenarios:
      1. PyInstaller bundle — assets are unpacked to ``sys._MEIPASS``.
      2. Source / pip install — walk up from this file until a ``library/``
         sibling appears.
    """
    import sys
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass is not None:
        return Path(meipass)
    candidate = Path(__file__).resolve().parent
    for _ in range(8):
        if (candidate / "library").is_dir():
            return candidate
        candidate = candidate.parent
    return Path(__file__).resolve().parent


_PKG_ROOT: Path = _find_package_root()

BUILTIN_LIBRARY_DIR: Path = _PKG_ROOT / "library"
BUILTIN_ASSETS_DIR: Path   = _PKG_ROOT / "assets"

# Default toolbar layout shipped with the package — copied to
# ``TOOLBARS_FILE`` on first start (when the user has no override).
# Lives inside the ``protoboard`` Python package so hatchling /
# PyInstaller pick it up automatically as part of the wheel.
BUILTIN_DEFAULT_TOOLBARS_FILE: Path = (
    Path(__file__).resolve().parent / "default_toolbars.yaml"
)
