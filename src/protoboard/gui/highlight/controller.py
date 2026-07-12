# SPDX-License-Identifier: MIT
"""``HighlightController`` — drive the highlight-stack dimming from Selectables.

A thin service that turns a set of
:class:`~protoboard.gui.selection.selectable.Selectable` entities into an
auto-tier :class:`~protoboard.gui.highlight_state.HighlightEntry` on the shared
:class:`~protoboard.gui.highlight_state.HighlightStack`.  ``BoardView``
already subscribes to the stack's ``changed`` signal and applies opacity dimming,
so the controller never touches the scene — it only publishes "what is
highlighted" and the existing pipeline does the rest.

Each controller owns one ``source_id`` on the stack's auto tier (at most one
auto entry per source — see :meth:`HighlightStack.push_auto`).  :meth:`show`
replaces it; :meth:`clear` removes it.

The controller is headless: it constructs pure ``HighlightEntry`` /
``HighlightProfile`` values and calls the stack; no QApplication needed to test
the entry it publishes.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from protoboard.gui.highlight_state import HighlightEntry
from protoboard.gui.interaction_mode import HighlightProfile

if TYPE_CHECKING:
    from collections.abc import Iterable

    from protoboard.gui.highlight_state import HighlightStack
    from protoboard.gui.selection.selectable import Selectable
    from protoboard.types import NetId


class HighlightController:
    """Publish a Selectable set as the stack's auto-tier highlight.

    Default client is SelectTool, but any tool can own one to highlight its
    own entities without re-implementing the dimming plumbing.
    """

    def __init__(
        self, stack: HighlightStack, *, source_id: str = "select_tool"
    ) -> None:
        self._stack = stack
        self._source_id = source_id

    @property
    def source_id(self) -> str:
        """The auto-tier key this controller owns on the stack."""
        return self._source_id

    def show(
        self,
        selectables: Iterable[Selectable],
        *,
        profile: HighlightProfile | None = None,
        net_id: NetId | None = None,
    ) -> None:
        """Highlight *selectables* — publish them as this source's auto entry.

        Splits the entities into component / wire id sets (via each
        :meth:`Selectable.to_hit`, the same kind bridge the FSM uses) and pushes
        one ``HighlightEntry``.  An empty set clears the entry instead, so a
        deselect collapses the highlight rather than dimming everything to an
        empty target.  ``profile`` defaults to the neutral dimming profile;
        ``net_id`` lets ``BoardView`` expand a net to its members when set.
        """
        comp_ids, wire_ids = self._split(selectables)
        if not comp_ids and not wire_ids and net_id is None:
            self.clear()
            return
        entry = HighlightEntry(
            comp_ids=comp_ids,
            wire_ids=wire_ids,
            net_id=net_id,
            profile=profile if profile is not None else HighlightProfile(),
            label=f"Auto ({self._source_id})",
        )
        self._stack.push_auto(entry, source_id=self._source_id)

    def clear(self) -> None:
        """Remove this source's auto entry (no-op if absent)."""
        self._stack.clear_auto(self._source_id)

    @staticmethod
    def _split(
        selectables: Iterable[Selectable],
    ) -> tuple[frozenset[uuid.UUID], frozenset[uuid.UUID]]:
        """Project Selectables onto (component-ids, wire-ids) for the stack.

        The stack speaks ids, so we read each entity's FSM ``Hit`` and route
        its id by kind.  ``group`` hits expand to nothing here — group dimming
        is driven by the group's member components, which the caller supplies
        as component Selectables (Phase 2 group selection); a bare group hit
        carries no comp/wire id of its own.
        """
        from protoboard.services.editing.item_handle import ItemTag

        comp_ids: set[uuid.UUID] = set()
        wire_ids: set[uuid.UUID] = set()
        for sel in selectables:
            iid = sel.to_hit().item_id()
            if iid is None:
                continue  # test stand-ins may use non-UUID ids
            if iid.tag is ItemTag.component:
                comp_ids.add(iid.uuid)
            elif iid.tag is ItemTag.wire:
                wire_ids.add(iid.uuid)
            # group — no own id to dim; members come in as component hits.
        return frozenset(comp_ids), frozenset(wire_ids)
