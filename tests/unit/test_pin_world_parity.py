# SPDX-License-Identifier: MIT
"""Characterization / parity guard for the group live-compose migration.

Records the exact ``pin_world_position`` world Points a component's pins land on,
across a matrix of {rotation} × {side} × {pin_offsets}, read through
``layout.placements`` — the seam that the FLATTEN→scene-graph migration
(docs plan) replaces with a compose-on-read shim.

Under FLATTEN (today) a group does not affect a member's world position at all —
the member carries an absolute placement.  Under LIVE COMPOSE the member carries a
group-relative placement and ``layout.placements[cid]`` composes the group
transform on read.  **The migration must keep these world Points bit-identical**:
this file is the guard that "survives the swap" (CLAUDE.md §Parity).  Phase 3 must
leave every assertion here green without editing the test.

The values are pinned as literals so a regression is a visible diff, not a
recomputation that silently tracks the code.
"""

from __future__ import annotations

import uuid

from protoboard.domain.components.footprints import DIL, SIL, Footprint
from protoboard.domain.layout.group import ComponentGroup
from protoboard.domain.layout.layout import ROOT_GROUP_ID, Layout
from protoboard.domain.layout.placement import (
    ComponentPlacement,
    compose_placement,
    pin_world_position,
    PlacementCollection,
)
from protoboard.geometry import Point, Transform2D, Vector


def _rotation_about(origin: Point, delta: int) -> Transform2D:
    o = Vector(origin.x, origin.y)
    return (
        Transform2D.translation(o)
        .compose(Transform2D.rotation(delta // 90))
        .compose(Transform2D.translation(-o))
    )


def _key(p: Point) -> tuple[float, float]:
    return (round(p.x, 4), round(p.y, 4))


def _world_pins(
    fp: Footprint, pl: ComponentPlacement
) -> list[tuple[float, float]]:
    return [
        _key(pin_world_position(fp, pl, n))
        for n in range(1, fp.pin_count() + 1)
    ]


def _placed(
    *,
    position: Point,
    rotation: int,
    side: str,
    pin_offsets: dict[int, Vector] | None = None,
) -> ComponentPlacement:
    return ComponentPlacement(
        component_id=uuid.uuid4(),
        position=position,
        rotation=rotation,
        side=side,  # type: ignore[arg-type]
        pin_offsets=pin_offsets or {},
    )


class TestPinWorldParityThroughPlacements:
    """World positions read through layout.placements — the migration seam.

    Today these read the stored placement directly; after the seam they read a
    composed placement.  With all group transforms identity (the state through
    Phase 2, and for ungrouped components forever) the two must be identical —
    that is what these literal snapshots lock down.
    """

    def test_sil3_rotations_top(self) -> None:
        fp = SIL(3)
        cid = uuid.uuid4()
        for rot, expected in {
            0: [(10.0, 10.0), (10.0, 11.0), (10.0, 12.0)],
            90: [(10.0, 10.0), (11.0, 10.0), (12.0, 10.0)],
            180: [(10.0, 10.0), (10.0, 9.0), (10.0, 8.0)],
            270: [(10.0, 10.0), (9.0, 10.0), (8.0, 10.0)],
        }.items():
            pl = _placed(position=Point(10.0, 10.0), rotation=rot, side="top")
            layout = Layout(placements=PlacementCollection({cid: pl}))
            assert _world_pins(fp, layout.placements[cid]) == expected, rot

    def test_sil3_bottom_is_mirror(self) -> None:
        # SIL pins run down +y (x=0) → the flip axis is parallel to y → reflect x
        # about x=0, which leaves x=0 pins in place: bottom == top for a SIL.
        fp = SIL(3)
        cid = uuid.uuid4()
        pl = _placed(position=Point(5.0, 5.0), rotation=0, side="bottom")
        layout = Layout(placements=PlacementCollection({cid: pl}))
        assert _world_pins(fp, layout.placements[cid]) == [
            (5.0, 5.0), (5.0, 6.0), (5.0, 7.0),
        ]

    def test_dil8_top_and_bottom(self) -> None:
        fp = DIL(8)
        cid = uuid.uuid4()
        pl_top = _placed(position=Point(20.0, 20.0), rotation=0, side="top")
        layout = Layout(placements=PlacementCollection({cid: pl_top}))
        assert _world_pins(fp, layout.placements[cid]) == [
            (20.0, 20.0), (20.0, 21.0), (20.0, 22.0), (20.0, 23.0),
            (23.0, 23.0), (23.0, 22.0), (23.0, 21.0), (23.0, 20.0),
        ]
        # Bottom: DIL columns (x=0,3) → reflect x about x=1.5 → columns swap on
        # the same holes (pin 1 lands where pin 8 was).
        pl_bot = _placed(position=Point(20.0, 20.0), rotation=0, side="bottom")
        layout_b = Layout(placements=PlacementCollection({cid: pl_bot}))
        assert _world_pins(fp, layout_b.placements[cid]) == [
            (23.0, 20.0), (23.0, 21.0), (23.0, 22.0), (23.0, 23.0),
            (20.0, 23.0), (20.0, 22.0), (20.0, 21.0), (20.0, 20.0),
        ]

    def test_pin_offsets_applied(self) -> None:
        fp = SIL(2)
        cid = uuid.uuid4()
        pl = _placed(
            position=Point(0.0, 0.0), rotation=0, side="top",
            pin_offsets={2: Vector(0.5, 0.0)},
        )
        layout = Layout(placements=PlacementCollection({cid: pl}))
        assert _world_pins(fp, layout.placements[cid]) == [
            (0.0, 0.0), (0.5, 1.0),
        ]

    def test_get_and_values_agree_with_index(self) -> None:
        # All three read paths on layout.placements must yield the same effective
        # placement (the seam must compose identically for [], .get, .values).
        fp = SIL(3)
        cid = uuid.uuid4()
        pl = _placed(position=Point(7.0, 3.0), rotation=90, side="top")
        layout = Layout(placements=PlacementCollection({cid: pl}))
        via_index = _world_pins(fp, layout.placements[cid])
        via_get = _world_pins(fp, layout.placements.get(cid))  # type: ignore[arg-type]
        via_values = _world_pins(fp, next(iter(layout.placements.values())))
        assert via_index == via_get == via_values


class TestGroupedMemberComposesThroughSeam:
    """A grouped member with a non-identity group transform reads its WORLD pins
    through the seam (Phase 3, the scene-graph read path).

    The stored placement is group-LOCAL; ``layout.placements[cid]`` folds the
    group transform on read.  These snapshots pin the composed world positions
    for a group rotated about an origin — the same Points the pre-scene-graph
    FLATTEN produced by baking the rotation into the placement.
    """

    def _grouped_layout(
        self, cid: uuid.UUID, local: ComponentPlacement, transform: Transform2D
    ) -> Layout:
        gid = uuid.uuid4()
        return Layout(
            placements=PlacementCollection({cid: local}),
            groups={
                ROOT_GROUP_ID: ComponentGroup(
                    id=ROOT_GROUP_ID, name="", child_group_ids=frozenset({gid})
                ),
                gid: ComponentGroup(
                    id=gid,
                    name="G",
                    component_ids=frozenset({cid}),
                    transform=transform,
                ),
            },
        )

    def test_group_rotation_90_composes(self) -> None:
        # Local SIL(3) at (10,10) rot 0; group rotated 90° CW about (10,10).
        # Pin1 sits on the pivot → stays; the row swings from +y to +x.
        fp = SIL(3)
        cid = uuid.uuid4()
        local = _placed(position=Point(10.0, 10.0), rotation=0, side="top")
        layout = self._grouped_layout(
            cid, local, _rotation_about(Point(10.0, 10.0), 90)
        )
        assert _world_pins(fp, layout.placements[cid]) == [
            (10.0, 10.0), (11.0, 10.0), (12.0, 10.0),
        ]

    def test_group_rotation_matches_baked_flatten(self) -> None:
        # For every rotation the composed (local + group transform) world pins
        # equal the FLATTEN result (baking the same rotation into the placement).
        fp = DIL(8)
        cid = uuid.uuid4()
        origin = Point(4.0, 7.0)
        local = _placed(position=Point(4.0, 7.0), rotation=90, side="bottom")
        for delta in (90, 180, 270):
            t = _rotation_about(origin, delta)
            layout = self._grouped_layout(cid, local, t)
            baked = compose_placement(t, local)  # the FLATTEN equivalent
            assert (
                _world_pins(fp, layout.placements[cid])
                == _world_pins(fp, baked)
            ), delta
