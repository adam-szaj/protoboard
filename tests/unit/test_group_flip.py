# SPDX-License-Identifier: MIT
"""Tests for group flip (FlipGroupCommand + the compose_placement det<0 branch).

A group flip folds a reflection (``det = -1``) into the group's ``transform``;
every member picks up the mirror + a board-side toggle when its placement is read
through the compose-on-read seam.  Owned wires flip physically (geometry mirrored,
side swapped).  The seam resolves each member's footprint via an injected resolver
(mirrors the session), so these tests wire one onto the test Layout.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.commands.group_cmds import (
    FlipGroupCommand,
    _reflection_about,
)
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.group import ComponentGroup
from protoboard.domain.layout.layout import ROOT_GROUP_ID, Layout
from protoboard.domain.layout.placement import (
    ComponentPlacement,
    pin_world_position,
)
from protoboard.geometry import Point


def _board() -> Board:
    return Board(root=BoardGrid(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        ),
        rows=100, cols=100, row_gap=0.0, col_gap=0.0,
    ))


_DEFN = ComponentDefinition(name="R", footprint=SIL(3), pin_names={1: "A", 2: "B", 3: "C"})


@dataclass
class _State:
    layout: Layout
    instances: dict = field(default_factory=dict)
    components: dict = field(default_factory=lambda: {"R": _DEFN})
    schematic: object = None
    board: Board = field(default_factory=_board)


def _make_state() -> _State:
    layout = Layout()
    layout.ensure_root_group()
    st = _State(layout=layout)

    # Wire the footprint resolver the seam's flip branch needs (mirrors session).
    def _resolve(cid: uuid.UUID):  # type: ignore[no-untyped-def]
        inst = st.instances.get(cid)
        if inst is None:
            return None
        return _DEFN.footprint

    layout.set_footprint_resolver(_resolve)
    return st


def _place(st: _State, x: float, y: float, rot: int = 0, side: str = "top") -> uuid.UUID:
    inst = ComponentInstance.new(
        definition_ref="R", refdes=f"R{len(st.instances)}", value="1k"
    ).bind(_DEFN, {})
    st.instances[inst.id] = inst
    st.layout.placements[inst.id] = ComponentPlacement(
        component_id=inst.id, position=Point(x, y), rotation=rot, side=side,  # type: ignore[arg-type]
    )
    return inst.id


def _attach_group(st: _State, comp_ids: frozenset[uuid.UUID]) -> uuid.UUID:
    g = ComponentGroup(id=uuid.uuid4(), name="G", component_ids=comp_ids)
    st.layout.groups[g.id] = g
    root = st.layout.groups[ROOT_GROUP_ID]
    st.layout.groups[ROOT_GROUP_ID] = root.replace(
        child_group_ids=root.child_group_ids | {g.id}
    )
    return g.id


def _world(st: _State, cids: list[uuid.UUID]) -> dict[uuid.UUID, list[tuple[float, float]]]:
    fp = _DEFN.footprint
    return {
        cid: [
            (round(pin_world_position(fp, st.layout.placements[cid], n).x, 4),
             round(pin_world_position(fp, st.layout.placements[cid], n).y, 4))
            for n in range(1, fp.pin_count() + 1)
        ]
        for cid in cids
    }


class TestFlipGroupCommand:
    def test_vertical_flip_mirrors_pins_about_centroid(self) -> None:
        st = _make_state()
        a = _place(st, 10.0, 10.0)
        b = _place(st, 14.0, 10.0)
        gid = _attach_group(st, frozenset({a, b}))
        before = _world(st, [a, b])

        FlipGroupCommand(group_id=gid, axis="v").apply(st)
        after = _world(st, [a, b])

        # Vertical flip about the pin centroid mirrors x, keeps y.  The centroid
        # x is shared, so each pin's mirrored x = 2*cx - x.
        all_x = [px for pins in before.values() for px, _ in pins]
        cx = sum(all_x) / len(all_x)
        for cid in (a, b):
            for (bx, by), (ax, ay) in zip(before[cid], after[cid]):
                assert ax == pytest.approx(2 * cx - bx)
                assert ay == pytest.approx(by)

    def test_members_toggle_side(self) -> None:
        st = _make_state()
        a = _place(st, 10.0, 10.0, side="top")
        gid = _attach_group(st, frozenset({a}))
        FlipGroupCommand(group_id=gid, axis="v").apply(st)
        assert st.layout.placements[a].side == "bottom"

    def test_group_transform_is_reflection(self) -> None:
        st = _make_state()
        a = _place(st, 10.0, 10.0)
        gid = _attach_group(st, frozenset({a}))
        FlipGroupCommand(group_id=gid, axis="h").apply(st)
        assert st.layout.groups[gid].transform.determinant() == -1.0

    def test_flip_twice_is_identity(self) -> None:
        st = _make_state()
        a = _place(st, 10.0, 10.0, rot=90)
        b = _place(st, 13.0, 12.0, rot=0)
        gid = _attach_group(st, frozenset({a, b}))
        before = _world(st, [a, b])

        FlipGroupCommand(group_id=gid, axis="v").apply(st)
        FlipGroupCommand(group_id=gid, axis="v").apply(st)
        after = _world(st, [a, b])
        assert after == before
        assert st.layout.groups[gid].transform.determinant() == 1.0

    def test_undo_restores_world_positions(self) -> None:
        st = _make_state()
        a = _place(st, 10.0, 10.0, rot=0)
        b = _place(st, 14.0, 13.0, rot=180)
        gid = _attach_group(st, frozenset({a, b}))
        before = _world(st, [a, b])

        cmd = FlipGroupCommand(group_id=gid, axis="h")
        cmd.apply(st)
        assert _world(st, [a, b]) != before
        cmd.revert(st)
        assert _world(st, [a, b]) == before
        assert st.layout.groups[gid].transform == ComponentGroup(
            id=gid, name="G"
        ).transform  # back to identity

    def test_undo_stores_no_member_placements(self) -> None:
        """Group flip snapshots NO per-member placement — the scene-graph
        invariant (mirror of the rotate case in test_group_rotation.py).

        The flip folds a reflection into ``group.transform``; each member's
        *stored local* placement is unchanged (the mirror + side toggle are
        composed on read).  ``FlipGroupCommand`` therefore carries no member
        placement snapshot at all — it holds only the old group + the
        reflection + the owned-wire ids.  Undo restores the world pose by
        putting back the old group transform, not by rewinding baked coords.
        """
        st = _make_state()
        a = _place(st, 10.0, 10.0, rot=0)
        b = _place(st, 14.0, 13.0, rot=90)
        gid = _attach_group(st, frozenset({a, b}))

        local_before = {
            cid: st.layout.placements.local_get(cid) for cid in (a, b)
        }

        cmd = FlipGroupCommand(group_id=gid, axis="v")
        cmd.apply(st)

        # The command holds no per-member placement snapshot (only the group,
        # the reflection and the owned-wire ids are saved for revert).
        assert not hasattr(cmd, "old_placements")
        assert not hasattr(cmd, "_saved_placements")

        # The stored local placement is untouched by apply — the world mirror +
        # side toggle came from the group transform, not from baking.
        for cid in (a, b):
            local_now = st.layout.placements.local_get(cid)
            before = local_before[cid]
            assert local_now is not None and before is not None
            assert local_now.position == before.position
            assert local_now.rotation == before.rotation
            assert local_now.side == before.side

        # The group carries the flip as a det=-1 transform.
        assert st.layout.groups[gid].transform.determinant() == -1.0

    def test_explicit_pivot_overrides_centroid(self) -> None:
        st = _make_state()
        a = _place(st, 10.0, 10.0)
        gid = _attach_group(st, frozenset({a}))
        # Vertical flip about x=0 → pin1 world x negates.
        FlipGroupCommand(group_id=gid, axis="v", pivot=Point(0.0, 0.0)).apply(st)
        p1 = pin_world_position(_DEFN.footprint, st.layout.placements[a], 1)
        assert p1.x == pytest.approx(-10.0)

    def test_nested_group_flip_propagates(self) -> None:
        # Inner group inside outer; flip the OUTER — the inner member mirrors too
        # because world_transform folds the outer reflection.
        st = _make_state()
        a = _place(st, 10.0, 10.0)
        inner = ComponentGroup(
            id=uuid.uuid4(), name="inner", component_ids=frozenset({a})
        )
        st.layout.groups[inner.id] = inner
        outer = ComponentGroup(
            id=uuid.uuid4(), name="outer", child_group_ids=frozenset({inner.id})
        )
        st.layout.groups[outer.id] = outer
        root = st.layout.groups[ROOT_GROUP_ID]
        st.layout.groups[ROOT_GROUP_ID] = root.replace(
            child_group_ids=root.child_group_ids | {outer.id}
        )
        before = _world(st, [a])
        FlipGroupCommand(group_id=outer.id, axis="v", pivot=Point(0.0, 0.0)).apply(st)
        after = _world(st, [a])
        # x mirrored about 0, y unchanged.
        for (bx, by), (ax, ay) in zip(before[a], after[a]):
            assert ax == pytest.approx(-bx)
            assert ay == pytest.approx(by)


class TestReflectionAbout:
    def test_vertical_negates_x_about_pivot(self) -> None:
        r = _reflection_about(Point(3.0, 0.0), "v")
        p = r.apply(Point(5.0, 7.0))
        assert p.x == pytest.approx(1.0)  # 2*3 - 5
        assert p.y == pytest.approx(7.0)

    def test_horizontal_negates_y_about_pivot(self) -> None:
        r = _reflection_about(Point(0.0, 2.0), "h")
        p = r.apply(Point(5.0, 7.0))
        assert p.x == pytest.approx(5.0)
        assert p.y == pytest.approx(-3.0)  # 2*2 - 7

    def test_reflection_is_involution(self) -> None:
        r = _reflection_about(Point(4.0, 6.0), "v")
        p = Point(9.0, 2.0)
        assert r.apply(r.apply(p)) == p
        assert r.determinant() == -1.0
