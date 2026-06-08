# SPDX-License-Identifier: MIT
"""Explicit state machine for the LMB + Shift+LMB + Esc + empty-canvas
click pipeline.

The pre-FSM code kept selection state in a handful of independent fields
on ``SelectTool`` (``_selected_id``, ``_selected_ids``, ``_selected_wire_id``,
…) and treated Esc as "clear everything".  The user formalised the
intended interaction in ``docs/highlight-state-machine.txt`` — a stack
of four layers (``normal``, ``highlight``, ``selection``,
``multi_selection``) where each click pushes or replaces a layer and Esc
pops exactly one.  This module owns that machine; ``SelectTool`` (Phase 2)
becomes a thin observer that mirrors the FSM's :class:`SelectionState`
onto the existing storage fields.

No Qt imports — the FSM is pure-Python so it can be unit-tested without
spinning up a ``QApplication``.  Observers receive a snapshot of the new
state via the :attr:`SelectionFSM.changed` callback registry.

Layer semantics (user-confirmed 2026-06-08):

* ``NORMAL``       — empty stack; nothing selected, no highlight.
* ``HIGHLIGHT``    — a relation set is being shown; ``highlight_source``
                     is the object whose relation drove the set.  No
                     "primary" object — the user has deselected but kept
                     the visual context.
* ``SELECTION``    — single primary object plus its highlight relation.
                     ``primary`` is set; ``multi`` is empty.
* ``MULTI_SELECTION`` — primary + an explicitly extended set.  All members
                     belong to the same relation as ``primary``; a
                     Shift+LMB on a different relation resets the layer.

Pop rules ([esc] or empty-canvas click) prune state in order:

* drop from ``MULTI_SELECTION`` → clear ``multi`` only;
* drop from ``SELECTION``       → clear ``primary``;
* drop from ``HIGHLIGHT``       → clear ``highlight_source`` + ``relation``;
* drop from ``NORMAL``          → no-op.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import IntEnum
from typing import Literal

from protoboard.gui.selection_axes import RelationClass


# ---------------------------------------------------------------------------
# Hit — the FSM's currency: "which kind of object was just clicked"
# ---------------------------------------------------------------------------


HitKind = Literal["component", "wire", "group"]


@dataclass(frozen=True, slots=True)
class Hit:
    """Identity of an object the user clicked.

    The FSM compares hits by equality, so two LMB-clicks on the same
    object end up routed through the "deselect" transition.  ``id`` is
    intentionally typed loosely (``object``) — domain code passes a
    ``uuid.UUID``; tests use plain strings.  All we need is hashability
    and equality.
    """

    kind: HitKind
    id: object

    def __post_init__(self) -> None:
        # ``frozen=True`` blocks setattr, so we validate here.
        if self.kind not in ("component", "wire", "group"):
            raise ValueError(f"unknown Hit.kind: {self.kind!r}")


# ---------------------------------------------------------------------------
# Click interaction enums — spec rev 2 (docs/highlight-state-machine.txt)
# ---------------------------------------------------------------------------


class BasisScope(IntEnum):
    """Which set a click contributes to the selection op.

    ``SINGLE``   — only ``{hit}``.
    ``RELATION`` — ``relation(hit)`` per current ``RelationClass`` (the
                   backtick-cycled enum).  When ``RelationClass.NONE``
                   is active, ``RELATION`` collapses to ``SINGLE``.

    Mapped from modifier keys at the call site (Alt → ``RELATION``,
    else ``SINGLE``).  Spec rev 2, Axis A.
    """

    SINGLE = 0
    RELATION = 1


class SetOp(IntEnum):
    """What we do with the basis relative to the current selection.

    ``SET``    — ``selection := basis``.  No modifier.
    ``ADD``    — ``selection := selection ∪ basis``.  Ctrl.
    ``REMOVE`` — ``selection := selection \\ basis``.  Ctrl+Shift.

    Spec rev 2, Axis B.
    """

    SET = 0
    ADD = 1
    REMOVE = 2


# ---------------------------------------------------------------------------
# Stack layer enum — ordered so ``int(layer) - 1`` is the pop target
# ---------------------------------------------------------------------------


class StackLayer(IntEnum):
    """Selection-FSM layer ordering.  ``IntEnum`` so callers can write
    ``state.depth >= StackLayer.SELECTION`` for predicate checks."""

    NORMAL = 0
    HIGHLIGHT = 1
    SELECTION = 2
    MULTI_SELECTION = 3


# ---------------------------------------------------------------------------
# Public state snapshot — handed to observers via ``changed`` callbacks
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SelectionState:
    """Immutable snapshot of the FSM's current state.

    ``SelectionFSM`` mutates internal mutable fields, then emits a frozen
    snapshot.  Observers must not mutate the snapshot — copy and rebuild
    if you need a derivative.

    Attributes:
        depth: top layer of the stack.
        primary: clicked object whose relation drives the highlight, or
            ``None`` when ``depth < SELECTION``.
        multi: extra objects extended into the selection at the
            ``MULTI_SELECTION`` layer.  Always empty below that depth.
            Stored as a frozenset so the snapshot is fully immutable.
        highlight_source: the object the highlight relation was computed
            from.  Survives a ``SELECTION → HIGHLIGHT`` pop so the user
            can drop primary while keeping the visual context.
        relation: ``RelationClass`` captured at the moment the layer was
            entered.  Cached instead of re-read live so a relation-mode
            change later doesn't retroactively rewrite the visible set.
    """

    depth: StackLayer = StackLayer.NORMAL
    primary: Hit | None = None
    multi: frozenset[Hit] = field(default_factory=frozenset)
    highlight_source: Hit | None = None
    relation: RelationClass = RelationClass.NONE


# ---------------------------------------------------------------------------
# Same-relation predicate — pluggable so the FSM can stay domain-free
# ---------------------------------------------------------------------------


#: Signature: ``(hit, relative_to) -> bool``.  Returns True when ``hit``
#: belongs to the same relation set as ``relative_to`` under the active
#: ``RelationClass``.  SelectTool installs a real implementation;
#: ``DEFAULT_SAME_RELATION_PREDICATE`` below is the trivial "same id"
#: fallback used by the unit tests.
SameRelationPredicate = Callable[[Hit, Hit, RelationClass], bool]


def _trivial_same_relation(
    hit: Hit, relative_to: Hit, relation: RelationClass,
) -> bool:
    """Default predicate for tests / no-graph environments: two hits are
    in the same relation iff they are the same object."""
    del relation  # unused in the trivial implementation
    return hit == relative_to


DEFAULT_SAME_RELATION_PREDICATE: SameRelationPredicate = _trivial_same_relation


# ---------------------------------------------------------------------------
# The FSM
# ---------------------------------------------------------------------------


class SelectionFSM:
    """Drives transitions between :class:`StackLayer` values.

    The FSM never touches Qt or SelectTool's storage.  Observers wired
    up via :meth:`subscribe` receive the new :class:`SelectionState`
    after every transition.  The previous state is also passed so the
    observer can detect specific edges (e.g. "MULTI_SELECTION dropped
    to SELECTION") without recomputing diffs.

    Construction:

        fsm = SelectionFSM(same_relation=my_predicate)

    The ``same_relation`` callable is consulted when deciding whether a
    Shift+LMB hit extends the existing multi-selection or resets to a
    new relation.  See :data:`SameRelationPredicate`.
    """

    def __init__(
        self,
        *,
        same_relation: SameRelationPredicate | None = None,
        initial_relation: RelationClass = RelationClass.SAME_LOGICAL_NET,
    ) -> None:
        self._state = SelectionState()
        self._same_relation = same_relation or DEFAULT_SAME_RELATION_PREDICATE
        self._default_relation = initial_relation
        self._listeners: list[
            Callable[[SelectionState, SelectionState], None]
        ] = []

    # ------------------------------------------------------------------
    # Observer wiring
    # ------------------------------------------------------------------

    def subscribe(
        self, listener: Callable[[SelectionState, SelectionState], None],
    ) -> None:
        """Register a listener called as ``listener(old, new)`` after
        every transition (including no-op transitions, so observers can
        treat the call as "the FSM has settled, re-sync now")."""
        self._listeners.append(listener)

    def current(self) -> SelectionState:
        """Return the current (immutable) state snapshot."""
        return self._state

    def set_default_relation(self, relation: RelationClass) -> None:
        """Update the relation used for newly-entered HIGHLIGHT layers.
        Does NOT rewrite the relation captured by an active layer — that
        would surprise the user mid-interaction."""
        self._default_relation = relation

    # ------------------------------------------------------------------
    # Transitions
    # ------------------------------------------------------------------

    def step_lmb(self, hit: Hit | None) -> None:
        """Handle a plain LMB click.  ``hit=None`` means empty-canvas;
        per user spec it folds onto :meth:`step_esc`."""
        if hit is None:
            self.step_esc()
            return
        st = self._state
        depth = st.depth
        if depth in (StackLayer.NORMAL, StackLayer.HIGHLIGHT):
            self._enter_selection(hit)
            return
        if depth == StackLayer.SELECTION:
            if hit == st.primary:
                # Click the same primary again → drop the SELECTION layer
                # but keep HIGHLIGHT (the user can still see the relation
                # context until they Esc once more).
                self._pop_to_highlight()
            else:
                self._switch_primary(hit)
            return
        # depth == MULTI_SELECTION
        if hit == st.primary:
            # User clicked the primary inside an extended selection.
            # Collapse both layers at once — otherwise the user would
            # have to click twice to get back to the highlight context.
            self._collapse_to_highlight()
        else:
            self._collapse_to_selection(hit)

    def step_shift_lmb(self, hit: Hit | None) -> None:
        """Handle Shift+LMB.  Empty-canvas folds onto :meth:`step_esc`."""
        if hit is None:
            self.step_esc()
            return
        st = self._state
        depth = st.depth
        if depth in (StackLayer.NORMAL, StackLayer.HIGHLIGHT):
            self._enter_multi(hit)
            return
        if depth == StackLayer.SELECTION:
            primary = st.primary
            assert primary is not None
            if self._same_relation(hit, primary, st.relation):
                self._extend_to_multi(hit)
            else:
                # Cross-net Shift in SELECTION resets to the new
                # relation, mirroring the NORMAL→MULTI shortcut.
                self._enter_multi(hit)
            return
        # depth == MULTI_SELECTION
        primary = st.primary
        assert primary is not None
        if hit in st.multi or hit == primary:
            # Shift on a member of the current set → deselect the whole
            # set (FSM spec: ``[..., multi] -Shift{same-set}-> [normal, highlight]``).
            self._collapse_to_highlight()
        elif self._same_relation(hit, primary, st.relation):
            self._extend_to_multi(hit)
        else:
            self._enter_multi(hit)

    def step_esc(self) -> None:
        """Pop one stack layer.  No-op at ``NORMAL``."""
        st = self._state
        if st.depth == StackLayer.NORMAL:
            self._emit(st)            # still emit so observers can resync
            return
        if st.depth == StackLayer.MULTI_SELECTION:
            self._set(replace(
                st,
                depth=StackLayer.SELECTION,
                multi=frozenset(),
            ))
            return
        if st.depth == StackLayer.SELECTION:
            self._set(replace(
                st,
                depth=StackLayer.HIGHLIGHT,
                primary=None,
            ))
            return
        # depth == HIGHLIGHT
        self._set(SelectionState())   # back to all defaults

    # ------------------------------------------------------------------
    # Transition helpers — all of these end with ``self._set(...)``
    # ------------------------------------------------------------------

    def _enter_selection(self, hit: Hit) -> None:
        self._set(SelectionState(
            depth=StackLayer.SELECTION,
            primary=hit,
            highlight_source=hit,
            relation=self._default_relation,
        ))

    def _enter_multi(self, hit: Hit) -> None:
        self._set(SelectionState(
            depth=StackLayer.MULTI_SELECTION,
            primary=hit,
            multi=frozenset({hit}),
            highlight_source=hit,
            relation=self._default_relation,
        ))

    def _switch_primary(self, hit: Hit) -> None:
        # SELECTION → SELECTION: replace primary; reseat the highlight
        # source so the relation set updates to the new object.
        self._set(SelectionState(
            depth=StackLayer.SELECTION,
            primary=hit,
            highlight_source=hit,
            relation=self._default_relation,
        ))

    def _pop_to_highlight(self) -> None:
        st = self._state
        self._set(replace(
            st,
            depth=StackLayer.HIGHLIGHT,
            primary=None,
        ))

    def _collapse_to_highlight(self) -> None:
        # MULTI_SELECTION → HIGHLIGHT: drop both multi and primary.
        st = self._state
        self._set(replace(
            st,
            depth=StackLayer.HIGHLIGHT,
            primary=None,
            multi=frozenset(),
        ))

    def _collapse_to_selection(self, hit: Hit) -> None:
        # MULTI_SELECTION → SELECTION on a different object.  The FSM
        # spec for ``lmb-click{other}`` at MULTI is "drop multi, switch
        # primary to other".
        self._set(SelectionState(
            depth=StackLayer.SELECTION,
            primary=hit,
            highlight_source=hit,
            relation=self._default_relation,
        ))

    def _extend_to_multi(self, hit: Hit) -> None:
        st = self._state
        if st.depth == StackLayer.SELECTION:
            new_multi = frozenset({st.primary, hit}) if st.primary else frozenset({hit})
            self._set(replace(
                st,
                depth=StackLayer.MULTI_SELECTION,
                multi=new_multi,
            ))
            return
        # already in MULTI_SELECTION — just grow the multi set
        self._set(replace(
            st,
            multi=st.multi | {hit},
        ))

    # ------------------------------------------------------------------
    # Internal: emit ``changed`` callbacks atomically around state swap
    # ------------------------------------------------------------------

    def _set(self, new_state: SelectionState) -> None:
        old = self._state
        self._state = new_state
        self._emit(old)

    def _emit(self, old: SelectionState) -> None:
        for listener in self._listeners:
            listener(old, self._state)


__all__ = [
    "Hit",
    "HitKind",
    "SameRelationPredicate",
    "SelectionFSM",
    "SelectionState",
    "StackLayer",
]
