# SPDX-License-Identifier: MIT
"""Qt event → seam input-DTO factories (qt-ports §2.2).

The single sanctioned conversion site between raw Qt input events and the
Qt-free DTOs in :mod:`protoboard.gui.ports.inputs`.  Only Qt-side code
(``BoardView`` / ``ToolHost``) imports this module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt

from protoboard.geometry import Point
from protoboard.gui.ports import KeyInput, Mods, PointerInput, WheelInput

if TYPE_CHECKING:
    from PySide6.QtGui import QKeyEvent, QMouseEvent, QWheelEvent


def mods_from_qt(qt_mods: Qt.KeyboardModifier) -> Mods:
    return Mods(
        ctrl=bool(qt_mods & Qt.KeyboardModifier.ControlModifier),
        shift=bool(qt_mods & Qt.KeyboardModifier.ShiftModifier),
        alt=bool(qt_mods & Qt.KeyboardModifier.AltModifier),
    )


def pointer_input(event: QMouseEvent, scene_pos: Point) -> PointerInput:
    pos = event.position()
    return PointerInput(
        view_pos=Point(pos.x(), pos.y()),
        scene_pos=scene_pos,
        button=int(event.button().value),
        native_mods=int(event.modifiers().value),
        mods=mods_from_qt(event.modifiers()),
    )


def wheel_input(event: QWheelEvent) -> WheelInput:
    pos = event.position()
    return WheelInput(
        delta_y=int(event.angleDelta().y()),
        view_pos=Point(pos.x(), pos.y()),
        native_mods=int(event.modifiers().value),
        mods=mods_from_qt(event.modifiers()),
    )


def key_input(event: QKeyEvent) -> KeyInput:
    return KeyInput(
        key=int(event.key()),
        native_mods=int(event.modifiers().value),
        is_auto_repeat=event.isAutoRepeat(),
        mods=mods_from_qt(event.modifiers()),
    )
