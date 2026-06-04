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
    parent_group_of,
    promote_selection_to_top_level,
    top_level_group_for,
    top_level_group_under,
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


# ---------------------------------------------------------------------------
# Selection promotion to top-level (2026-06-04 rule):
# any selected component nested inside a child group under the active
# context promotes the WHOLE top-level group into the new structure.
# See memory/project_selection_promotion_to_top_level_2026_06_04.md.
# ---------------------------------------------------------------------------


class TestSelectionPromotion:
    def _build_tree(
        self,
    ) -> tuple[
        dict[uuid.UUID, ComponentGroup],
        dict[str, uuid.UUID],
    ]:
        """Build A[B[c1, c2], C[c3]] under root, plus freelance f1 at root.

        Returns (groups_dict, name → id map) for readable assertions.
        """
        c1, c2, c3, f1 = (uuid.uuid4() for _ in range(4))
        b_id, c_id, a_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        b = ComponentGroup(
            id=b_id, name="B", component_ids=frozenset({c1, c2})
        )
        c = ComponentGroup(id=c_id, name="C", component_ids=frozenset({c3}))
        a = ComponentGroup(
            id=a_id, name="A", child_group_ids=frozenset({b_id, c_id})
        )
        root = ComponentGroup(
            id=ROOT_GROUP_ID, name="", child_group_ids=frozenset({a_id})
        )
        groups = {ROOT_GROUP_ID: root, a_id: a, b_id: b, c_id: c}
        names = {"A": a_id, "B": b_id, "C": c_id, "c1": c1, "c2": c2,
                 "c3": c3, "f1": f1}
        return groups, names

    def test_top_level_under_active_direct_child(self) -> None:
        """active=A, c1 ∈ B (direct child of A) → returns B."""
        groups, n = self._build_tree()
        assert top_level_group_under(groups, n["A"], n["c1"]) == n["B"]

    def test_top_level_under_active_direct_member_returns_none(self) -> None:
        """active=A, c1 is a member of B but B is direct child of A; if we
        ask about a hypothetical direct-of-A component, return None."""
        groups, n = self._build_tree()
        # Add a freelance component directly to A and query about it.
        free_in_a = uuid.uuid4()
        groups[n["A"]] = ComponentGroup(
            id=n["A"], name="A",
            component_ids=frozenset({free_in_a}),
            child_group_ids=groups[n["A"]].child_group_ids,
        )
        assert top_level_group_under(groups, n["A"], free_in_a) is None

    def test_top_level_under_active_outside_subtree_returns_none(self) -> None:
        """A and B'-as-sibling tree: active=A, c outside A's subtree → None."""
        c_outside = uuid.uuid4()
        a_id = uuid.uuid4()
        sibling_id = uuid.uuid4()
        sibling = ComponentGroup(
            id=sibling_id, name="S", component_ids=frozenset({c_outside})
        )
        a = ComponentGroup(id=a_id, name="A")
        groups = {a_id: a, sibling_id: sibling}
        assert top_level_group_under(groups, a_id, c_outside) is None

    def test_promote_single_component_from_subgroup(self) -> None:
        """Repro of user's 2026-06-04 scenario, simplified:
        active=root, selection includes 1 component from A=Group14 →
        promoted = (free={}, groups={A})."""
        groups, n = self._build_tree()
        free, promoted = promote_selection_to_top_level(
            groups, ROOT_GROUP_ID, frozenset({n["c1"]})
        )
        # c1 lives in B which lives in A which is direct child of root.
        # Promotion under root walks one level: c1 → A (not B).
        assert promoted == frozenset({n["A"]})
        assert free == frozenset()

    def test_promote_mix_with_freelance(self) -> None:
        """active=root, selection={c1 from A, f1 freelance} →
        promoted = (free={f1}, groups={A})."""
        groups, n = self._build_tree()
        free, promoted = promote_selection_to_top_level(
            groups, ROOT_GROUP_ID, frozenset({n["c1"], n["f1"]})
        )
        assert promoted == frozenset({n["A"]})
        assert free == frozenset({n["f1"]})

    def test_promote_inside_active_picks_direct_child(self) -> None:
        """active=A, selection={c1 from B} → promoted = (free={}, groups={B}).
        Demonstrates that the "top level" is relative to the active context."""
        groups, n = self._build_tree()
        free, promoted = promote_selection_to_top_level(
            groups, n["A"], frozenset({n["c1"]})
        )
        assert promoted == frozenset({n["B"]})
        assert free == frozenset()

    def test_promote_two_members_of_same_group_yields_one_promotion(
        self,
    ) -> None:
        """Selecting c1 AND c2 (both members of B) under A still promotes
        just B once, never doubles."""
        groups, n = self._build_tree()
        free, promoted = promote_selection_to_top_level(
            groups, n["A"], frozenset({n["c1"], n["c2"]})
        )
        assert promoted == frozenset({n["B"]})
        assert free == frozenset()

    def test_promote_multiple_groups(self) -> None:
        """active=A, selection={c1 from B, c3 from C} → both B and C
        promoted, no freelance."""
        groups, n = self._build_tree()
        free, promoted = promote_selection_to_top_level(
            groups, n["A"], frozenset({n["c1"], n["c3"]})
        )
        assert promoted == frozenset({n["B"], n["C"]})
        assert free == frozenset()

    def test_parent_group_of(self) -> None:
        groups, n = self._build_tree()
        assert parent_group_of(groups, n["A"]) == ROOT_GROUP_ID
        assert parent_group_of(groups, n["B"]) == n["A"]
        assert parent_group_of(groups, n["C"]) == n["A"]
        # ROOT itself has no parent.
        assert parent_group_of(groups, ROOT_GROUP_ID) is None
