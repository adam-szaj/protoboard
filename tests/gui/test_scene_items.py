# SPDX-License-Identifier: MIT
"""Shared Qt scene-item lifecycle helpers."""

from __future__ import annotations

from PySide6.QtWidgets import QGraphicsRectItem, QGraphicsScene
from pytestqt.qtbot import QtBot

from protoboard.gui.scene_items import remove_scene_items


def test_remove_scene_items_removes_live_items(qtbot: QtBot) -> None:
    del qtbot
    scene = QGraphicsScene()
    items = [QGraphicsRectItem(), QGraphicsRectItem()]
    for item in items:
        scene.addItem(item)

    remove_scene_items(scene, items)

    assert all(item.scene() is None for item in items)


def test_remove_scene_items_ignores_items_deleted_by_scene_clear(qtbot: QtBot) -> None:
    del qtbot
    scene = QGraphicsScene()
    items = [QGraphicsRectItem(), QGraphicsRectItem()]
    for item in items:
        scene.addItem(item)
    scene.clear()

    remove_scene_items(scene, items)


def test_remove_scene_items_ignores_items_not_owned_by_scene(qtbot: QtBot) -> None:
    del qtbot
    scene = QGraphicsScene()
    other_scene = QGraphicsScene()
    item = QGraphicsRectItem()
    other_scene.addItem(item)

    remove_scene_items(scene, [item, object()])

    assert item.scene() is other_scene
