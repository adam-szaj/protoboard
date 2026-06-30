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
        component_id=uuid.uuid4(), position=pos, rotation=rotation, side="top",
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
