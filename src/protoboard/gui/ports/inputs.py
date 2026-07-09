# SPDX-License-Identifier: MIT
"""Qt-free input vocabulary of the board-view seam (qt-ports §2.2).

Two layers, aligned with the vocabularies that already existed:

* :class:`Mods` / :class:`Intent` — the controller-facing semantics
  (moved here from ``gui.selection.mouse_input``, which re-exports them).
* :class:`PointerInput` / :class:`WheelInput` / :class:`KeyInput` — the
  event DTOs tools receive instead of raw Qt events.  They carry exactly
  the fields ever read across the seam; ``button`` / ``native_mods`` stay
  ints because the KeyMap chord model already takes ints
  (``mouse_button_chord(button, modifiers)``, ``matches(key, mods)``).

The Qt → DTO conversion lives Qt-side in ``gui.qt_inputs``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from protoboard.geometry import Point

__all__ = [
    "Intent",
    "KeyInput",
    "LEFT_BUTTON",
    "MIDDLE_BUTTON",
    "Mods",
    "PointerInput",
    "RIGHT_BUTTON",
    "WheelInput",
]

# ``Qt.MouseButton`` values (stable Qt ABI), named so Qt-free consumers can
# compare ``PointerInput.button`` without importing Qt.
LEFT_BUTTON = 0x1
RIGHT_BUTTON = 0x2
MIDDLE_BUTTON = 0x4


@dataclass(frozen=True, slots=True)
class Mods:
    """Keyboard modifiers active during an input event (Qt-free snapshot).

    The view extracts these from the Qt event once; controllers interpret
    them themselves (the meaning is controller-specific, so the tool never
    decodes them into intents).
    """

    ctrl: bool = False
    shift: bool = False
    alt: bool = False

    @property
    def none(self) -> bool:
        """True when no modifier is held (a plain click / move)."""
        return not (self.ctrl or self.shift or self.alt)


class Intent(Enum):
    """A semantic interaction intent the tool delivers via ``on_event``.

    Translated from a key press by the tool's keybinding layer, so controllers
    stay independent of the user's actual key mapping.  Kept to the small set an
    in-flight interaction needs to be *driven* — selection/navigation actions
    stay in the tool, not here.
    """

    CONFIRM = "confirm"
    """Finalize the interaction (e.g. Enter ends a pick / commits a floating drag)."""
    CANCEL = "cancel"
    """Abort the interaction (e.g. Esc)."""
    UNDO = "undo"
    """Revise — drop the last collected step (e.g. Backspace during a pick)."""


@dataclass(frozen=True, slots=True)
class PointerInput:
    """One pointer event crossing the seam (press / move / release / click)."""

    view_pos: Point
    """Viewport-pixel position (was ``event.pos()``)."""
    scene_pos: Point
    """Scene (raster) position, pre-mapped by the view."""
    button: int
    """``Qt.MouseButton`` value — the KeyMap chords already take ints."""
    native_mods: int
    """Exact modifier mask for KeyMap chord resolution."""
    mods: Mods
    """Decoded modifiers for controller semantics."""


@dataclass(frozen=True, slots=True)
class WheelInput:
    """One wheel tick crossing the seam."""

    delta_y: int
    """``angleDelta().y()``; notch accumulation stays with the consumer."""
    view_pos: Point
    native_mods: int
    mods: Mods


@dataclass(frozen=True, slots=True)
class KeyInput:
    """One key press/release crossing the seam."""

    key: int
    native_mods: int
    is_auto_repeat: bool
    mods: Mods
