# SPDX-License-Identifier: MIT
"""Exhaustive transition tests for ``protoboard.controller.selection_fsm``.

The user formalised the state machine in ``docs/highlight-state-machine.txt``
on 2026-06-08.  Every transition from that file is covered here, plus the
two user-confirmed elaborations:

* **Empty-canvas click** folds onto ``step_esc`` (one layer popped).
* **Cross-net Shift+LMB** in ``SELECTION`` or ``MULTI_SELECTION``
  resets to the new object's relation instead of growing the existing
  multi-set.

The FSM is pure-Python; these tests run in milliseconds without Qt.
"""

from __future__ import annotations

import pytest

from protoboard.controller.selection_axes import RelationClass
from protoboard.controller.selection_fsm import (
    BasisScope,
    Hit,
    SelectionFSM,
    SelectionState,
    SetOp,
    StackLayer,
)


# ---------------------------------------------------------------------------
# Fixtures: tiny "world" of three components in two logical nets
# ---------------------------------------------------------------------------


# Net "A" contains comp_a1 + comp_a2; net "B" contains comp_b.
_NET_OF: dict[Hit, str] = {}
_a1 = Hit("component", "a1")
_a2 = Hit("component", "a2")
_b = Hit("component", "b")
_w_a = Hit("wire", "wire_a")
_NET_OF[_a1] = "A"
_NET_OF[_a2] = "A"
_NET_OF[_b] = "B"
_NET_OF[_w_a] = "A"


def _same_net(hit: Hit, relative_to: Hit, relation: RelationClass) -> bool:
    """Production-style same-relation predicate: two hits are 'related'
    iff they sit on the same logical net.  Mirrors the SAME_LOGICAL_NET
    semantics SelectTool will install."""
    del relation
    return _NET_OF.get(hit) == _NET_OF.get(relative_to)


@pytest.fixture
def fsm() -> SelectionFSM:
    """Standard FSM wired with the same-net predicate."""
    return SelectionFSM(same_relation=_same_net)


# ---------------------------------------------------------------------------
# Basics: construction defaults
# ---------------------------------------------------------------------------


class TestInitial:
    def test_starts_in_normal_layer(self, fsm: SelectionFSM) -> None:
        st = fsm.current()
        assert st.depth == StackLayer.NORMAL
        assert st.primary is None
        assert st.multi == frozenset()
        assert st.highlight_source is None
        assert st.relation == RelationClass.NONE


# ---------------------------------------------------------------------------
# LMB transitions from each starting depth
# ---------------------------------------------------------------------------


class TestLmbFromNormal:
    def test_lmb_enters_selection(self, fsm: SelectionFSM) -> None:
        fsm.step_lmb(_a1)
        st = fsm.current()
        assert st.depth == StackLayer.SELECTION
        assert st.primary == _a1
        assert st.highlight_source == _a1
        assert st.relation == RelationClass.SAME_LOGICAL_NET
        assert st.multi == frozenset()

    def test_empty_canvas_lmb_is_noop_at_normal(
        self, fsm: SelectionFSM,
    ) -> None:
        fsm.step_lmb(None)  # equivalent to Esc — no layer to pop
        st = fsm.current()
        assert st.depth == StackLayer.NORMAL


class TestLmbFromSelection:
    def test_lmb_same_object_deselects(self, fsm: SelectionFSM) -> None:
        fsm.step_lmb(_a1)
        fsm.step_lmb(_a1)  # same object → drop SELECTION → HIGHLIGHT
        st = fsm.current()
        assert st.depth == StackLayer.HIGHLIGHT
        assert st.primary is None
        assert st.highlight_source == _a1  # context retained
        assert st.relation == RelationClass.SAME_LOGICAL_NET

    def test_lmb_other_object_switches_primary(
        self, fsm: SelectionFSM,
    ) -> None:
        fsm.step_lmb(_a1)
        fsm.step_lmb(_b)
        st = fsm.current()
        assert st.depth == StackLayer.SELECTION
        assert st.primary == _b
        assert st.highlight_source == _b   # highlight reseats to new object

    def test_lmb_empty_canvas_pops_one_layer(self, fsm: SelectionFSM) -> None:
        fsm.step_lmb(_a1)
        fsm.step_lmb(None)
        st = fsm.current()
        assert st.depth == StackLayer.HIGHLIGHT
        assert st.primary is None


class TestLmbFromHighlight:
    def test_lmb_re_enters_selection(self, fsm: SelectionFSM) -> None:
        # Get into HIGHLIGHT by clicking and then clicking the same object.
        fsm.step_lmb(_a1)
        fsm.step_lmb(_a1)
        assert fsm.current().depth == StackLayer.HIGHLIGHT
        # New LMB on whatever object → fresh SELECTION.
        fsm.step_lmb(_b)
        st = fsm.current()
        assert st.depth == StackLayer.SELECTION
        assert st.primary == _b


class TestLmbFromMulti:
    def test_lmb_on_primary_collapses_to_highlight(
        self, fsm: SelectionFSM,
    ) -> None:
        fsm.step_shift_lmb(_a1)            # NORMAL → MULTI
        fsm.step_shift_lmb(_a2)            # grow multi within net A
        assert fsm.current().depth == StackLayer.MULTI_SELECTION
        fsm.step_lmb(_a1)                   # LMB on primary — collapses
        st = fsm.current()
        assert st.depth == StackLayer.HIGHLIGHT
        assert st.primary is None
        assert st.multi == frozenset()

    def test_lmb_on_other_object_drops_to_selection(
        self, fsm: SelectionFSM,
    ) -> None:
        fsm.step_shift_lmb(_a1)
        fsm.step_shift_lmb(_a2)
        fsm.step_lmb(_b)
        st = fsm.current()
        assert st.depth == StackLayer.SELECTION
        assert st.primary == _b
        assert st.multi == frozenset()


# ---------------------------------------------------------------------------
# Shift+LMB transitions
# ---------------------------------------------------------------------------


class TestShiftLmbFromNormal:
    def test_shift_enters_multi(self, fsm: SelectionFSM) -> None:
        fsm.step_shift_lmb(_a1)
        st = fsm.current()
        assert st.depth == StackLayer.MULTI_SELECTION
        assert st.primary == _a1
        assert st.multi == frozenset({_a1})
        assert st.highlight_source == _a1


class TestShiftLmbFromSelection:
    def test_shift_same_relation_extends_to_multi(
        self, fsm: SelectionFSM,
    ) -> None:
        fsm.step_lmb(_a1)
        fsm.step_shift_lmb(_a2)            # same net A
        st = fsm.current()
        assert st.depth == StackLayer.MULTI_SELECTION
        assert st.primary == _a1
        assert st.multi == frozenset({_a1, _a2})

    def test_shift_different_relation_resets(
        self, fsm: SelectionFSM,
    ) -> None:
        fsm.step_lmb(_a1)
        fsm.step_shift_lmb(_b)             # net B != A — reset
        st = fsm.current()
        assert st.depth == StackLayer.MULTI_SELECTION
        assert st.primary == _b
        assert st.multi == frozenset({_b})


class TestShiftLmbFromMulti:
    def test_shift_on_set_member_collapses_to_highlight(
        self, fsm: SelectionFSM,
    ) -> None:
        fsm.step_shift_lmb(_a1)
        fsm.step_shift_lmb(_a2)
        fsm.step_shift_lmb(_a2)            # already in set — collapse
        st = fsm.current()
        assert st.depth == StackLayer.HIGHLIGHT
        assert st.primary is None
        assert st.multi == frozenset()

    def test_shift_on_other_same_relation_grows_multi(
        self, fsm: SelectionFSM,
    ) -> None:
        fsm.step_shift_lmb(_a1)
        fsm.step_shift_lmb(_a2)
        fsm.step_shift_lmb(_w_a)           # also net A, new member
        st = fsm.current()
        assert st.depth == StackLayer.MULTI_SELECTION
        assert st.multi == frozenset({_a1, _a2, _w_a})

    def test_shift_cross_relation_resets_multi(
        self, fsm: SelectionFSM,
    ) -> None:
        fsm.step_shift_lmb(_a1)
        fsm.step_shift_lmb(_a2)
        fsm.step_shift_lmb(_b)             # net B — reset to fresh MULTI
        st = fsm.current()
        assert st.depth == StackLayer.MULTI_SELECTION
        assert st.primary == _b
        assert st.multi == frozenset({_b})

    def test_shift_on_primary_also_collapses(
        self, fsm: SelectionFSM,
    ) -> None:
        # Spec: ``Shift{same-set}`` covers both "any member of multi"
        # and "the primary" — both should collapse.
        fsm.step_shift_lmb(_a1)
        fsm.step_shift_lmb(_a2)
        fsm.step_shift_lmb(_a1)            # primary == _a1
        st = fsm.current()
        assert st.depth == StackLayer.HIGHLIGHT


# ---------------------------------------------------------------------------
# Esc transitions
# ---------------------------------------------------------------------------


class TestEsc:
    def test_esc_at_normal_is_noop(self, fsm: SelectionFSM) -> None:
        fsm.step_esc()
        assert fsm.current().depth == StackLayer.NORMAL

    def test_esc_pops_multi_to_selection(self, fsm: SelectionFSM) -> None:
        fsm.step_shift_lmb(_a1)
        fsm.step_shift_lmb(_a2)
        fsm.step_esc()
        st = fsm.current()
        assert st.depth == StackLayer.SELECTION
        assert st.primary == _a1
        assert st.multi == frozenset()

    def test_esc_pops_selection_to_highlight(
        self, fsm: SelectionFSM,
    ) -> None:
        fsm.step_lmb(_a1)
        fsm.step_esc()
        st = fsm.current()
        assert st.depth == StackLayer.HIGHLIGHT
        assert st.primary is None
        assert st.highlight_source == _a1

    def test_esc_pops_highlight_to_normal(self, fsm: SelectionFSM) -> None:
        fsm.step_lmb(_a1)
        fsm.step_esc()       # SELECTION → HIGHLIGHT
        fsm.step_esc()       # HIGHLIGHT → NORMAL
        st = fsm.current()
        assert st.depth == StackLayer.NORMAL
        assert st.highlight_source is None
        assert st.relation == RelationClass.NONE

    def test_esc_full_cascade_three_steps(self, fsm: SelectionFSM) -> None:
        # Build the deepest stack, then Esc three times — each step
        # should drop exactly one layer.
        fsm.step_shift_lmb(_a1)
        fsm.step_shift_lmb(_a2)
        assert fsm.current().depth == StackLayer.MULTI_SELECTION
        fsm.step_esc()
        assert fsm.current().depth == StackLayer.SELECTION
        fsm.step_esc()
        assert fsm.current().depth == StackLayer.HIGHLIGHT
        fsm.step_esc()
        assert fsm.current().depth == StackLayer.NORMAL


# ---------------------------------------------------------------------------
# Empty-canvas == Esc
# ---------------------------------------------------------------------------


class TestEmptyCanvas:
    def test_empty_canvas_lmb_equals_esc(self, fsm: SelectionFSM) -> None:
        fsm.step_lmb(_a1)
        fsm.step_lmb(None)
        assert fsm.current().depth == StackLayer.HIGHLIGHT

    def test_empty_canvas_shift_lmb_equals_esc(
        self, fsm: SelectionFSM,
    ) -> None:
        fsm.step_shift_lmb(_a1)
        fsm.step_shift_lmb(None)
        # SHIFT in NORMAL pushed MULTI_SELECTION; empty-canvas Shift+LMB
        # should pop exactly one layer (→ SELECTION).
        assert fsm.current().depth == StackLayer.SELECTION


# ---------------------------------------------------------------------------
# Observer wiring
# ---------------------------------------------------------------------------


class TestSubscribers:
    def test_subscribers_receive_old_and_new_states(
        self, fsm: SelectionFSM,
    ) -> None:
        seen: list[tuple[SelectionState, SelectionState]] = []
        fsm.subscribe(lambda old, new: seen.append((old, new)))
        fsm.step_lmb(_a1)
        fsm.step_esc()
        assert len(seen) == 2
        # First call: NORMAL → SELECTION
        assert seen[0][0].depth == StackLayer.NORMAL
        assert seen[0][1].depth == StackLayer.SELECTION
        # Second call: SELECTION → HIGHLIGHT
        assert seen[1][0].depth == StackLayer.SELECTION
        assert seen[1][1].depth == StackLayer.HIGHLIGHT

    def test_noop_esc_still_emits(self, fsm: SelectionFSM) -> None:
        # No-op transitions still fire ``changed`` so observers can use
        # them as a "resync now" signal.
        seen: list[tuple[SelectionState, SelectionState]] = []
        fsm.subscribe(lambda old, new: seen.append((old, new)))
        fsm.step_esc()  # at NORMAL — no-op
        assert len(seen) == 1
        assert seen[0][0].depth == StackLayer.NORMAL
        assert seen[0][1].depth == StackLayer.NORMAL


# ---------------------------------------------------------------------------
# Default-relation setter
# ---------------------------------------------------------------------------


class TestDefaultRelation:
    def test_set_default_relation_affects_new_layers(
        self, fsm: SelectionFSM,
    ) -> None:
        fsm.set_default_relation(RelationClass.NEIGHBORS)
        fsm.step_lmb(_a1)
        assert fsm.current().relation == RelationClass.NEIGHBORS

    def test_set_default_does_not_rewrite_active_layer(
        self, fsm: SelectionFSM,
    ) -> None:
        fsm.step_lmb(_a1)
        original_relation = fsm.current().relation
        fsm.set_default_relation(RelationClass.NEIGHBORS)
        # Active layer is untouched.
        assert fsm.current().relation == original_relation


# ---------------------------------------------------------------------------
# Hit equality / hashing — required for set membership in multi
# ---------------------------------------------------------------------------


class TestHit:
    def test_hits_with_same_kind_and_id_are_equal(self) -> None:
        a = Hit("wire", "x")
        b = Hit("wire", "x")
        assert a == b
        assert hash(a) == hash(b)

    def test_hits_with_different_kind_are_unequal(self) -> None:
        assert Hit("wire", "x") != Hit("component", "x")

    def test_unknown_kind_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown Hit.kind"):
            Hit("solder_blob", "x")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Trivial fallback predicate (used in tests + no-graph callers)
# ---------------------------------------------------------------------------


class TestTrivialPredicate:
    def test_default_predicate_treats_distinct_hits_as_unrelated(
        self,
    ) -> None:
        fsm_trivial = SelectionFSM()  # default predicate = identity
        fsm_trivial.step_lmb(_a1)
        fsm_trivial.step_shift_lmb(_a2)
        # With identity predicate, _a2 is NOT same-relation as _a1 →
        # Shift+LMB resets to fresh MULTI of {_a2}.
        st = fsm_trivial.current()
        assert st.depth == StackLayer.MULTI_SELECTION
        assert st.primary == _a2
        assert st.multi == frozenset({_a2})


# ---------------------------------------------------------------------------
# Phase 2c enums — sanity tests for BasisScope and SetOp
# ---------------------------------------------------------------------------


class TestBasisScopeEnum:
    """`BasisScope` is an IntEnum with two members.  Spec rev 2, Axis A.

    These tests pin the int values + ordering so downstream code that
    relies on `if scope == BasisScope.SINGLE: ...` short-circuits stays
    honest, and so accidentally renumbering doesn't silently flip
    semantics.
    """

    def test_member_count(self) -> None:
        assert len(BasisScope) == 2

    def test_int_values(self) -> None:
        assert int(BasisScope.SINGLE) == 0
        assert int(BasisScope.RELATION) == 1

    def test_members_distinct(self) -> None:
        assert BasisScope.SINGLE != BasisScope.RELATION


class TestSetOpEnum:
    """`SetOp` is an IntEnum with three members.  Spec rev 2, Axis B."""

    def test_member_count(self) -> None:
        assert len(SetOp) == 3

    def test_int_values(self) -> None:
        assert int(SetOp.SET) == 0
        assert int(SetOp.ADD) == 1
        assert int(SetOp.REMOVE) == 2

    def test_members_distinct(self) -> None:
        assert SetOp.SET != SetOp.ADD
        assert SetOp.SET != SetOp.REMOVE
        assert SetOp.ADD != SetOp.REMOVE
