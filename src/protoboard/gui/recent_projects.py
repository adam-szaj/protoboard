# SPDX-License-Identifier: MIT
"""Recent projects list — persisted to ~/.config/protoboard-designer/recent.json."""

from __future__ import annotations

import json
from pathlib import Path

_CONFIG_DIR = Path.home() / ".config" / "protoboard-designer"
_RECENT_FILE = _CONFIG_DIR / "recent.json"
_MAX = 10


def load() -> list[Path]:
    """Return the list of recent project paths (most recent first).

    Entries that no longer exist on disk are silently dropped.
    """
    try:
        data = json.loads(_RECENT_FILE.read_text(encoding="utf-8"))
        paths = [Path(p) for p in data if isinstance(p, str)]
    except Exception:  # noqa: BLE001
        paths = []
    return [p for p in paths if p.exists()]


def add(path: Path) -> None:
    """Prepend *path* to the recent list and persist it."""
    existing = load()
    # Remove duplicates, prepend new entry, truncate.
    updated = [path] + [p for p in existing if p != path]
    updated = updated[:_MAX]
    try:
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _RECENT_FILE.write_text(
            json.dumps([str(p) for p in updated], indent=2),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass
