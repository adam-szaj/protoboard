# SPDX-License-Identifier: MIT
"""Tests for the group.origin invariant.

User-facing rule (2026-06-05): ``group.origin`` is the world-coordinate
pivot of the group.

1. Recomputed on every group **edit** (create / add / remove / move
   member, exit edit mode) to the bbox top-left of the group's pins.
2. **Follows the group during drag** — every component move recomputes
   origin, so for a uniform drag delta the new origin equals the old
   origin translated by the same delta.
3. **Stays put during rotate** — the immutable pivot of a rotation
   stays at the same world point.
4. Survives save/load bit-perfect (covered by persistence tests).
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
        )
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
        rotation=root.rotation,
        origin=root.origin,
    )


# ---------------------------------------------------------------------------
# CreateGroupCommand
# ---------------------------------------------------------------------------


class TestCreateGroupOrigin:
    def test_origin_recomputed_from_placements(self) -> None:
        # SIL(2) at (3, 5) rotation=0 → pin1=(3, 5), pin2=(3, 6).
        # bbox top-left (min x+y) = (3, 5).
        state, cids = _build_state([(3.0, 5.0)])
        group = ComponentGroup(
            id=uuid.uuid4(),
            name="G",
            component_ids=frozenset(cids),
            origin=None,
        )
        CreateGroupCommand(group=group).apply(state)
        synced = state.layout.groups[group.id]
        assert synced.origin is not None
        assert synced.origin.x == pytest.approx(3.0)
        assert synced.origin.y == pytest.approx(5.0)

    def test_origin_overrides_supplied_value(self) -> None:
        # Callers sometimes pass an explicit origin; the canonical value
        # is bbox-top-left of *current* placements, so apply overrides.
        state, cids = _build_state([(3.0, 5.0)])
        group = ComponentGroup(
            id=uuid.uuid4(),
            name="G",
            component_ids=frozenset(cids),
            origin=Point(99.0, 99.0),
        )
        CreateGroupCommand(group=group).apply(state)
        synced = state.layout.groups[group.id]
        assert synced.origin is not None
        assert synced.origin.x == pytest.approx(3.0)
        assert synced.origin.y == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# MoveComponentCommand → origin follows the group
# ---------------------------------------------------------------------------


class TestMoveComponentOriginFollow:
    def test_uniform_drag_translates_origin_by_delta(self) -> None:
        # Two resistors forming a tight group at top-left (3, 5).
        state, cids = _build_state([(3.0, 5.0), (5.0, 5.0)])
        group = ComponentGroup(
            id=uuid.uuid4(), name="G",
            component_ids=frozenset(cids), origin=None,
        )
        _attach_to_root(state, group)
        # Sync origin via Create-style first call.
        from protoboard.domain.commands.group_cmds import (
            _recompute_group_origin,
        )
        _recompute_group_origin(state, group.id)
        orig = state.layout.groups[group.id].origin
        assert orig is not None
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

        # Origin should now be (13, 12) — the old origin + delta.
        new_origin = state.layout.groups[group.id].origin
        assert new_origin is not None
        assert new_origin.x == pytest.approx(13.0)
        assert new_origin.y == pytest.approx(12.0)

    def test_revert_restores_pre_move_origin(self) -> None:
        state, cids = _build_state([(3.0, 5.0)])
        group = ComponentGroup(
            id=uuid.uuid4(), name="G",
            component_ids=frozenset(cids), origin=None,
        )
        _attach_to_root(state, group)
        from protoboard.domain.commands.group_cmds import (
            _recompute_group_origin,
        )
        _recompute_group_origin(state, group.id)
        pre_origin = state.layout.groups[group.id].origin
        assert pre_origin is not None

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
        restored = state.layout.groups[group.id].origin
        assert restored is not None
        assert restored.x == pytest.approx(pre_origin.x)
        assert restored.y == pytest.approx(pre_origin.y)

    def test_move_outside_group_leaves_other_groups_alone(self) -> None:
        # Two separate groups; moving a member of group A leaves group B.origin
        # alone.  Sanity check on _groups_containing_component scope.
        state, cids = _build_state([(3.0, 5.0), (10.0, 10.0)])
        ga = ComponentGroup(
            id=uuid.uuid4(), name="A",
            component_ids=frozenset({cids[0]}), origin=None,
        )
        gb = ComponentGroup(
            id=uuid.uuid4(), name="B",
            component_ids=frozenset({cids[1]}), origin=None,
        )
        _attach_to_root(state, ga)
        _attach_to_root(state, gb)
        from protoboard.domain.commands.group_cmds import (
            _recompute_group_origin,
        )
        _recompute_group_origin(state, ga.id)
        _recompute_group_origin(state, gb.id)
        b_origin_before = state.layout.groups[gb.id].origin

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

        assert state.layout.groups[gb.id].origin == b_origin_before


# ---------------------------------------------------------------------------
# RotateGroupCommand → origin stays put
# ---------------------------------------------------------------------------


class TestRotateGroupOriginInvariant:
    def test_origin_unchanged_after_rotate(self) -> None:
        state, cids = _build_state([(3.0, 5.0), (5.0, 5.0)])
        group = ComponentGroup(
            id=uuid.uuid4(), name="G",
            component_ids=frozenset(cids), origin=Point(3.0, 5.0),
        )
        _attach_to_root(state, group)
        before = state.layout.groups[group.id].origin
        assert before is not None
        RotateGroupCommand(group_id=group.id, delta=90).apply(state)
        after = state.layout.groups[group.id].origin
        assert after is not None
        assert (after.x, after.y) == (
            pytest.approx(before.x), pytest.approx(before.y),
        )

    def test_origin_invariant_across_four_quarter_turns(self) -> None:
        # 4× 90° = identity rotation; origin must end where it started.
        state, cids = _build_state([(3.0, 5.0), (5.0, 5.0)])
        group = ComponentGroup(
            id=uuid.uuid4(), name="G",
            component_ids=frozenset(cids), origin=Point(3.0, 5.0),
        )
        _attach_to_root(state, group)
        for _ in range(4):
            RotateGroupCommand(group_id=group.id, delta=90).apply(state)
        ending = state.layout.groups[group.id].origin
        assert ending is not None
        assert ending.x == pytest.approx(3.0)
        assert ending.y == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# AddToGroupCommand / RemoveFromGroupCommand
# ---------------------------------------------------------------------------


class TestMembershipChangeRecomputesOrigin:
    def test_add_member_pulls_origin_to_new_bbox_top_left(self) -> None:
        # Existing group at (5, 5).  Add a new component sitting at (2, 3)
        # — the new bbox top-left is (2, 3).
        state, cids = _build_state([(5.0, 5.0), (2.0, 3.0)])
        group = ComponentGroup(
            id=uuid.uuid4(), name="G",
            component_ids=frozenset({cids[0]}), origin=Point(5.0, 5.0),
        )
        _attach_to_root(state, group)
        AddToGroupCommand(
            group_id=group.id, component_ids=frozenset({cids[1]}),
        ).apply(state)
        after = state.layout.groups[group.id].origin
        assert after is not None
        assert after.x == pytest.approx(2.0)
        assert after.y == pytest.approx(3.0)

    def test_remove_member_releases_origin_to_remaining_bbox(self) -> None:
        # Group with two components.  Remove the top-left one ⇒ origin
        # snaps to the remaining one's pin1.
        state, cids = _build_state([(2.0, 3.0), (5.0, 5.0)])
        group = ComponentGroup(
            id=uuid.uuid4(), name="G",
            component_ids=frozenset(cids), origin=Point(2.0, 3.0),
        )
        _attach_to_root(state, group)
        RemoveFromGroupCommand(
            group_id=group.id, component_ids=frozenset({cids[0]}),
        ).apply(state)
        after = state.layout.groups[group.id].origin
        assert after is not None
        assert after.x == pytest.approx(5.0)
        assert after.y == pytest.approx(5.0)

    def test_remove_revert_restores_origin(self) -> None:
        state, cids = _build_state([(2.0, 3.0), (5.0, 5.0)])
        group = ComponentGroup(
            id=uuid.uuid4(), name="G",
            component_ids=frozenset(cids), origin=Point(2.0, 3.0),
        )
        _attach_to_root(state, group)
        cmd = RemoveFromGroupCommand(
            group_id=group.id, component_ids=frozenset({cids[0]}),
        )
        cmd.apply(state)
        assert state.layout.groups[group.id].origin == Point(5.0, 5.0)
        cmd.revert(state)
        restored = state.layout.groups[group.id].origin
        assert restored is not None
        assert restored.x == pytest.approx(2.0)
        assert restored.y == pytest.approx(3.0)
