# SPDX-License-Identifier: MIT
"""Tests for protoboard.controller.editing.drag_set.

`DragSet` is the GUI-level snapshot of what the user is dragging:
free top-level components + monolithic groups + wires.  These tests
verify the data structure semantics + the `from_selection` factory
that turns SelectTool's three storage fields into a frozen DragSet.

Pure-Python — no Qt, no QApplication.
"""

from __future__ import annotations

import uuid
from typing import Callable, TypedDict

import pytest

from protoboard.domain.layout.group import ComponentGroup
from protoboard.controller.editing.drag_set import DragSet


class World(TypedDict):
    """Typed view of the ``world`` fixture's heterogeneous contents."""

    g1_id: uuid.UUID
    g2_id: uuid.UUID
    g3_id: uuid.UUID
    c1: uuid.UUID
    c2: uuid.UUID
    c3: uuid.UUID
    c4: uuid.UUID
    c5: uuid.UUID
    c6: uuid.UUID
    c7: uuid.UUID
    groups_map: dict[uuid.UUID, ComponentGroup]
    in_group_of: Callable[[uuid.UUID], set[uuid.UUID]]


# ---------------------------------------------------------------------------
# Tiny fixtures: a couple of groups with known membership
# ---------------------------------------------------------------------------


@pytest.fixture
def world() -> World:
    """A two-group world:

    G1 = {c1, c2}
    G2 = {c3} + child group G3
    G3 = {c4, c5}
    free c6 (no group)
    free c7 (no group)
    """
    g1_id = uuid.uuid4()
    g2_id = uuid.uuid4()
    g3_id = uuid.uuid4()
    c1, c2, c3, c4, c5, c6, c7 = (uuid.uuid4() for _ in range(7))

    g1 = ComponentGroup(id=g1_id, name="G1", component_ids=frozenset({c1, c2}))
    g3 = ComponentGroup(id=g3_id, name="G3", component_ids=frozenset({c4, c5}))
    g2 = ComponentGroup(
        id=g2_id, name="G2",
        component_ids=frozenset({c3}),
        child_group_ids=frozenset({g3_id}),
    )
    groups_map = {g1_id: g1, g2_id: g2, g3_id: g3}

    def in_group_of(gid: uuid.UUID) -> set[uuid.UUID]:
        from protoboard.domain.layout.group import all_component_ids
        return set(all_component_ids(groups_map, gid))

    return {
        "g1_id": g1_id, "g2_id": g2_id, "g3_id": g3_id,
        "c1": c1, "c2": c2, "c3": c3, "c4": c4, "c5": c5,
        "c6": c6, "c7": c7,
        "groups_map": groups_map,
        "in_group_of": in_group_of,
    }


# ---------------------------------------------------------------------------
# Constructor + invariants
# ---------------------------------------------------------------------------


class TestDragSetConstructor:
    def test_default_is_empty(self) -> None:
        ds = DragSet()
        assert ds.is_empty
        assert ds.free_components == frozenset()
        assert ds.groups == frozenset()
        assert ds.wires == frozenset()

    def test_is_frozen(self) -> None:
        """DragSet is frozen so press-time snapshots can't drift."""
        ds = DragSet()
        with pytest.raises(Exception):
            ds.free_components = frozenset({uuid.uuid4()})  # type: ignore[misc]

    def test_non_empty_with_any_field(self) -> None:
        cid = uuid.uuid4()
        assert not DragSet(free_components=frozenset({cid})).is_empty
        assert not DragSet(groups=frozenset({cid})).is_empty
        assert not DragSet(wires=frozenset({cid})).is_empty


# ---------------------------------------------------------------------------
# all_component_ids
# ---------------------------------------------------------------------------


class TestAllComponentIds:
    def test_only_free_components(self, world: World) -> None:
        ds = DragSet(free_components=frozenset({world["c6"], world["c7"]}))
        assert ds.all_component_ids(world["groups_map"]) == frozenset(
            {world["c6"], world["c7"]}
        )

    def test_only_one_group_expands_to_members(self, world: World) -> None:
        ds = DragSet(groups=frozenset({world["g1_id"]}))
        assert ds.all_component_ids(world["groups_map"]) == frozenset(
            {world["c1"], world["c2"]}
        )

    def test_nested_group_includes_child_members(self, world: World) -> None:
        # G2 = {c3} + child G3{c4, c5} → expanded set should contain all three.
        ds = DragSet(groups=frozenset({world["g2_id"]}))
        assert ds.all_component_ids(world["groups_map"]) == frozenset(
            {world["c3"], world["c4"], world["c5"]}
        )

    def test_union_of_free_and_groups(self, world: World) -> None:
        ds = DragSet(
            free_components=frozenset({world["c6"]}),
            groups=frozenset({world["g1_id"]}),
        )
        assert ds.all_component_ids(world["groups_map"]) == frozenset(
            {world["c1"], world["c2"], world["c6"]}
        )

    def test_unknown_group_is_silently_skipped(self, world: World) -> None:
        # A stale group id (e.g. after disband mid-session) must not raise.
        unknown = uuid.uuid4()
        ds = DragSet(groups=frozenset({unknown}))
        assert ds.all_component_ids(world["groups_map"]) == frozenset()


# ---------------------------------------------------------------------------
# with_added — convenience for incremental construction
# ---------------------------------------------------------------------------


class TestWithAdded:
    def test_adds_free_components(self) -> None:
        ds = DragSet()
        c1, c2 = uuid.uuid4(), uuid.uuid4()
        ds2 = ds.with_added(free_components=frozenset({c1, c2}))
        # Original is untouched.
        assert ds.free_components == frozenset()
        # New has the additions.
        assert ds2.free_components == frozenset({c1, c2})

    def test_unioning_with_existing_members(self) -> None:
        c1, c2, c3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        ds = DragSet(free_components=frozenset({c1, c2}))
        ds2 = ds.with_added(free_components=frozenset({c2, c3}))
        assert ds2.free_components == frozenset({c1, c2, c3})

    def test_none_means_no_change(self) -> None:
        c1 = uuid.uuid4()
        ds = DragSet(free_components=frozenset({c1}))
        ds2 = ds.with_added()
        assert ds2 == ds


# ---------------------------------------------------------------------------
# from_selection — the canonical SelectTool → DragSet factory
# ---------------------------------------------------------------------------


class TestFromSelection:
    def test_empty_selection_yields_empty_drag_set(
        self, world: World
    ) -> None:
        ds = DragSet.from_selection(
            selected_ids=set(),
            selected_wire_ids=set(),
            selected_group_ids=set(),
            groups_map=world["groups_map"],  # type: ignore[arg-type]
            is_locked_in_group=lambda _cid: False,
        )
        assert ds.is_empty

    def test_top_level_components_become_free(
        self, world: World
    ) -> None:
        ds = DragSet.from_selection(
            selected_ids={world["c6"], world["c7"]},  # type: ignore[arg-type]
            selected_wire_ids=set(),
            selected_group_ids=set(),
            groups_map=world["groups_map"],  # type: ignore[arg-type]
            is_locked_in_group=lambda _cid: False,
        )
        assert ds.free_components == frozenset({world["c6"], world["c7"]})
        assert ds.groups == frozenset()

    def test_group_member_with_group_selected_goes_to_group(
        self, world: World
    ) -> None:
        """When the user selects both a group AND one of its members,
        the member should not appear in ``free_components`` — it moves
        via the group monolith.
        """
        members_of_g1 = world["in_group_of"](world["g1_id"])  # type: ignore[operator]
        # User somehow had c1 in `_selected_ids` (e.g. via a prior
        # Ctrl+klik) plus then added G1 as a whole.
        ds = DragSet.from_selection(
            selected_ids={world["c1"]},  # type: ignore[arg-type]
            selected_wire_ids=set(),
            selected_group_ids={world["g1_id"]},  # type: ignore[arg-type]
            groups_map=world["groups_map"],  # type: ignore[arg-type]
            is_locked_in_group=lambda cid: cid in members_of_g1,
        )
        assert ds.free_components == frozenset()
        assert ds.groups == frozenset({world["g1_id"]})
        # all_component_ids still returns c1 via the group.
        assert world["c1"] in ds.all_component_ids(world["groups_map"])  # type: ignore[arg-type]

    def test_locked_member_without_group_in_selection_is_dropped(
        self, world: World
    ) -> None:
        """A component belonging to a non-active group, when its group
        is NOT in ``selected_group_ids``, must be dropped from the
        drag set — it cannot move alone (current `_is_locked_in_group`
        rule).
        """
        members_of_g1 = world["in_group_of"](world["g1_id"])  # type: ignore[operator]
        ds = DragSet.from_selection(
            selected_ids={world["c1"], world["c6"]},  # type: ignore[arg-type]
            selected_wire_ids=set(),
            selected_group_ids=set(),  # G1 NOT selected
            groups_map=world["groups_map"],  # type: ignore[arg-type]
            is_locked_in_group=lambda cid: cid in members_of_g1,
        )
        # c1 is locked + group not selected → dropped.
        assert world["c1"] not in ds.free_components
        # c6 is free → kept.
        assert ds.free_components == frozenset({world["c6"]})

    def test_mixed_free_and_group(self, world: World) -> None:
        """Realistic mixed drag: one free component + one group selected."""
        members_of_g1 = world["in_group_of"](world["g1_id"])  # type: ignore[operator]
        ds = DragSet.from_selection(
            selected_ids={world["c6"]},  # type: ignore[arg-type]
            selected_wire_ids=set(),
            selected_group_ids={world["g1_id"]},  # type: ignore[arg-type]
            groups_map=world["groups_map"],  # type: ignore[arg-type]
            is_locked_in_group=lambda cid: cid in members_of_g1,
        )
        assert ds.free_components == frozenset({world["c6"]})
        assert ds.groups == frozenset({world["g1_id"]})
        # all_component_ids includes both the group members and c6.
        assert ds.all_component_ids(world["groups_map"]) == frozenset(  # type: ignore[arg-type]
            {world["c1"], world["c2"], world["c6"]}
        )

    def test_wires_pass_through_verbatim(self, world: World) -> None:
        w1, w2 = uuid.uuid4(), uuid.uuid4()
        ds = DragSet.from_selection(
            selected_ids=set(),
            selected_wire_ids={w1, w2},
            selected_group_ids=set(),
            groups_map=world["groups_map"],  # type: ignore[arg-type]
            is_locked_in_group=lambda _cid: False,
        )
        assert ds.wires == frozenset({w1, w2})
