# SPDX-License-Identifier: MIT
"""``MouseInputController`` — one protocol for every in-tool mouse interaction.

SelectTool drives several distinct interactions — dragging the selection,
rubber-band area-select, and click-by-click *picks* (wire vertices, pad
waypoints).  They have different shapes (a drag is one press→move→release; a pick
is N separate clicks then a confirm), but they share a *routing* need: while one
is active, the tool should forward raw pointer events to it and nothing else.

This protocol captures exactly that routing surface, so the tool holds a single
``self._mouse: MouseInputController | None`` and forwards uniformly — no
branching on which interaction is active.

Two deliberate boundaries keep the controllers headless and config-aware:

* **Modifiers travel as data, not Qt.**  Ctrl/Shift/Alt change what a click
  *means*, and that meaning is controller-specific (Ctrl = "add to selection"
  for selection, "re-pin the end node" for wire-edit).  So each pointer event
  carries a plain :class:`Mods` value object; the controller interprets it.  The
  tool never pre-translates modifiers (that would leak each controller's
  semantics into the tool).
* **Keys travel as intents, not keycodes.**  The app has a full, user-remappable
  keybinding layer; a controller must not hard-code ``Key_Return``.  The tool
  translates a key press into a semantic :class:`Intent` (via keybindings) and
  delivers it through :meth:`on_event`.  A controller reacts to ``CONFIRM`` /
  ``CANCEL`` / ``UNDO``, never to a raw key.

Pure protocol — no Qt.  Concrete controllers may hold Qt seams (overlays, the
view's ``snap``) injected at construction, but the protocol and its value types
are Qt-free, so the tool can depend on them and tests can drive a full
interaction with ``Point`` / ``Mods`` / ``Intent`` and no ``QApplication``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

# The value types moved to the seam's Qt-free port vocabulary (qt-ports §2.2);
# re-exported here so controller code keeps its historical import path.
from protoboard.gui.ports.inputs import Intent, Mods

if TYPE_CHECKING:
    from protoboard.geometry import Point

__all__ = ["Intent", "Mods", "MouseInputController"]


@runtime_checkable
class MouseInputController(Protocol):
    """One in-flight mouse interaction the tool routes pointer + intent events to.

    Constructed (armed) by the tool when an interaction begins; the tool then
    forwards every pointer event and translated intent here until
    :attr:`finished` is true.
    """

    def on_press(self, scene_pt: Point, mods: Mods) -> bool:
        """Handle a button press; return True if the interaction consumed it.

        Low-level: the start of a press-drag-release gesture.  Discrete
        interactions (picks) ignore this and act on :meth:`on_click` instead.
        """
        ...

    def on_move(self, scene_pt: Point, mods: Mods) -> None:
        """Handle pointer motion (live preview / provisional update)."""
        ...

    def on_release(self, scene_pt: Point, mods: Mods) -> None:
        """Handle button release (the end of a press-drag-release gesture)."""
        ...

    def on_click(self, scene_pt: Point, mods: Mods) -> bool:
        """Handle a click (press + release with no intervening drag).

        The higher-level event for discrete interactions: a pick collects one
        point per click.  Synthesised by the tool (as Qt and most GUI toolkits
        do) separately from the press/release pair, so a controller picks the
        granularity it needs — continuous gestures use press/move/release,
        click-driven ones use this.  Returns True if consumed.
        """
        ...

    def on_double_click(self, scene_pt: Point, mods: Mods) -> bool:
        """Handle a double-click (e.g. enter a group / open an editor).

        Delivered as its own event (Qt raises ``MouseButtonDblClick``); a
        controller that doesn't care returns False to let the tool handle it.
        """
        ...

    def on_event(self, intent: Intent) -> bool:
        """Handle a semantic intent (CONFIRM / CANCEL / UNDO).

        Returns True if the controller acted on it (so the tool can stop
        propagating); False to let the tool handle it otherwise.
        """
        ...

    @property
    def finished(self) -> bool:
        """True once the interaction has committed or been cancelled."""
        ...
