# SPDX-License-Identifier: MIT
"""Recent projects list — persisted to ~/.config/protoboard/recent.json."""

from __future__ import annotations

from pathlib import Path

from protoboard.paths import RECENT_PROJECTS_FILE as _RECENT_FILE
from protoboard.services.persistence.recent_store import RecentStore

_MAX = 10


def _store() -> RecentStore[Path]:
    return RecentStore(
        path=_RECENT_FILE,
        max_entries=_MAX,
        decode=lambda raw: Path(raw) if isinstance(raw, str) else None,
        encode=str,
        key=lambda path: path,
    )


def load() -> list[Path]:
    """Return the list of recent project paths (most recent first).

    Entries that no longer exist on disk are silently dropped.
    """
    return [path for path in _store().load() if path.exists()]


def add(path: Path) -> None:
    """Prepend *path* to the recent list and persist it."""
    _store().add(path)
