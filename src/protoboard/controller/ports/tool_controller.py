# SPDX-License-Identifier: MIT
"""``ToolController`` — the verb surface the View drives (docs MVC step 1).

The View (``ToolHost`` + ``BoardView``) owns everything physical: Qt events,
mapping, scrollbars, timers/throttling, and the ``KeyMap`` that turns a
key/chord into an ``action-id``.  It translates all of that into a small
vocabulary of **semantic verbs** in model coordinates and hands them to the
active tool through this protocol:

* :meth:`default_action` — the primary click (LMB by default);
* :meth:`secondary_action` — the context click (RMB by default);
* :meth:`empty_action` — a (throttled) hover/move.

The boundary rule: a headless test drives the app as a sequence of these verbs
with :class:`~protoboard.geometry.Point` (already in model units) and
:class:`~protoboard.controller.ports.inputs.Mods`; anything it must fake or bypass —
Qt events, the ``KeyMap``, throttling — is *View*, not *Controller*.

:attr:`state` is a first-class, assertable Controller fact (e.g. ``"select.idle"``
vs ``"move.float.rotate"``): a test can assert it directly, including the
negative case (arming rotate with no selection leaves the state ``select.idle``).
It is not merely an input-layer concern — the View happens to consume it to pick
a ``KeyMap`` layer, but the source is the domain state of the interaction.

Concrete tools implement it via the default adapters on
:class:`~protoboard.gui.board_view_tool.ToolSurface`, which delegate to the existing
``on_press`` / ``on_move`` / ``on_right_press`` hooks — so introducing this
protocol changes no behaviour on its own (parity before swap, CLAUDE.md §8).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from protoboard.controller.ports.hover import HoverEvent
    from protoboard.controller.ports.inputs import Mods
    from protoboard.geometry import Point


@runtime_checkable
class ToolController(Protocol):
    """The semantic-verb surface of an interaction tool."""

    def default_action(self, point: Point, mods: Mods) -> bool:
        """Primary action at *point* (LMB).  True if the tool consumed it."""
        ...

    def secondary_action(self, point: Point, mods: Mods) -> bool:
        """Context action at *point* (RMB).

        Returns True when the tool published an item-aware context; False on
        bare canvas so the View falls through to the generic canvas context.
        """
        ...

    def empty_action(self, point: Point, mods: Mods) -> HoverEvent:
        """A hover/move at *point*; reply with what is under the cursor."""
        ...

    @property
    def state(self) -> str:
        """The current interaction state (e.g. ``"select.idle"``)."""
        ...
