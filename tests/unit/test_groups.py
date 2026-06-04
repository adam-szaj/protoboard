# SPDX-License-Identifier: MIT
"""Tests for ComponentGroup domain model and group commands."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest

from protoboard.domain.commands.group_cmds import (
    AddToGroupCommand,
    CreateGroupCommand,
    DisbandGroupCommand,
    RemoveFromGroupCommand,
    RenameGroupCommand,
)
from protoboard.domain.layout.group import (
    ComponentGroup,
    all_component_ids,
    top_level_group_for,
    would_create_cycle,
)
from protoboard.domain.layout.layout import Layout, ROOT_GROUP_ID

# ---------------------------------------------------------------------------
# Minimal state stub
# ---------------------------------------------------------------------------


@dataclass
class _State:
    layout: Layout = field(default_factory=Layout)


def _make_group(
    name: str = "G",
    comp_ids: frozenset[uuid.UUID] | None = None,
    child_ids: frozenset[uuid.UUID] | None = None,
) -> ComponentGroup:
    return ComponentGroup(
        id=uuid.uuid4(),
        name=name,
        component_ids=comp_ids or frozenset(),
        child_group_ids=child_ids or frozenset(),
    )


# ---------------------------------------------------------------------------
# all_component_ids (recursive)
# ---------------------------------------------------------------------------


class TestAllComponentIds:
    def test_flat_group(self) -> None:
        c1, c2 = uuid.uuid4(), uuid.uuid4()
        g = _make_group(comp_ids=frozenset({c1, c2}))
        groups = {g.id: g}
        assert all_component_ids(groups, g.id) == frozenset({c1, c2})

    def test_nested_group(self) -> None:
        c1, c2, c3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        child = _make_group(comp_ids=frozenset({c2, c3}))
        parent = _make_group(
            comp_ids=frozenset({c1}), child_ids=frozenset({child.id})
        )
        groups = {child.id: child, parent.id: parent}
        assert all_component_ids(groups, parent.id) == frozenset({c1, c2, c3})

    def test_cycle_protection(self) -> None:
        g1 = _make_group()
        g2 = _make_group(child_ids=frozenset({g1.id}))
        # Manually create a cycle (normally forbidden).
        g1_cyclic = ComponentGroup(
            id=g1.id,
            name=g1.name,
            component_ids=g1.component_ids,
            child_group_ids=frozenset({g2.id}),
        )
        groups = {g1.id: g1_cyclic, g2.id: g2}
        # Should not hang or crash.
        result = all_component_ids(groups, g1.id)
        assert isinstance(result, frozenset)


# ---------------------------------------------------------------------------
# would_create_cycle
# ---------------------------------------------------------------------------


class TestWouldCreateCycle:
    def test_self_cycle(self) -> None:
        gid = uuid.uuid4()
        assert would_create_cycle({}, gid, gid) is True

    def test_no_cycle(self) -> None:
        g1 = _make_group()
        g2 = _make_group()
        groups = {g1.id: g1, g2.id: g2}
        assert would_create_cycle(groups, g1.id, g2.id) is False

    def test_indirect_cycle(self) -> None:
        g1 = _make_group()
        g2 = _make_group(child_ids=frozenset({g1.id}))
        g3 = _make_group(child_ids=frozenset({g2.id}))
        groups = {g1.id: g1, g2.id: g2, g3.id: g3}
        # Adding g3 as child of g1 would create g1→g2→g3→g1.
        assert would_create_cycle(groups, g1.id, g3.id) is True


# ---------------------------------------------------------------------------
# top_level_group_for
# ---------------------------------------------------------------------------


class TestTopLevelGroupFor:
    def test_not_in_any_group(self) -> None:
        cid = uuid.uuid4()
        assert top_level_group_for({}, cid) is None

    def test_direct_member(self) -> None:
        cid = uuid.uuid4()
        g = _make_group(comp_ids=frozenset({cid}))
        groups = {g.id: g}
        assert top_level_group_for(groups, cid) == g.id

    def test_in_child_group_returns_top(self) -> None:
        cid = uuid.uuid4()
        child = _make_group(comp_ids=frozenset({cid}))
        parent = _make_group(child_ids=frozenset({child.id}))
        groups = {child.id: child, parent.id: parent}
        assert top_level_group_for(groups, cid) == parent.id


# ---------------------------------------------------------------------------
# CreateGroupCommand
# ---------------------------------------------------------------------------


class TestCreateGroupCommand:
    def test_apply_adds_group(self) -> None:
        state = _State()
        g = _make_group("A")
        cmd = CreateGroupCommand(group=g)
        cmd.apply(state)
        assert state.layout.groups[g.id] == g

    def test_revert_removes_group(self) -> None:
        state = _State()
        g = _make_group("A")
        cmd = CreateGroupCommand(group=g)
        cmd.apply(state)
        cmd.revert(state)
        assert g.id not in state.layout.groups

    def test_description(self) -> None:
        g = _make_group("MyGroup")
        cmd = CreateGroupCommand(group=g)
        assert "MyGroup" in cmd.description


# ---------------------------------------------------------------------------
# DisbandGroupCommand
# ---------------------------------------------------------------------------


class TestDisbandGroupCommand:
    def test_apply_removes_group(self) -> None:
        state = _State()
        g = _make_group("G")
        state.layout.groups[g.id] = g
        cmd = DisbandGroupCommand(group_id=g.id)
        cmd.apply(state)
        assert g.id not in state.layout.groups

    def test_revert_restores_group(self) -> None:
        state = _State()
        g = _make_group("G")
        state.layout.groups[g.id] = g
        cmd = DisbandGroupCommand(group_id=g.id)
        cmd.apply(state)
        cmd.revert(state)
        assert state.layout.groups[g.id] == g

    def test_disband_removes_from_parent(self) -> None:
        state = _State()
        child = _make_group("child")
        parent = _make_group("parent", child_ids=frozenset({child.id}))
        state.layout.groups[child.id] = child
        state.layout.groups[parent.id] = parent
        cmd = DisbandGroupCommand(group_id=child.id)
        cmd.apply(state)
        assert child.id not in state.layout.groups[parent.id].child_group_ids

    def test_revert_restores_parent_reference(self) -> None:
        state = _State()
        child = _make_group("child")
        parent = _make_group("parent", child_ids=frozenset({child.id}))
        state.layout.groups[child.id] = child
        state.layout.groups[parent.id] = parent
        cmd = DisbandGroupCommand(group_id=child.id)
        cmd.apply(state)
        cmd.revert(state)
        assert child.id in state.layout.groups[parent.id].child_group_ids


# ---------------------------------------------------------------------------
# AddToGroupCommand
# ---------------------------------------------------------------------------


class TestAddToGroupCommand:
    def test_add_components(self) -> None:
        state = _State()
        g = _make_group("G")
        state.layout.groups[g.id] = g
        c1 = uuid.uuid4()
        cmd = AddToGroupCommand(
            group_id=g.id, component_ids=frozenset({c1})
        )
        cmd.apply(state)
        assert c1 in state.layout.groups[g.id].component_ids

    def test_revert_removes_added(self) -> None:
        state = _State()
        g = _make_group("G")
        state.layout.groups[g.id] = g
        c1 = uuid.uuid4()
        cmd = AddToGroupCommand(
            group_id=g.id, component_ids=frozenset({c1})
        )
        cmd.apply(state)
        cmd.revert(state)
        assert c1 not in state.layout.groups[g.id].component_ids

    def test_add_child_group_cycle_raises(self) -> None:
        state = _State()
        g = _make_group("G")
        state.layout.groups[g.id] = g
        with pytest.raises(ValueError, match="cycle"):
            AddToGroupCommand(
                group_id=g.id, child_group_ids=frozenset({g.id})
            ).apply(state)


# ---------------------------------------------------------------------------
# RenameGroupCommand
# ---------------------------------------------------------------------------


class TestRenameGroupCommand:
    def test_apply_renames(self) -> None:
        state = _State()
        g = _make_group("Old")
        state.layout.groups[g.id] = g
        cmd = RenameGroupCommand(group_id=g.id, new_name="New")
        cmd.apply(state)
        assert state.layout.groups[g.id].name == "New"

    def test_revert_restores_name(self) -> None:
        state = _State()
        g = _make_group("Old")
        state.layout.groups[g.id] = g
        cmd = RenameGroupCommand(group_id=g.id, new_name="New")
        cmd.apply(state)
        cmd.revert(state)
        assert state.layout.groups[g.id].name == "Old"


# ---------------------------------------------------------------------------
# Invariant: a component lives in exactly one group at any tree depth.
# Repro for the 2026-06-04 bug where Ctrl+G inside an edited parent left
# the components as direct members of BOTH the parent and the new child,
# which broke _is_locked_in_group hit-tests for nested drag/rotate.
# ---------------------------------------------------------------------------


class TestNestedGroupInvariant:
    def test_create_subgroup_inside_active_parent_detaches_components(self) -> None:
        """The Ctrl+G command sequence (Create + AddToGroup + RemoveFromGroup)
        leaves each component as a direct member of exactly one group."""
        state = _State()
        c1, c2 = uuid.uuid4(), uuid.uuid4()
        parent = ComponentGroup(
            id=uuid.uuid4(),
            name="A",
            component_ids=frozenset({c1, c2}),
        )
        state.layout.groups[parent.id] = parent

        # Replay the command sequence that _group_for_selection runs for
        # "Ctrl+G on selected components inside an edited parent group".
        child = ComponentGroup(
            id=uuid.uuid4(),
            name="B",
            component_ids=frozenset({c1, c2}),
        )
        CreateGroupCommand(group=child).apply(state)
        AddToGroupCommand(
            group_id=parent.id,
            child_group_ids=frozenset({child.id}),
        ).apply(state)
        RemoveFromGroupCommand(
            group_id=parent.id,
            component_ids=frozenset({c1, c2}),
        ).apply(state)

        # Invariant: the components are now direct members of B only,
        # and B is a child of A; A has no direct members.
        assert state.layout.groups[child.id].component_ids == frozenset({c1, c2})
        assert state.layout.groups[parent.id].component_ids == frozenset()
        assert state.layout.groups[parent.id].child_group_ids == frozenset({child.id})
        # And the recursive collector still reports both under A.
        assert all_component_ids(state.layout.groups, parent.id) == frozenset({c1, c2})

    def test_create_subgroup_at_root_no_op_detach(self) -> None:
        """Ctrl+G at root level: components were never direct members of
        root, so the RemoveFromGroupCommand is a harmless no-op."""
        state = _State()
        # Seed root group (the GUI always has one).
        root = ComponentGroup(id=ROOT_GROUP_ID, name="", component_ids=frozenset())
        state.layout.groups[ROOT_GROUP_ID] = root

        c1 = uuid.uuid4()
        child = ComponentGroup(
            id=uuid.uuid4(),
            name="G",
            component_ids=frozenset({c1}),
        )
        CreateGroupCommand(group=child).apply(state)
        AddToGroupCommand(
            group_id=ROOT_GROUP_ID,
            child_group_ids=frozenset({child.id}),
        ).apply(state)
        RemoveFromGroupCommand(
            group_id=ROOT_GROUP_ID,
            component_ids=frozenset({c1}),
        ).apply(state)

        assert state.layout.groups[child.id].component_ids == frozenset({c1})
        assert state.layout.groups[ROOT_GROUP_ID].component_ids == frozenset()
        assert child.id in state.layout.groups[ROOT_GROUP_ID].child_group_ids
