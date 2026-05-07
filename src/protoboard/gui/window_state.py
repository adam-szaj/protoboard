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
            _clamp_to_screen(window)
        if state := data.get("state"):
            window.restoreState(QByteArray(base64.b64decode(state)))
    except Exception:  # noqa: BLE001
        pass


def _clamp_to_screen(window: QMainWindow) -> None:
    """Ensure the window fits within the available screen area after restore.

    Qt's restoreGeometry() blindly replays saved coordinates — if the window
    was last seen on a larger monitor or dragged off-screen, it can appear
    partially or fully invisible on next launch.  This function clamps both
    size and position to the geometry of the screen that contains the window's
    centre point (or the primary screen if the centre is off all screens).
    """
    from PySide6.QtWidgets import QApplication

    centre = window.geometry().center()
    screen = QApplication.screenAt(centre) or QApplication.primaryScreen()
    if screen is None:
        return
    available = screen.availableGeometry()

    geo = window.geometry()

    # Clamp size — don't exceed available area but keep above the default minimum.
    new_w = max(640, min(geo.width(), available.width()))
    new_h = max(480, min(geo.height(), available.height()))

    # Clamp position — ensure the window is fully inside the available area.
    new_x = max(available.left(), min(geo.left(), available.right() - new_w))
    new_y = max(available.top(), min(geo.top(), available.bottom() - new_h))

    window.setGeometry(new_x, new_y, new_w, new_h)
