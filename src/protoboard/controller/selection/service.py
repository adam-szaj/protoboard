# SPDX-License-Identifier: MIT
"""``SelectionService`` — SelectTool's selection state and pick API as a service.

Two ways to consume selection:

* **Passive** — "what is selected?" / "what is under the cursor?".  Tools and
  commands read :meth:`SelectionService.selected` (and the id helpers) without
  driving any interaction.
* **Active pick** — "ask the user for a new selection".  A tool calls
  :meth:`request_pick` (or the awaitable :meth:`request_pick_async`); the
  service enters a *pick mode*, the new SelectTool routes board clicks into the
  live :class:`PickSession`, and when the :class:`Stop` condition is met the
  ``on_done`` callback fires with the collected :class:`Selectable` list.  This
  is what makes SelectTool a *service*: e.g. EditWire asks for two wire-vertices
  and then a confirm-terminated run of pad waypoints, instead of re-implementing
  hit-testing and a click loop.

The service wraps the pure :class:`~protoboard.controller.selection_fsm.SelectionFSM`
for the passive selection state, and owns the pick machinery itself.  It holds
no Qt — highlighting is :class:`HighlightController`'s job (the stack-based
dimming pipeline), never the service's.  This keeps the service and its pick
state machine headless-testable.
"""

from __future__ import annotations

from concurrent.futures import Future
from typing import TYPE_CHECKING

from protoboard.controller.selection.selectable import Selectable, Stop
from protoboard.controller.selection_fsm import (
    Hit,
    SelectionFSM,
    SelectionState,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from protoboard.controller.selection.selectable import PickKind


class PickSession:
    """One in-flight ``request_pick`` run.

    Created by :meth:`SelectionService.request_pick` and fed one
    :class:`Selectable` per qualifying board click via :meth:`feed`.  Ends
    when the :class:`Stop` condition is satisfied (``feed`` returns done), the
    user confirms an open-ended (``Stop.confirm``) run via :meth:`confirm`, or
    the user cancels via :meth:`cancel`.  Exactly one of ``on_done`` /
    ``on_cancel`` fires, once.
    """

    def __init__(
        self,
        what: PickKind,
        stop: Stop,
        on_done: Callable[[list[Selectable]], None],
        on_cancel: Callable[[], None] | None,
        on_progress: Callable[[list[Selectable]], None] | None,
    ) -> None:
        self.what = what
        self.stop = stop
        self._on_done = on_done
        self._on_cancel = on_cancel
        self._on_progress = on_progress
        self._collected: list[Selectable] = []
        self._finished = False

    @property
    def collected(self) -> list[Selectable]:
        """The items picked so far (a copy; callers must not mutate state)."""
        return list(self._collected)

    @property
    def finished(self) -> bool:
        """True once ``on_done`` or ``on_cancel`` has fired."""
        return self._finished

    def feed(self, item: Selectable) -> bool:
        """Add one picked *item*; return True iff the pick is now complete.

        Fires ``on_progress`` after appending, then ``on_done`` if the stop
        condition is met.  ``Stop.confirm`` / ``Stop.count_confirm`` runs never
        auto-complete here — they wait for :meth:`confirm`; a ``count_confirm``
        run at capacity ignores further items (revise with :meth:`undo` first).
        """
        if self._finished:
            return True
        if self.stop.at_capacity(self._collected):
            return False  # capped — drop the click until one is undone
        self._collected.append(item)
        if self._on_progress is not None:
            self._on_progress(self.collected)
        if self.stop.satisfied(self._collected):
            self._complete()
            return True
        return False

    def undo(self) -> bool:
        """Drop the most recently picked item; return True if one was removed.

        Lets the user correct a pick before finalizing — only meaningful for
        revisable runs (``confirm`` / ``count_confirm``).  Fires ``on_progress``
        with the shortened list so the client can redraw its preview.
        """
        if self._finished or not self.stop.revisable or not self._collected:
            return False
        self._collected.pop()
        if self._on_progress is not None:
            self._on_progress(self.collected)
        return True

    def confirm(self) -> bool:
        """Finalize a user-confirmed run; return True if it ended.

        Ends ``confirm`` and ``count_confirm`` runs (the user pressed Enter).
        No-op (returns False) for ``count`` / ``until`` runs, which only end
        through :meth:`feed`.
        """
        if self._finished or not self.stop.user_confirmable:
            return False
        self._complete()
        return True

    @property
    def at_capacity(self) -> bool:
        """True when a ``count_confirm`` run has its full count (awaiting Enter)."""
        return self.stop.at_capacity(self._collected)

    def cancel(self) -> None:
        """Abort the pick — fires ``on_cancel`` (once), discards collected items."""
        if self._finished:
            return
        self._finished = True
        if self._on_cancel is not None:
            self._on_cancel()

    def _complete(self) -> None:
        self._finished = True
        self._on_done(self.collected)


class SelectionService:
    """Selection state + active-pick API, injected into tools at ``activate``.

    Lives per session; the new SelectTool mutates selection through it and
    routes clicks into the active :class:`PickSession` while a pick is live.
    Commands receive ids explicitly (the service supplies them in the GUI
    layer) — it is not imported by the domain.
    """

    def __init__(self, fsm: SelectionFSM | None = None) -> None:
        self._fsm = fsm if fsm is not None else SelectionFSM()
        # Most-recent ``Selectable`` per FSM ``Hit`` identity, so passive
        # reads return rich adapters rather than re-deriving them from ids.
        self._by_hit: dict[Hit, Selectable] = {}
        self._active_pick: PickSession | None = None

    # -- passive: what is selected -----------------------------------------

    @property
    def fsm(self) -> SelectionFSM:
        """The wrapped pure selection FSM (state + transitions)."""
        return self._fsm

    def state(self) -> SelectionState:
        """Current FSM snapshot (depth, primary, multi, relation)."""
        return self._fsm.current()

    def selected(self) -> list[Selectable]:
        """Every selected entity as a :class:`Selectable`.

        Primary first (when set), then the multi set, de-duplicated by FSM
        ``Hit``.  Entities the service never saw as a ``Selectable`` (e.g.
        restored from a serialized hit) are skipped — passive consumers that
        need ids should use the id helpers instead.
        """
        st = self._fsm.current()
        hits: list[Hit] = []
        if st.primary is not None:
            hits.append(st.primary)
        hits.extend(h for h in st.multi if h != st.primary)
        out: list[Selectable] = []
        for h in hits:
            sel = self._by_hit.get(h)
            if sel is not None:
                out.append(sel)
        return out

    def is_empty(self) -> bool:
        """True when nothing is selected (FSM at NORMAL/HIGHLIGHT, no primary)."""
        return self._fsm.current().primary is None and not self._fsm.current().multi

    # -- passive: mutate selection -----------------------------------------

    def set(self, sel: Selectable) -> None:
        """Replace the selection with *sel* (SET op via the FSM)."""
        self._remember(sel)
        self._fsm.step_lmb(sel.to_hit())

    def add(self, sel: Selectable) -> None:
        """Extend the selection with *sel* (ADD op via the FSM)."""
        self._remember(sel)
        self._fsm.step_shift_lmb(sel.to_hit())

    def set_many(self, selectables: list[Selectable]) -> None:
        """Replace the selection with an explicit *set* of entities.

        Unlike :meth:`add` (Shift-click, which only extends within the same
        relation), this is a relation-agnostic batch — the rubber-band's "these
        exact items" semantics.  Empty list clears.  The first becomes primary,
        the rest extend regardless of relation.
        """
        if not selectables:
            self.clear()
            return
        first, *rest = selectables
        self.set(first)
        if not rest:
            return
        # Force the extend regardless of the relation predicate: the band is an
        # explicit set, not a relation expansion.
        prev = self._fsm._same_relation  # noqa: SLF001 — same-package seam
        self._fsm._same_relation = lambda *_a: True  # noqa: SLF001
        try:
            for sel in rest:
                self._remember(sel)
                self._fsm.step_shift_lmb(sel.to_hit())
        finally:
            self._fsm._same_relation = prev  # noqa: SLF001

    def clear(self) -> None:
        """Pop one selection layer (Esc semantics)."""
        self._fsm.step_esc()

    def remember(self, sel: Selectable) -> None:
        """Record *sel* so a later passive read returns the rich adapter.

        Used by the tool when a hit's :class:`Selectable` is known but the
        selection mutation goes through a lower-level FSM op.
        """
        self._remember(sel)

    def _remember(self, sel: Selectable) -> None:
        self._by_hit[sel.to_hit()] = sel

    # -- active: request a pick --------------------------------------------

    @property
    def active_pick(self) -> PickSession | None:
        """The in-flight pick, or ``None``.  The tool routes clicks here."""
        return self._active_pick

    def request_pick(
        self,
        what: PickKind,
        stop: Stop,
        on_done: Callable[[list[Selectable]], None],
        on_cancel: Callable[[], None] | None = None,
        on_progress: Callable[[list[Selectable]], None] | None = None,
    ) -> PickSession:
        """Enter pick mode: ask the user to click *what* until *stop*.

        The new SelectTool feeds qualifying clicks into the returned session;
        ``on_progress`` fires per pick, then exactly one of ``on_done`` (stop
        met / confirmed) or ``on_cancel`` (Esc).  Starting a new pick cancels
        any pick already in flight.
        """
        if self._active_pick is not None and not self._active_pick.finished:
            self._active_pick.cancel()

        def _done(items: list[Selectable]) -> None:
            self._active_pick = None
            on_done(items)

        def _cancel() -> None:
            self._active_pick = None
            if on_cancel is not None:
                on_cancel()

        session = PickSession(
            what=what,
            stop=stop,
            on_done=_done,
            on_cancel=_cancel,
            on_progress=on_progress,
        )
        self._active_pick = session
        return session

    def request_pick_async(
        self,
        what: PickKind,
        stop: Stop,
        *,
        on_progress: Callable[[list[Selectable]], None] | None = None,
    ) -> Future[list[Selectable]]:
        """Awaitable façade over :meth:`request_pick`.

        Returns a :class:`concurrent.futures.Future` that resolves with the
        picked list (or is cancelled when the user aborts).  Built on the
        callback primitive — no event-loop rewrite — so a caller can write
        linear code (``await asyncio.wrap_future(svc.request_pick_async(...))``)
        while the synchronous tool runtime keeps driving clicks.
        """
        future: Future[list[Selectable]] = Future()

        def _done(items: list[Selectable]) -> None:
            if not future.done():
                future.set_result(items)

        def _cancel() -> None:
            if not future.done():
                future.cancel()

        self.request_pick(
            what, stop, on_done=_done, on_cancel=_cancel, on_progress=on_progress
        )
        return future
