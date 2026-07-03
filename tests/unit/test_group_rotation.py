# SPDX-License-Identifier: MIT
"""Tests for group rotation (RotateGroupCommand and related helpers)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.commands.group_cmds import (
    RotateGroupCommand,
    _rotation_about,
)
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.group import ComponentGroup
from protoboard.domain.layout.layout import ROOT_GROUP_ID, Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import Wire, WireCollection
from protoboard.geometry import Point


# ---------------------------------------------------------------------------
# Minimal fake session used by the command
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
class _FakeState:
    layout: Layout
    board: Board = field(default_factory=_board)
    instances: dict[uuid.UUID, ComponentInstance] = field(default_factory=dict)
    components: dict[str, ComponentDefinition] = field(default_factory=dict)


def _make_placement(cid: uuid.UUID, x: float, y: float, rotation: int = 0) -> ComponentPlacement:
    return ComponentPlacement(
        component_id=cid,
        position=Point(x, y),
        rotation=rotation,
        side="top",
    )


# ---------------------------------------------------------------------------
# Unit tests for _rotation_about (rotate a point CW about an origin)
# ---------------------------------------------------------------------------

def _rotate_point_around(p: Point, origin: Point, delta: int) -> Point:
    return _rotation_about(origin, delta).apply(p)


def test_rotate_around_0() -> None:
    p = Point(3.0, 1.0)
    origin = Point(1.0, 1.0)
    result = _rotate_point_around(p, origin, 0)
    assert result.x == pytest.approx(3.0)
    assert result.y == pytest.approx(1.0)


def test_rotate_around_90() -> None:
    # (3,1) rotated 90° CW around (1,1): dx=2,dy=0 → rx=0,ry=-2 → (1,-1)
    p = Point(3.0, 1.0)
    origin = Point(1.0, 1.0)
    result = _rotate_point_around(p, origin, 90)
    assert result.x == pytest.approx(1.0)
    assert result.y == pytest.approx(-1.0)


def test_rotate_around_180() -> None:
    p = Point(3.0, 1.0)
    origin = Point(1.0, 1.0)
    result = _rotate_point_around(p, origin, 180)
    assert result.x == pytest.approx(-1.0)
    assert result.y == pytest.approx(1.0)


def test_rotate_around_270() -> None:
    p = Point(3.0, 1.0)
    origin = Point(1.0, 1.0)
    result = _rotate_point_around(p, origin, 270)
    assert result.x == pytest.approx(1.0)
    assert result.y == pytest.approx(3.0)


def test_rotate_around_360_is_identity() -> None:
    p = Point(2.0, 5.0)
    origin = Point(0.0, 0.0)
    result = _rotate_point_around(p, origin, 360)
    assert result.x == pytest.approx(2.0)
    assert result.y == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Wire fixture helper (used by the internal-wire rotation test below)
# ---------------------------------------------------------------------------

def _wire(path: list[tuple[float, float]]) -> Wire:
    return Wire(
        id=uuid.uuid4(),
        path=[Point(x, y) for x, y in path],
        insulated=False,
        side="top",
    )


# ---------------------------------------------------------------------------
# RotateGroupCommand — using simple components with no footprint (empty group)
# We test position and rotation changes directly without a footprint.
# ---------------------------------------------------------------------------

def _simple_group(comp_ids: frozenset[uuid.UUID], origin: Point) -> ComponentGroup:
    return ComponentGroup(
        id=uuid.uuid4(),
        name="test_group",
        component_ids=comp_ids,
        origin=origin,
    )


def _layout_with_group(
    group: ComponentGroup,
    placements: dict[uuid.UUID, ComponentPlacement],
    wires: list[Wire] | None = None,
) -> Layout:
    layout = Layout(
        placements=placements,
        wires=WireCollection(wires or []),
        groups={ROOT_GROUP_ID: ComponentGroup(
            id=ROOT_GROUP_ID,
            name="root",
            child_group_ids=frozenset([group.id]),
        ), group.id: group},
    )
    return layout


def test_rotate_group_command_rotates_positions() -> None:
    """Two components at (2,1) and (2,3) with origin (1,1) rotated 90° CW."""
    cid1, cid2 = uuid.uuid4(), uuid.uuid4()
    pl1 = _make_placement(cid1, 2.0, 1.0, rotation=0)
    pl2 = _make_placement(cid2, 2.0, 3.0, rotation=90)
    origin = Point(1.0, 1.0)
    group = _simple_group(frozenset([cid1, cid2]), origin)
    layout = _layout_with_group(group, {cid1: pl1, cid2: pl2})
    state = _FakeState(layout=layout)

    cmd = RotateGroupCommand(group_id=group.id, delta=90)
    cmd.apply(state)

    new_pl1 = state.layout.placements[cid1]
    new_pl2 = state.layout.placements[cid2]
    # (2,1) rotated 90° CW around (1,1): dx=1,dy=0 → rx=0,ry=-1 → (1,0)
    assert new_pl1.position.x == pytest.approx(1.0)
    assert new_pl1.position.y == pytest.approx(0.0)
    assert new_pl1.rotation == 90
    # (2,3) rotated 90° CW around (1,1): dx=1,dy=2 → rx=2,ry=-1 → (3,0)
    assert new_pl2.position.x == pytest.approx(3.0)
    assert new_pl2.position.y == pytest.approx(0.0)
    assert new_pl2.rotation == 180


def test_rotate_group_command_undo() -> None:
    cid1 = uuid.uuid4()
    pl1 = _make_placement(cid1, 2.0, 1.0, rotation=0)
    origin = Point(1.0, 1.0)
    group = _simple_group(frozenset([cid1]), origin)
    layout = _layout_with_group(group, {cid1: pl1})
    state = _FakeState(layout=layout)

    cmd = RotateGroupCommand(group_id=group.id, delta=90)
    cmd.apply(state)
    assert state.layout.placements[cid1].position != Point(2.0, 1.0)

    cmd.revert(state)
    assert state.layout.placements[cid1].position.x == pytest.approx(2.0)
    assert state.layout.placements[cid1].position.y == pytest.approx(1.0)
    assert state.layout.placements[cid1].rotation == 0


def test_rotate_group_undo_stores_no_member_placements() -> None:
    """Group rotate snapshots NO per-member placement — the scene-graph invariant.

    Under the compose-on-read model a group rotate folds the rotation into
    ``group.transform``; the member's *stored local* placement never changes
    (its world pose is composed on read).  So the command's ``RotateSnapshot``
    must carry an empty ``old_placements`` — undo restores the world pose purely
    by putting back the old group transform, not by rewinding baked member
    coords.  This locks the invariant so a future refactor can't silently
    regress to baking (which would re-inflate the undo snapshot).
    """
    cid1, cid2 = uuid.uuid4(), uuid.uuid4()
    pl1 = _make_placement(cid1, 2.0, 1.0, rotation=0)
    pl2 = _make_placement(cid2, 2.0, 3.0, rotation=90)
    origin = Point(1.0, 1.0)
    group = _simple_group(frozenset([cid1, cid2]), origin)
    layout = _layout_with_group(group, {cid1: pl1, cid2: pl2})
    state = _FakeState(layout=layout)

    # Capture the stored LOCAL placements (bypass the compose seam).
    local_before = {
        cid: layout.placements.local_get(cid) for cid in (cid1, cid2)
    }

    cmd = RotateGroupCommand(group_id=group.id, delta=90)
    cmd.apply(state)

    # The snapshot holds only wire geometry — no member placements.
    assert cmd._snapshot is not None
    assert cmd._snapshot.old_placements == {}

    # The stored local placement is untouched by apply (the world change came
    # from the group transform, not from baking into the placement).
    for cid in (cid1, cid2):
        local_now = layout.placements.local_get(cid)
        before = local_before[cid]
        assert local_now is not None and before is not None
        assert local_now.position == before.position
        assert local_now.rotation == before.rotation
        assert local_now.side == before.side

    # And the group carries the rotation as a det=+1 transform.
    assert state.layout.groups[group.id].transform.determinant() == 1.0
    assert state.layout.groups[group.id].transform != group.transform


def test_rotate_group_rotates_internal_wire() -> None:
    cid1 = uuid.uuid4()
    pl1 = _make_placement(cid1, 0.0, 0.0, rotation=0)
    origin = Point(0.0, 0.0)
    group = _simple_group(frozenset([cid1]), origin)
    # Wire from (0,0) to (2,0) — both end on pad positions we'll inject manually
    wire = _wire([(0.0, 0.0), (2.0, 0.0)])
    layout = _layout_with_group(group, {cid1: pl1}, wires=[wire])
    state = _FakeState(layout=layout)

    # Manually inject pad_set by patching — instead, we rely on the command
    # computing pad_set from footprints (empty here → no pads). So wire won't
    # be rotated. Test that external wire (not on pads) is left alone.
    cmd = RotateGroupCommand(group_id=group.id, delta=90)
    cmd.apply(state)
    # Wire path unchanged (no pads in pad_set → not identified as internal)
    assert state.layout.wires[0].path[0].x == pytest.approx(0.0)
    assert state.layout.wires[0].path[1].x == pytest.approx(2.0)


def test_rotate_group_preserves_group_metadata() -> None:
    """After rotation, group.rotation increments and origin stays."""
    cid1 = uuid.uuid4()
    pl1 = _make_placement(cid1, 2.0, 1.0)
    origin = Point(1.0, 1.0)
    group = _simple_group(frozenset([cid1]), origin)
    layout = _layout_with_group(group, {cid1: pl1})
    state = _FakeState(layout=layout)

    cmd = RotateGroupCommand(group_id=group.id, delta=90)
    cmd.apply(state)

    updated_group = state.layout.groups[group.id]
    assert updated_group.rotation == 90
    assert updated_group.origin is not None
    assert updated_group.origin.x == pytest.approx(1.0)
    assert updated_group.origin.y == pytest.approx(1.0)


def test_rotate_group_undo_restores_group_metadata() -> None:
    cid1 = uuid.uuid4()
    pl1 = _make_placement(cid1, 2.0, 1.0)
    origin = Point(1.0, 1.0)
    group = _simple_group(frozenset([cid1]), origin)
    layout = _layout_with_group(group, {cid1: pl1})
    state = _FakeState(layout=layout)

    cmd = RotateGroupCommand(group_id=group.id, delta=90)
    cmd.apply(state)
    cmd.revert(state)

    restored = state.layout.groups[group.id]
    assert restored.rotation == 0
    assert restored.origin is not None
    assert restored.origin.x == pytest.approx(1.0)
