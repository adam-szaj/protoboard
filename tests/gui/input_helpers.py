# SPDX-License-Identifier: MIT
"""Shared builders for the seam input DTOs (qt-ports §2.2).

Test suites drive tools through ``PointerInput`` instead of hand-built
``QMouseEvent``s; Qt modifier flags are still accepted so call sites keep
their historical ``mods=Qt.KeyboardModifier...`` arguments.
"""

from __future__ import annotations

from PySide6.QtCore import Qt

from protoboard.geometry import Point
from protoboard.gui.ports import LEFT_BUTTON, KeyInput, Mods, PointerInput, WheelInput
from protoboard.gui.qt_inputs import mods_from_qt


def pointer(
    x: float,
    y: float,
    *,
    button: int = LEFT_BUTTON,
    mods: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
) -> PointerInput:
    """A PointerInput at scene == view position (x, y)."""
    return PointerInput(
        view_pos=Point(x, y),
        scene_pos=Point(x, y),
        button=int(button),
        native_mods=int(mods.value),
        mods=mods_from_qt(mods),
    )


def wheel(delta_y: int, *, x: float = 0.0, y: float = 0.0) -> WheelInput:
    """A WheelInput with *delta_y* at view position (x, y), no modifiers."""
    return WheelInput(
        delta_y=delta_y, view_pos=Point(x, y), native_mods=0, mods=Mods()
    )


def key_inp(
    key: int,
    mods: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
    *,
    auto_repeat: bool = False,
) -> KeyInput:
    """A KeyInput for *key* with Qt modifier flags *mods*."""
    return KeyInput(
        key=int(key),
        native_mods=int(mods.value),
        is_auto_repeat=auto_repeat,
        mods=mods_from_qt(mods),
    )
