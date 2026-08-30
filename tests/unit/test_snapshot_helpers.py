# SPDX-License-Identifier: MIT
"""Shared snapshot/restore helpers for undo (placement + wire geometry).

These back the commands' apply()/revert() — extracted so the {cid: pl.copy()}
and {wid: (path, fragments)} snapshot patterns live in one place.
"""

from __future__ import annotations

import uuid

from protoboard.domain.layout.placement import (
    ComponentPlacement,
    restore_placements,
    snapshot_placements,
)
from protoboard.geometry import Point


def _pl(pos: Point, rotation: int = 0) -> ComponentPlacement:
    return ComponentPlacement(
        component_id=uuid.uuid4(),
        position=pos,
        rotation=rotation,
        side="top",
    )


class TestPlacementSnapshot:
    def test_snapshot_deep_copies(self) -> None:
        cid = uuid.uuid4()
        live = {cid: _pl(Point(2.5, 2.5))}
        saved = snapshot_placements(live, [cid])
        # Mutating the live placement must not touch the snapshot.
        live[cid].position = Point(9.5, 9.5)
        assert saved[cid].position == Point(2.5, 2.5)

    def test_snapshot_skips_missing_ids(self) -> None:
        cid = uuid.uuid4()
        live = {cid: _pl(Point(1.5, 1.5))}
        saved = snapshot_placements(live, [cid, uuid.uuid4()])
        assert set(saved) == {cid}

    def test_restore_writes_back(self) -> None:
        cid = uuid.uuid4()
        live = {cid: _pl(Point(0.5, 0.5), rotation=0)}
        saved = snapshot_placements(live, [cid])
        live[cid] = _pl(Point(5.5, 5.5), rotation=90)  # mutate
        restore_placements(live, saved)
        assert live[cid].position == Point(0.5, 0.5)
        assert live[cid].rotation == 0

    def test_round_trip(self) -> None:
        cid = uuid.uuid4()
        original = _pl(Point(3.5, 4.5), rotation=180)
        live = {cid: original}
        saved = snapshot_placements(live, [cid])
        live[cid] = _pl(Point(0.0, 0.0))
        restore_placements(live, saved)
        assert live[cid].position == original.position
        assert live[cid].rotation == original.rotation


class TestRebuildFragments:
    def test_swaps_sides_preserves_paths(self) -> None:
        from protoboard.domain.layout.wire import Fragment, rebuild_fragments

        frags = [
            Fragment(path=[Point(0, 0), Point(1, 0)], side="top"),
            Fragment(path=[Point(1, 0), Point(2, 0)], side="top"),
        ]
        out = rebuild_fragments(frags, ["bottom", "top"])
        assert [f.side for f in out] == ["bottom", "top"]
        assert [f.path for f in out] == [f.path for f in frags]

    def test_deep_copies_paths(self) -> None:
        from protoboard.domain.layout.wire import Fragment, rebuild_fragments

        frags = [Fragment(path=[Point(0, 0), Point(1, 0)], side="top")]
        out = rebuild_fragments(frags, ["top"])
        out[0].path.append(Point(2, 0))  # mutate the copy
        assert len(frags[0].path) == 2  # original untouched


class TestTransformWireGeometry:
    """Parity for the inlined point-transform-over-path+fragments loops.

    These assert the helper does exactly what the rotate / drag-move /
    stale-path-snap call-sites did inline before the extraction: map a
    point function over ``wire.path`` and every ``Fragment.path`` in place,
    preserving side, identity and knot refs.
    """

    def _translate(self, dx: float, dy: float):
        return lambda p: Point(p.x + dx, p.y + dy)

    def test_single_side_wire_translates_path_and_fragment(self) -> None:
        from protoboard.domain.layout.wire import Wire, transform_wire_geometry

        wire = Wire(id=uuid.uuid4(), path=[Point(0, 0), Point(2, 0)], side="top")
        transform_wire_geometry(wire, self._translate(1, 3))
        assert wire.path == [Point(1, 3), Point(3, 3)]
        # The single derived fragment tracks the path.
        assert len(wire.fragments) == 1
        assert wire.fragments[0].path == [Point(1, 3), Point(3, 3)]
        assert wire.fragments[0].side == "top"

    def test_multi_fragment_preserves_side_and_shared_boundary(self) -> None:
        from protoboard.domain.layout.wire import (
            Fragment,
            Wire,
            transform_wire_geometry,
        )

        wire = Wire(
            id=uuid.uuid4(),
            a=None,
            b=None,
            fragments=[
                Fragment(path=[Point(0, 0), Point(1, 0)], side="top"),
                Fragment(path=[Point(1, 0), Point(2, 0)], side="bottom"),
            ],
        )
        transform_wire_geometry(wire, self._translate(0, 5))
        # Per-fragment side is untouched.
        assert [f.side for f in wire.fragments] == ["top", "bottom"]
        # Boundary vertex stays shared (Wire.__post_init__ would raise otherwise).
        assert wire.fragments[0].path[-1] == wire.fragments[1].path[0]
        assert wire.fragments[0].path == [Point(0, 5), Point(1, 5)]
        assert wire.fragments[1].path == [Point(1, 5), Point(2, 5)]
        # Flattened path matches the transformed fragments.
        assert wire.path == [Point(0, 5), Point(1, 5), Point(2, 5)]

    def test_rotation_round_trip_matches_inline_loop(self) -> None:
        """A +90 then -90 rotation about a pivot restores the geometry.

        Mirrors rotate_components_around / restore_rotation, which now both
        route through this helper.
        """
        from protoboard.domain.layout.wire import Wire, transform_wire_geometry

        def rot(origin: Point, delta: int):
            def fn(p: Point) -> Point:
                dx, dy = p.x - origin.x, p.y - origin.y
                if delta % 360 == 90:
                    return Point(origin.x + dy, origin.y - dx)
                if delta % 360 == 270:
                    return Point(origin.x - dy, origin.y + dx)
                return p

            return fn

        origin = Point(1, 1)
        wire = Wire(id=uuid.uuid4(), path=[Point(2, 1), Point(2, 3)], side="top")
        before = list(wire.path)
        transform_wire_geometry(wire, rot(origin, 90))
        assert wire.path != before  # actually moved
        transform_wire_geometry(wire, rot(origin, 270))
        assert wire.path == before

    def test_identity_and_knot_refs_preserved(self) -> None:
        from protoboard.domain.layout.wire import (
            KnotRef,
            Wire,
            transform_wire_geometry,
        )

        wid = uuid.uuid4()
        a, b = KnotRef(id=uuid.uuid4()), KnotRef(id=uuid.uuid4())
        wire = Wire(
            id=wid,
            path=[Point(0, 0), Point(1, 0)],
            side="top",
            insulated=True,
            color="#abc",
            a=a,
            b=b,
        )
        transform_wire_geometry(wire, self._translate(10, 0))
        assert wire.id == wid
        assert wire.a is a and wire.b is b
        assert wire.insulated is True
        assert wire.color == "#abc"
