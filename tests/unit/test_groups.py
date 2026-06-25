# SPDX-License-Identifier: MIT
"""Tests for ComponentGroup domain model and group commands."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.commands.group_cmds import (
    AddToGroupCommand,
    CreateGroupCommand,
    DisbandGroupCommand,
    RemoveFromGroupCommand,
    RenameGroupCommand,
)
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.schematic.net import PinRef
from protoboard.domain.layout.group import (
    ComponentGroup,
    all_component_ids,
    exposable_surface,
    parent_group_of,
    promote_selection_to_top_level,
    top_level_group_for,
    top_level_group_under,
    validate_exported_pins,
    would_create_cycle,
)
from protoboard.domain.layout.layout import Layout, ROOT_GROUP_ID

# ---------------------------------------------------------------------------
# Minimal state stub
# ---------------------------------------------------------------------------


def _board() -> Board:
    return Board(root=BoardGrid(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        ),
        rows=4, cols=4, row_gap=0.0, col_gap=0.0,
    ))


@dataclass
class _State:
    """Minimal :class:`ComponentState` stub.

    The group commands under test only ever touch ``layout``; the other
    three attributes exist solely so the dataclass structurally satisfies
    the :class:`~protoboard.domain.commands.base.ComponentState` protocol
    that ``apply``/``revert`` are typed against.
    """

    layout: Layout = field(default_factory=Layout)
    board: Board = field(default_factory=_board)
    instances: dict[uuid.UUID, ComponentInstance] = field(default_factory=dict)
    components: dict[str, ComponentDefinition] = field(default_factory=dict)


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


# ---------------------------------------------------------------------------
# internal_net_ids_for_components — drives the "Autoroute group" action,
# which restricts routing to nets that live entirely inside the group's
# recursive component set.
# ---------------------------------------------------------------------------


class TestInternalNetIds:
    def _build(self) -> tuple[
        "Schematic",  # type: ignore[name-defined]  # noqa: F821
        dict[str, uuid.UUID],
    ]:
        from protoboard.domain.schematic.net import Net, PinRef
        from protoboard.domain.schematic.schematic import Schematic

        c1, c2, c3, c4 = (uuid.uuid4() for _ in range(4))
        n_internal = Net(
            id=uuid.uuid4(),
            name="N_INT",
            pins={PinRef(c1, 1), PinRef(c2, 1)},
        )
        n_external = Net(
            id=uuid.uuid4(),
            name="N_EXT",
            pins={PinRef(c1, 2), PinRef(c3, 1)},  # c3 outside group
        )
        n_global = Net(
            id=uuid.uuid4(),
            name="VCC",
            pins={PinRef(c1, 3), PinRef(c2, 2)},
            global_net=True,
        )
        n_empty = Net(id=uuid.uuid4(), name="N_EMPTY", pins=set())
        sch = Schematic(nets={
            n_internal.id: n_internal,
            n_external.id: n_external,
            n_global.id: n_global,
            n_empty.id: n_empty,
        })
        names = {"c1": c1, "c2": c2, "c3": c3, "c4": c4,
                 "int": n_internal.id, "ext": n_external.id,
                 "vcc": n_global.id, "empty": n_empty.id}
        return sch, names

    def test_internal_net_collected(self) -> None:
        from protoboard.domain.layout.group import internal_net_ids_for_components

        sch, n = self._build()
        result = internal_net_ids_for_components(
            frozenset({n["c1"], n["c2"]}), sch
        )
        assert result == frozenset({n["int"]})

    def test_external_net_excluded(self) -> None:
        from protoboard.domain.layout.group import internal_net_ids_for_components

        sch, n = self._build()
        # Group {c1} alone: N_INT no longer fully internal (c2 outside),
        # N_EXT still external (c3 outside).
        result = internal_net_ids_for_components(frozenset({n["c1"]}), sch)
        assert result == frozenset()

    def test_global_net_excluded_even_when_fully_internal(self) -> None:
        from protoboard.domain.layout.group import internal_net_ids_for_components

        sch, n = self._build()
        # VCC pins are on {c1, c2}; both in the group — but VCC is global
        # and must NOT be reported as internal.
        result = internal_net_ids_for_components(
            frozenset({n["c1"], n["c2"]}), sch
        )
        assert n["vcc"] not in result

    def test_empty_net_excluded(self) -> None:
        from protoboard.domain.layout.group import internal_net_ids_for_components

        sch, n = self._build()
        result = internal_net_ids_for_components(
            frozenset({n["c1"], n["c2"]}), sch
        )
        assert n["empty"] not in result


class TestSchematicProjectionForGroup:
    """schematic_projection_for_group prepares the router input for
    "Autoroute group": every net is restricted to the pins inside the
    group, and nets with fewer than 2 internal pins are dropped."""

    def _build(self) -> tuple[
        "Schematic",  # type: ignore[name-defined]  # noqa: F821
        dict[str, uuid.UUID],
    ]:
        from protoboard.domain.schematic.net import Net, PinRef
        from protoboard.domain.schematic.schematic import Schematic

        c1, c2, c3 = (uuid.uuid4() for _ in range(3))
        n_internal = Net(
            id=uuid.uuid4(),
            name="N_INT",
            pins={PinRef(c1, 1), PinRef(c2, 1)},
        )
        # External-to-group net: pins on c1+c2 (in group) AND c3 (outside).
        # Projection should keep only the in-group pins.
        n_ext = Net(
            id=uuid.uuid4(),
            name="N_EXT",
            pins={PinRef(c1, 2), PinRef(c2, 2), PinRef(c3, 1)},
        )
        # Net with only one in-group pin — dropped from projection.
        n_lonely = Net(
            id=uuid.uuid4(),
            name="N_LONELY",
            pins={PinRef(c1, 3), PinRef(c3, 2)},
        )
        n_global = Net(
            id=uuid.uuid4(),
            name="VCC",
            pins={PinRef(c1, 4), PinRef(c2, 3), PinRef(c3, 3)},
            global_net=True,
        )
        sch = Schematic(nets={
            n_internal.id: n_internal,
            n_ext.id: n_ext,
            n_lonely.id: n_lonely,
            n_global.id: n_global,
        })
        return sch, {"c1": c1, "c2": c2, "c3": c3,
                     "int": n_internal.id, "ext": n_ext.id,
                     "lonely": n_lonely.id, "vcc": n_global.id}

    def test_internal_net_preserved(self) -> None:
        from protoboard.domain.layout.group import schematic_projection_for_group

        sch, n = self._build()
        proj = schematic_projection_for_group(
            frozenset({n["c1"], n["c2"]}), sch
        )
        assert n["int"] in proj.nets
        assert len(proj.nets[n["int"]].pins) == 2

    def test_external_net_keeps_only_internal_pins(self) -> None:
        """N_EXT has pins on c1, c2, c3.  Inside group {c1, c2} the
        projection keeps c1/c2 pins and drops the c3 pin."""
        from protoboard.domain.layout.group import schematic_projection_for_group

        sch, n = self._build()
        proj = schematic_projection_for_group(
            frozenset({n["c1"], n["c2"]}), sch
        )
        assert n["ext"] in proj.nets
        projected_ext = proj.nets[n["ext"]]
        component_ids = {p.component_id for p in projected_ext.pins}
        assert component_ids == {n["c1"], n["c2"]}

    def test_lonely_net_dropped(self) -> None:
        """A net with only one pin inside the group has nothing to wire
        up — dropped from the projection."""
        from protoboard.domain.layout.group import schematic_projection_for_group

        sch, n = self._build()
        proj = schematic_projection_for_group(
            frozenset({n["c1"], n["c2"]}), sch
        )
        assert n["lonely"] not in proj.nets

    def test_global_net_kept_when_at_least_two_internal_pins(self) -> None:
        """Global nets are still routed inside the group when 2+ of
        their pins fall inside.  Per user (2026-06-04) Autoroute Group
        routes *any* net with internal connectivity, not just fully
        internal ones."""
        from protoboard.domain.layout.group import schematic_projection_for_group

        sch, n = self._build()
        proj = schematic_projection_for_group(
            frozenset({n["c1"], n["c2"]}), sch
        )
        assert n["vcc"] in proj.nets
        assert proj.nets[n["vcc"]].global_net is True
        # External c3 pin dropped.
        assert {p.component_id for p in proj.nets[n["vcc"]].pins} == {
            n["c1"], n["c2"]
        }

    def test_projection_preserves_net_identity(self) -> None:
        """The projected nets keep the original id/name/color so wires
        produced by the router can be merged back into the schematic."""
        from protoboard.domain.layout.group import schematic_projection_for_group

        sch, n = self._build()
        proj = schematic_projection_for_group(
            frozenset({n["c1"], n["c2"]}), sch
        )
        assert proj.nets[n["int"]].name == sch.nets[n["int"]].name
        assert proj.nets[n["ext"]].name == sch.nets[n["ext"]].name


# ---------------------------------------------------------------------------
# exposable_surface / validate_exported_pins
# (encapsulated re-export chain — docs/archive/group-export-encapsulation.md)
# ---------------------------------------------------------------------------


def _make_defn(pin_count: int, name: str = "DUT") -> ComponentDefinition:
    from protoboard.domain.components.footprints import SIL

    return ComponentDefinition(
        name=name,
        footprint=SIL(n=pin_count),
        pin_names={i: f"P{i}" for i in range(1, pin_count + 1)},
    )


def _make_inst(defn_ref: str, refdes: str) -> ComponentInstance:
    return ComponentInstance.new(definition_ref=defn_ref, refdes=refdes, value="")


class TestExposableSurface:
    def test_leaf_group_direct_pins_only(self) -> None:
        defn = _make_defn(pin_count=3)
        inst = _make_inst("DUT", "U1")
        g = _make_group(comp_ids=frozenset({inst.id}))
        groups = {g.id: g}
        surface = exposable_surface(
            groups, g.id, {inst.id: inst}, {"DUT": defn}
        )
        assert surface == frozenset(
            PinRef(component_id=inst.id, pin_number=p) for p in (1, 2, 3)
        )

    def test_includes_child_reexports(self) -> None:
        # A directly contains nothing; B (child of A) contains U1 and exports its pin 2.
        defn = _make_defn(pin_count=3)
        u1 = _make_inst("DUT", "U1")
        ep = PinRef(component_id=u1.id, pin_number=2)
        b = _make_group(
            name="B", comp_ids=frozenset({u1.id})
        )
        b = ComponentGroup(
            id=b.id, name=b.name, component_ids=b.component_ids,
            exported_pins=frozenset({ep}),
        )
        a = _make_group(name="A", child_ids=frozenset({b.id}))
        groups = {a.id: a, b.id: b}
        surface = exposable_surface(
            groups, a.id, {u1.id: u1}, {"DUT": defn}
        )
        assert ep in surface
        # A has no direct members; only B's exports show up.
        assert surface == frozenset({ep})

    def test_excludes_grandchild_unless_chain(self) -> None:
        # A[B[C]], C exports c1 pin 1, B does NOT re-export.
        defn = _make_defn(pin_count=2)
        c1 = _make_inst("DUT", "C1")
        ep = PinRef(component_id=c1.id, pin_number=1)
        c = _make_group(name="C", comp_ids=frozenset({c1.id}))
        c = ComponentGroup(
            id=c.id, name=c.name, component_ids=c.component_ids,
            exported_pins=frozenset({ep}),
        )
        b = _make_group(name="B", child_ids=frozenset({c.id}))
        # B has no direct components and no exports — empty surface to A.
        a = _make_group(name="A", child_ids=frozenset({b.id}))
        groups = {a.id: a, b.id: b, c.id: c}
        surface = exposable_surface(
            groups, a.id, {c1.id: c1}, {"DUT": defn}
        )
        assert surface == frozenset()  # B blocks the chain.

    def test_chain_propagates_when_child_reexports(self) -> None:
        # A[B[C]], C exports c1 pin 1, B re-exports it → surface(A) contains it.
        defn = _make_defn(pin_count=2)
        c1 = _make_inst("DUT", "C1")
        ep = PinRef(component_id=c1.id, pin_number=1)
        c = _make_group(name="C", comp_ids=frozenset({c1.id}))
        c = ComponentGroup(
            id=c.id, name=c.name, component_ids=c.component_ids,
            exported_pins=frozenset({ep}),
        )
        b = _make_group(name="B", child_ids=frozenset({c.id}))
        b = ComponentGroup(
            id=b.id, name=b.name, child_group_ids=b.child_group_ids,
            exported_pins=frozenset({ep}),
        )
        a = _make_group(name="A", child_ids=frozenset({b.id}))
        groups = {a.id: a, b.id: b, c.id: c}
        surface = exposable_surface(
            groups, a.id, {c1.id: c1}, {"DUT": defn}
        )
        assert ep in surface

    def test_unknown_group_returns_empty(self) -> None:
        assert exposable_surface({}, uuid.uuid4(), {}, {}) == frozenset()

    def test_missing_instance_skipped(self) -> None:
        # Direct member without a corresponding instance entry → no pins added.
        g = _make_group(comp_ids=frozenset({uuid.uuid4()}))
        groups = {g.id: g}
        assert exposable_surface(groups, g.id, {}, {}) == frozenset()


class TestValidatePinRefs:
    def test_no_exports_is_valid(self) -> None:
        g = _make_group()
        assert validate_exported_pins({g.id: g}, g.id) == frozenset()

    def test_direct_member_pin_is_valid(self) -> None:
        cid = uuid.uuid4()
        ep = PinRef(component_id=cid, pin_number=1)
        g = ComponentGroup(
            id=uuid.uuid4(),
            name="G",
            component_ids=frozenset({cid}),
            exported_pins=frozenset({ep}),
        )
        assert validate_exported_pins({g.id: g}, g.id) == frozenset()

    def test_grandchild_flat_export_is_invalid(self) -> None:
        # A[B[C]]; A.exported_pins lists a pin of c1 (which lives in C).
        # B doesn't re-export it → A's entry is invalid.
        c_comp = uuid.uuid4()
        ep = PinRef(component_id=c_comp, pin_number=1)
        c = ComponentGroup(
            id=uuid.uuid4(), name="C", component_ids=frozenset({c_comp}),
            exported_pins=frozenset({ep}),
        )
        b = ComponentGroup(
            id=uuid.uuid4(), name="B", child_group_ids=frozenset({c.id}),
        )
        a = ComponentGroup(
            id=uuid.uuid4(), name="A", child_group_ids=frozenset({b.id}),
            exported_pins=frozenset({ep}),
        )
        groups = {a.id: a, b.id: b, c.id: c}
        assert validate_exported_pins(groups, a.id) == frozenset({ep})

    def test_chain_makes_export_valid(self) -> None:
        # Same setup but B also re-exports the pin → A's entry is valid.
        c_comp = uuid.uuid4()
        ep = PinRef(component_id=c_comp, pin_number=1)
        c = ComponentGroup(
            id=uuid.uuid4(), name="C", component_ids=frozenset({c_comp}),
            exported_pins=frozenset({ep}),
        )
        b = ComponentGroup(
            id=uuid.uuid4(), name="B", child_group_ids=frozenset({c.id}),
            exported_pins=frozenset({ep}),
        )
        a = ComponentGroup(
            id=uuid.uuid4(), name="A", child_group_ids=frozenset({b.id}),
            exported_pins=frozenset({ep}),
        )
        groups = {a.id: a, b.id: b, c.id: c}
        assert validate_exported_pins(groups, a.id) == frozenset()
        assert validate_exported_pins(groups, b.id) == frozenset()
        assert validate_exported_pins(groups, c.id) == frozenset()

    def test_mixed_exports_returns_only_invalid(self) -> None:
        # A has a direct member c_self (valid) + a flat grandchild export (invalid).
        c_self = uuid.uuid4()
        c_grand = uuid.uuid4()
        ep_self = PinRef(component_id=c_self, pin_number=1)
        ep_grand = PinRef(component_id=c_grand, pin_number=1)
        c = ComponentGroup(
            id=uuid.uuid4(), name="C", component_ids=frozenset({c_grand}),
            exported_pins=frozenset({ep_grand}),
        )
        b = ComponentGroup(
            id=uuid.uuid4(), name="B", child_group_ids=frozenset({c.id}),
            # B doesn't re-export ep_grand.
        )
        a = ComponentGroup(
            id=uuid.uuid4(), name="A",
            component_ids=frozenset({c_self}),
            child_group_ids=frozenset({b.id}),
            exported_pins=frozenset({ep_self, ep_grand}),
        )
        groups = {a.id: a, b.id: b, c.id: c}
        assert validate_exported_pins(groups, a.id) == frozenset({ep_grand})

    def test_unknown_group_returns_empty(self) -> None:
        assert validate_exported_pins({}, uuid.uuid4()) == frozenset()


# ---------------------------------------------------------------------------
# external_pins_for_group — encapsulated replacement for
# external_pins_for_components.  Walks G's component tree but only
# surfaces direct-member pins + child re-exports.
# ---------------------------------------------------------------------------


class TestExternalPinsForGroup:
    def _build_sch(
        self, names: dict[str, uuid.UUID]
    ) -> "Schematic":  # type: ignore[name-defined]  # noqa: F821
        """Build a schematic with one external net (c1 in G, c_out outside)
        and one internal net (c1↔c2 both in G)."""
        from protoboard.domain.schematic.net import Net, PinRef
        from protoboard.domain.schematic.schematic import Schematic

        n_ext = Net(
            id=uuid.uuid4(), name="N_EXT",
            pins={PinRef(names["c1"], 1), PinRef(names["c_out"], 1)},
        )
        n_int = Net(
            id=uuid.uuid4(), name="N_INT",
            pins={PinRef(names["c1"], 2), PinRef(names["c2"], 1)},
        )
        return Schematic(nets={n_ext.id: n_ext, n_int.id: n_int})

    def test_flat_group_direct_member_pin_exported(self) -> None:
        from protoboard.domain.layout.group import external_pins_for_group

        c1, c2, c_out = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        names = {"c1": c1, "c2": c2, "c_out": c_out}
        sch = self._build_sch(names)

        g = _make_group(comp_ids=frozenset({c1, c2}))
        groups = {g.id: g}
        result = external_pins_for_group(groups, g.id, sch)
        # c1 pin 1 reaches outside via N_EXT → must be re-exported.
        # c1 pin 2 is fully internal — NOT exported.
        assert result == frozenset({PinRef(component_id=c1, pin_number=1)})

    def test_grandchild_pin_not_promoted_when_chain_broken(self) -> None:
        # A[B[c1, c2]]; B does NOT export c1.  Auto-export on A must NOT
        # surface c1 — encapsulation says A cannot see what B doesn't expose.
        from protoboard.domain.layout.group import external_pins_for_group

        c1, c2, c_out = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        names = {"c1": c1, "c2": c2, "c_out": c_out}
        sch = self._build_sch(names)

        b = _make_group(name="B", comp_ids=frozenset({c1, c2}))
        a = _make_group(name="A", child_ids=frozenset({b.id}))
        groups = {a.id: a, b.id: b}
        # Even though c1 reaches outside via N_EXT, A cannot expose it
        # because B's interface doesn't include c1.
        assert external_pins_for_group(groups, a.id, sch) == frozenset()

    def test_grandchild_pin_surfaces_when_child_reexports(self) -> None:
        from protoboard.domain.layout.group import external_pins_for_group

        c1, c2, c_out = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        names = {"c1": c1, "c2": c2, "c_out": c_out}
        sch = self._build_sch(names)

        ep = PinRef(component_id=c1, pin_number=1)
        b = ComponentGroup(
            id=uuid.uuid4(), name="B",
            component_ids=frozenset({c1, c2}),
            exported_pins=frozenset({ep}),
        )
        a = ComponentGroup(
            id=uuid.uuid4(), name="A", child_group_ids=frozenset({b.id}),
        )
        groups = {a.id: a, b.id: b}
        assert external_pins_for_group(groups, a.id, sch) == frozenset({ep})

    def test_global_net_pin_exported_via_direct_member(self) -> None:
        from protoboard.domain.layout.group import external_pins_for_group
        from protoboard.domain.schematic.net import Net, PinRef
        from protoboard.domain.schematic.schematic import Schematic

        c1, c2 = uuid.uuid4(), uuid.uuid4()
        # Global net fully inside the group's component set — still external
        # by virtue of being global (reaches the whole board).
        n_vcc = Net(
            id=uuid.uuid4(), name="VCC",
            pins={PinRef(c1, 3), PinRef(c2, 3)},
            global_net=True,
        )
        sch = Schematic(nets={n_vcc.id: n_vcc})
        g = _make_group(comp_ids=frozenset({c1, c2}))
        groups = {g.id: g}
        result = external_pins_for_group(groups, g.id, sch)
        assert result == frozenset({
            PinRef(component_id=c1, pin_number=3),
            PinRef(component_id=c2, pin_number=3),
        })

    def test_pure_internal_net_not_exported(self) -> None:
        # No outside pin, not global → must not produce any exports.
        from protoboard.domain.layout.group import external_pins_for_group
        from protoboard.domain.schematic.net import Net, PinRef
        from protoboard.domain.schematic.schematic import Schematic

        c1, c2 = uuid.uuid4(), uuid.uuid4()
        n_int = Net(
            id=uuid.uuid4(), name="N", pins={PinRef(c1, 1), PinRef(c2, 1)},
        )
        sch = Schematic(nets={n_int.id: n_int})
        g = _make_group(comp_ids=frozenset({c1, c2}))
        groups = {g.id: g}
        assert external_pins_for_group(groups, g.id, sch) == frozenset()

    def test_unknown_group_returns_empty(self) -> None:
        from protoboard.domain.layout.group import external_pins_for_group
        from protoboard.domain.schematic.schematic import Schematic

        assert external_pins_for_group(
            {}, uuid.uuid4(), Schematic(nets={})
        ) == frozenset()

    def test_child_reexport_not_on_outside_net_not_promoted(self) -> None:
        # B re-exports c2 pin 1, but that pin lies on a fully internal net.
        # A's auto-export must NOT include c2 — re-exports only surface
        # when their underlying net actually reaches outside G.
        from protoboard.domain.layout.group import external_pins_for_group

        c1, c2, c_out = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        names = {"c1": c1, "c2": c2, "c_out": c_out}
        sch = self._build_sch(names)

        ep_internal = PinRef(component_id=c2, pin_number=1)
        b = ComponentGroup(
            id=uuid.uuid4(), name="B",
            component_ids=frozenset({c1, c2}),
            exported_pins=frozenset({ep_internal}),
        )
        a = ComponentGroup(
            id=uuid.uuid4(), name="A", child_group_ids=frozenset({b.id}),
        )
        groups = {a.id: a, b.id: b}
        # N_INT (c1.pin2 ↔ c2.pin1) is fully inside A's recursive set →
        # neither leg is external to A → c2 not promoted.
        # N_EXT goes through c1.pin1, but B doesn't re-export c1 → also not
        # promoted.
        assert external_pins_for_group(groups, a.id, sch) == frozenset()
