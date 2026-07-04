# SPDX-License-Identifier: MIT
"""Tests for the derived group pivot (the old ``group.origin`` invariant).

User-facing rule (2026-06-05, re-based 2026-07-04 on the scene-graph model):
the group's rotate pivot is the world-coordinate **bbox top-left pad** of its
members.  It used to be a stored ``group.origin`` field kept in sync by a
recompute pass; it is now DERIVED on demand from the group transform's fixed
point (``group_cmds._group_rotate_pivot``), so the behaviours below hold by
construction rather than by bookkeeping:

1. For a fresh group it is the bbox top-left (min x+y) pad of the members.
2. **Follows the group during drag** — a uniform drag delta translates it by
   the same delta.
3. **Stays put during rotate** — the immutable pivot of a rotation stays at
   the same world point (consecutive rotations orbit the same pad).
4. Membership changes re-derive it from the new member set.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.commands.component_cmds import MoveComponentCommand
from protoboard.domain.commands.group_cmds import (
    AddToGroupCommand,
    CreateGroupCommand,
    RemoveFromGroupCommand,
    RotateGroupCommand,
    _group_rotate_pivot,
)
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.group import ComponentGroup
from protoboard.domain.layout.layout import ROOT_GROUP_ID, Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import WireCollection
from protoboard.geometry import Point


def _board(rows: int = 100, cols: int = 100) -> Board:
    """Spare board big enough to cover any test placement."""
    return Board(root=BoardGrid(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        ),
        rows=rows, cols=cols, row_gap=0.0, col_gap=0.0,
    ))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class _State:
    layout: Layout
    instances: dict[uuid.UUID, ComponentInstance] = field(default_factory=dict)
    components: dict[str, ComponentDefinition] = field(default_factory=dict)
    board: Board = field(default_factory=_board)


def _resistor_defn() -> ComponentDefinition:
    return ComponentDefinition(
        name="R", footprint=SIL(2), pin_names={1: "A", 2: "B"},
    )


def _placement(cid: uuid.UUID, x: float, y: float, rot: int = 0) -> ComponentPlacement:
    return ComponentPlacement(
        component_id=cid, position=Point(x, y), rotation=rot, side="top",
    )


def _build_state(
    components: list[tuple[float, float]],
    rot: int = 0,
) -> tuple[_State, list[uuid.UUID]]:
    """Make a state with N resistors at the given (x, y) positions.

    Returns (state, [component_ids]).  All components share definition_ref="R".
    """
    defn = _resistor_defn()
    cids: list[uuid.UUID] = []
    placements: dict[uuid.UUID, ComponentPlacement] = {}
    instances: dict[uuid.UUID, ComponentInstance] = {}
    for i, (x, y) in enumerate(components):
        inst = ComponentInstance.new(
            definition_ref="R", refdes=f"R{i+1}", value="10k",
        ).bind(defn, {})
        cid = inst.id
        cids.append(cid)
        placements[cid] = _placement(cid, x, y, rot)
        instances[cid] = inst
    layout = Layout(placements=placements, wires=WireCollection())
    layout.ensure_root_group()
    state = _State(
        layout=layout, instances=instances, components={defn.name: defn},
    )
    return state, cids


def _attach_to_root(state: _State, group: ComponentGroup) -> None:
    state.layout.groups[group.id] = group
    root = state.layout.groups[ROOT_GROUP_ID]
    state.layout.groups[ROOT_GROUP_ID] = ComponentGroup(
        id=root.id,
        name=root.name,
        component_ids=root.component_ids,
        child_group_ids=root.child_group_ids | {group.id},
        exported_pins=root.exported_pins,
    )


def _pivot(state: _State, gid: uuid.UUID) -> Point:
    p = _group_rotate_pivot(state, gid)  # type: ignore[arg-type]
    assert p is not None
    return p


# ---------------------------------------------------------------------------
# Fresh group → pivot at the bbox top-left pad
# ---------------------------------------------------------------------------


class TestCreateGroupPivot:
    def test_pivot_derives_from_placements(self) -> None:
        # SIL(2) at (3, 5) rotation=0 → pin1=(3, 5), pin2=(3, 6).
        # bbox top-left (min x+y) = (3, 5).
        state, cids = _build_state([(3.0, 5.0)])
        group = ComponentGroup(
            id=uuid.uuid4(),
            name="G",
            component_ids=frozenset(cids),
        )
        CreateGroupCommand(group=group).apply(state)
        pivot = _pivot(state, group.id)
        assert pivot.x == pytest.approx(3.0)
        assert pivot.y == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# MoveComponentCommand → pivot follows the group
# ---------------------------------------------------------------------------


class TestMoveComponentPivotFollow:
    def test_uniform_drag_translates_pivot_by_delta(self) -> None:
        # Two resistors forming a tight group at top-left (3, 5).
        state, cids = _build_state([(3.0, 5.0), (5.0, 5.0)])
        group = ComponentGroup(
            id=uuid.uuid4(), name="G",
            component_ids=frozenset(cids),
        )
        _attach_to_root(state, group)
        orig = _pivot(state, group.id)
        assert (orig.x, orig.y) == (pytest.approx(3.0), pytest.approx(5.0))

        # Move both resistors by (+10, +7) — group drag delta.
        for cid in cids:
            old_pl = state.layout.placements[cid]
            new_pl = ComponentPlacement(
                component_id=cid,
                position=Point(old_pl.position.x + 10.0, old_pl.position.y + 7.0),
                rotation=old_pl.rotation, side=old_pl.side,
            )
            MoveComponentCommand(
                component_id=cid,
                new_placement=new_pl,
                old_placement=old_pl,
                skip_conflict_check=True,
                placement_only=True,
            ).apply(state)

        # Pivot is now (13, 12) — the old pivot + delta.
        new_pivot = _pivot(state, group.id)
        assert new_pivot.x == pytest.approx(13.0)
        assert new_pivot.y == pytest.approx(12.0)

    def test_revert_restores_pre_move_pivot(self) -> None:
        state, cids = _build_state([(3.0, 5.0)])
        group = ComponentGroup(
            id=uuid.uuid4(), name="G",
            component_ids=frozenset(cids),
        )
        _attach_to_root(state, group)
        pre = _pivot(state, group.id)

        cid = cids[0]
        old_pl = state.layout.placements[cid]
        new_pl = ComponentPlacement(
            component_id=cid,
            position=Point(old_pl.position.x + 4.0, old_pl.position.y + 4.0),
            rotation=old_pl.rotation, side=old_pl.side,
        )
        cmd = MoveComponentCommand(
            component_id=cid,
            new_placement=new_pl,
            old_placement=old_pl,
            skip_conflict_check=True,
            placement_only=True,
        )
        cmd.apply(state)
        cmd.revert(state)
        restored = _pivot(state, group.id)
        assert restored.x == pytest.approx(pre.x)
        assert restored.y == pytest.approx(pre.y)

    def test_move_outside_group_leaves_other_groups_alone(self) -> None:
        # Two separate groups; moving a member of group A leaves group B's
        # pivot alone.
        state, cids = _build_state([(3.0, 5.0), (10.0, 10.0)])
        ga = ComponentGroup(
            id=uuid.uuid4(), name="A",
            component_ids=frozenset({cids[0]}),
        )
        gb = ComponentGroup(
            id=uuid.uuid4(), name="B",
            component_ids=frozenset({cids[1]}),
        )
        _attach_to_root(state, ga)
        _attach_to_root(state, gb)
        b_pivot_before = _pivot(state, gb.id)

        # Move cids[0] (A's member) — B should be untouched.
        old_pl = state.layout.placements[cids[0]]
        new_pl = ComponentPlacement(
            component_id=cids[0],
            position=Point(99.0, 99.0),
            rotation=old_pl.rotation, side=old_pl.side,
        )
        MoveComponentCommand(
            component_id=cids[0], new_placement=new_pl, old_placement=old_pl,
            skip_conflict_check=True, placement_only=True,
        ).apply(state)

        b_after = _pivot(state, gb.id)
        assert (b_after.x, b_after.y) == (
            pytest.approx(b_pivot_before.x), pytest.approx(b_pivot_before.y),
        )


# ---------------------------------------------------------------------------
# RotateGroupCommand → pivot stays put (the transform's fixed point)
# ---------------------------------------------------------------------------


class TestRotateGroupPivotInvariant:
    def test_pivot_unchanged_after_rotate(self) -> None:
        state, cids = _build_state([(3.0, 5.0), (5.0, 5.0)])
        group = ComponentGroup(
            id=uuid.uuid4(), name="G",
            component_ids=frozenset(cids),
        )
        _attach_to_root(state, group)
        before = _pivot(state, group.id)
        RotateGroupCommand(group_id=group.id, delta=90).apply(state)
        after = _pivot(state, group.id)
        assert (after.x, after.y) == (
            pytest.approx(before.x), pytest.approx(before.y),
        )

    def test_pivot_invariant_across_four_quarter_turns(self) -> None:
        # 4× 90° = identity rotation; pivot AND members end where they
        # started.
        state, cids = _build_state([(3.0, 5.0), (5.0, 5.0)])
        group = ComponentGroup(
            id=uuid.uuid4(), name="G",
            component_ids=frozenset(cids),
        )
        _attach_to_root(state, group)
        world_before = {
            cid: state.layout.placements[cid].position for cid in cids
        }
        for _ in range(4):
            RotateGroupCommand(group_id=group.id, delta=90).apply(state)
        ending = _pivot(state, group.id)
        assert ending.x == pytest.approx(3.0)
        assert ending.y == pytest.approx(5.0)
        for cid in cids:
            pos = state.layout.placements[cid].position
            assert pos.x == pytest.approx(world_before[cid].x)
            assert pos.y == pytest.approx(world_before[cid].y)


# ---------------------------------------------------------------------------
# AddToGroupCommand / RemoveFromGroupCommand → pivot re-derives
# ---------------------------------------------------------------------------


class TestMembershipChangeRederivesPivot:
    def test_add_member_pulls_pivot_to_new_bbox_top_left(self) -> None:
        # Existing group at (5, 5).  Add a new component sitting at (2, 3)
        # — the new bbox top-left is (2, 3).
        state, cids = _build_state([(5.0, 5.0), (2.0, 3.0)])
        group = ComponentGroup(
            id=uuid.uuid4(), name="G",
            component_ids=frozenset({cids[0]}),
        )
        _attach_to_root(state, group)
        AddToGroupCommand(
            group_id=group.id, component_ids=frozenset({cids[1]}),
        ).apply(state)
        after = _pivot(state, group.id)
        assert after.x == pytest.approx(2.0)
        assert after.y == pytest.approx(3.0)

    def test_remove_member_releases_pivot_to_remaining_bbox(self) -> None:
        # Group with two components.  Remove the top-left one ⇒ pivot
        # snaps to the remaining one's pin1.
        state, cids = _build_state([(2.0, 3.0), (5.0, 5.0)])
        group = ComponentGroup(
            id=uuid.uuid4(), name="G",
            component_ids=frozenset(cids),
        )
        _attach_to_root(state, group)
        RemoveFromGroupCommand(
            group_id=group.id, component_ids=frozenset({cids[0]}),
        ).apply(state)
        after = _pivot(state, group.id)
        assert after.x == pytest.approx(5.0)
        assert after.y == pytest.approx(5.0)

    def test_remove_revert_restores_pivot(self) -> None:
        state, cids = _build_state([(2.0, 3.0), (5.0, 5.0)])
        group = ComponentGroup(
            id=uuid.uuid4(), name="G",
            component_ids=frozenset(cids),
        )
        _attach_to_root(state, group)
        cmd = RemoveFromGroupCommand(
            group_id=group.id, component_ids=frozenset({cids[0]}),
        )
        cmd.apply(state)
        after = _pivot(state, group.id)
        assert (after.x, after.y) == (pytest.approx(5.0), pytest.approx(5.0))
        cmd.revert(state)
        restored = _pivot(state, group.id)
        assert restored.x == pytest.approx(2.0)
        assert restored.y == pytest.approx(3.0)
