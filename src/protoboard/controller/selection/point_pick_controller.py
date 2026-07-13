# SPDX-License-Identifier: MIT
"""``PointPickController`` — a click-by-click pick as a MouseInputController.

A *pick* asks the user to click a sequence of things (wire vertices, pad
waypoints) with a live preview and a correction phase — the EditWire / redrag
use-case.  The acquisition state machine already exists as
:class:`~protoboard.controller.selection.service.PickSession` (feed / undo / confirm /
cancel / on_progress, with the ``Stop`` termination strategy).  This controller
is the thin adapter that lets that machine ride the same
:class:`~protoboard.controller.selection.mouse_input.MouseInputController` routing the
tool uses for drag and rubber-band — so the tool forwards every interaction
through one ``self._mouse`` slot, with no branching on which kind it is.

It is *click-driven*, not press-drag-release: each :meth:`on_click` resolves the
clicked point into a :class:`Selectable` and feeds the session; ``CONFIRM`` /
``UNDO`` / ``CANCEL`` intents map onto the session's correction-phase methods.
Resolving a click into a ``Selectable`` is delegated to an injected ``resolve``
callback (different per tool / configurable per user — a wire-edit pick resolves
to a wire vertex, a reroute pick to a snapped pad), so the controller stays
generic and headless: a test drives it with a fake resolver and ``Point`` /
``Mods`` / ``Intent``, no ``QApplication``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from protoboard.controller.selection.mouse_input import Intent

if TYPE_CHECKING:
    from collections.abc import Callable

    from protoboard.geometry import Point
    from protoboard.controller.selection.mouse_input import Mods
    from protoboard.controller.selection.selectable import Selectable
    from protoboard.controller.selection.service import PickSession

    Resolve = Callable[["Point", Mods], "Selectable | None"]
    """``(scene_pt, mods) -> Selectable | None`` — the clicked entity, or None
    when the click missed (a no-op, never a fall-through to selection)."""

__all__ = ["PointPickController"]


class PointPickController:
    """Adapt a :class:`PickSession` to the MouseInputController protocol.

    Constructed (armed) by the tool when a pick begins; wraps the session the
    pick service created and routes pointer + intent events onto it.
    """

    def __init__(
        self,
        session: PickSession,
        resolve: Resolve,
        *,
        on_preview: Callable[[Point, Mods], None] | None = None,
    ) -> None:
        self._session = session
        self._resolve = resolve
        self._on_preview = on_preview

    # -- MouseInputController -----------------------------------------------

    def on_press(self, scene_pt: Point, mods: Mods) -> bool:
        """A pick acts on clicks, not raw presses — consumed but no-op."""
        return True

    def on_move(self, scene_pt: Point, mods: Mods) -> None:
        """Drive the live cursor preview (the session redraws on feed/undo)."""
        if self._on_preview is not None:
            self._on_preview(scene_pt, mods)

    def on_release(self, scene_pt: Point, mods: Mods) -> None:
        """Picks finalize on click / intent, not on release — nothing to do."""

    def on_click(self, scene_pt: Point, mods: Mods) -> bool:
        """Resolve the clicked point and feed it to the session.

        A missed click (resolver returns None) is swallowed — staying inside the
        pick rather than falling through to selection.
        """
        if self._session.finished:
            return True
        sel = self._resolve(scene_pt, mods)
        if sel is not None:
            self._session.feed(sel)
        return True

    def on_double_click(self, scene_pt: Point, mods: Mods) -> bool:
        """No double-click meaning during a pick — swallow it."""
        return True

    def on_event(self, intent: Intent) -> bool:
        """Map CONFIRM / UNDO / CANCEL onto the session's correction phase."""
        if self._session.finished:
            return False
        if intent is Intent.CONFIRM:
            return self._session.confirm()
        if intent is Intent.UNDO:
            return self._session.undo()
        if intent is Intent.CANCEL:
            self._session.cancel()
            return True
        return False

    @property
    def finished(self) -> bool:
        return self._session.finished

    @property
    def session(self) -> PickSession:
        """The wrapped pick session (for the tool's status / introspection)."""
        return self._session
