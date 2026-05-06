# SPDX-License-Identifier: MIT
"""Persistence of QMainWindow geometry and dock layout.

State is stored as base64-encoded byte blobs (Qt's native format) in
~/.config/protoboard/window_state.yaml.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

from protoboard.paths import WINDOW_STATE_FILE as _CONFIG_PATH

if TYPE_CHECKING:
    from PySide6.QtWidgets import QMainWindow


def save_window_state(window: QMainWindow) -> None:
    """Persist geometry and dock/toolbar layout of *window*."""
    try:
        from ruamel.yaml import YAML  # type: ignore[import-untyped]

        data = {
            "geometry": base64.b64encode(bytes(window.saveGeometry())).decode(),
            "state": base64.b64encode(bytes(window.saveState())).decode(),
        }
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        yaml = YAML()
        yaml.default_flow_style = False
        with _CONFIG_PATH.open("w", encoding="utf-8") as fh:
            yaml.dump(data, fh)
    except Exception:  # noqa: BLE001
        pass  # non-critical — silently skip on any error


def load_window_state(window: QMainWindow) -> None:
    """Restore geometry and dock/toolbar layout into *window*."""
    if not _CONFIG_PATH.exists():
        return
    try:
        from ruamel.yaml import YAML  # type: ignore[import-untyped]
        from PySide6.QtCore import QByteArray

        data = YAML().load(_CONFIG_PATH)
        if not isinstance(data, dict):
            return
        if geo := data.get("geometry"):
            window.restoreGeometry(QByteArray(base64.b64decode(geo)))
        if state := data.get("state"):
            window.restoreState(QByteArray(base64.b64decode(state)))
    except Exception:  # noqa: BLE001
        pass
