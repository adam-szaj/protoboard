# SPDX-License-Identifier: MIT
"""Small helpers for managing transient ``QGraphicsScene`` items."""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtWidgets import QGraphicsItem, QGraphicsScene


def remove_scene_items(
    scene: QGraphicsScene,
    items: Iterable[object],
) -> None:
    """Remove items owned by *scene*, tolerating stale Qt wrappers."""
    for item in items:
        try:
            if isinstance(item, QGraphicsItem) and item.scene() is scene:
                scene.removeItem(item)
        except RuntimeError:
            pass
