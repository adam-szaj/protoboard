# SPDX-License-Identifier: MIT
"""Tests that ``_from_logical_path`` and ``_remerge`` preserve wire identity.

Regression for the d33ea56 symptom: unweave on 3 input wires produced 7
output wires because every reconstructed segment minted a fresh UUID,
defeating highlight/recording/undo invariants.
"""

from __future__ import annotations

import uuid

from protoboard.domain.layout.wire import Wire
from protoboard.geometry import Point
from protoboard.services.routing.unweave import (
    _from_logical_path,
    _LogicalPoint,
    _remerge,
)


class TestFromLogicalPath:
    def test_no_seed_mints_fresh_ids(self) -> None:
        lp = [
            _LogicalPoint(point=Point(0, 0), side="top", insulated=False),
            _LogicalPoint(point=Point(1, 0), side="top", insulated=False),
        ]
        out = _from_logical_path(lp)
        assert len(out) == 1
        # Fresh id — but we just check it's a UUID, not asserting any value.
        assert isinstance(out[0].id, uuid.UUID)

    def test_seed_id_preserved_single_segment(self) -> None:
        target = uuid.uuid4()
        lp = [
            _LogicalPoint(point=Point(0, 0), side="top", insulated=False),
            _LogicalPoint(point=Point(1, 0), side="top", insulated=False),
        ]
        out = _from_logical_path(lp, seed_ids=[target])
        assert len(out) == 1
        assert out[0].id == target

    def test_seed_ids_consumed_in_emission_order(self) -> None:
        # A 2-segment split (top→bottom).
        lp = [
            _LogicalPoint(point=Point(0, 0), side="top", insulated=False),
            _LogicalPoint(point=Point(1, 0), side="top", insulated=False),
            _LogicalPoint(point=Point(2, 0), side="bottom", insulated=False),
            _LogicalPoint(point=Point(3, 0), side="bottom", insulated=False),
        ]
        a, b = uuid.uuid4(), uuid.uuid4()
        out = _from_logical_path(lp, seed_ids=[a, b])
        assert len(out) == 2
        assert out[0].id == a
        assert out[1].id == b

    def test_extra_emitted_segments_get_fresh_ids(self) -> None:
        # 2-segment split, only 1 seed → second segment freshly minted.
        lp = [
            _LogicalPoint(point=Point(0, 0), side="top", insulated=False),
            _LogicalPoint(point=Point(1, 0), side="top", insulated=False),
            _LogicalPoint(point=Point(2, 0), side="bottom", insulated=False),
            _LogicalPoint(point=Point(3, 0), side="bottom", insulated=False),
        ]
        seed = uuid.uuid4()
        out = _from_logical_path(lp, seed_ids=[seed])
        assert len(out) == 2
        assert out[0].id == seed
        assert out[1].id != seed

    def test_fewer_emitted_segments_drops_unused_seeds(self) -> None:
        # 1-segment output, 2 seeds — only the first is used.
        lp = [
            _LogicalPoint(point=Point(0, 0), side="top", insulated=False),
            _LogicalPoint(point=Point(1, 0), side="top", insulated=False),
        ]
        a, b = uuid.uuid4(), uuid.uuid4()
        out = _from_logical_path(lp, seed_ids=[a, b])
        assert len(out) == 1
        assert out[0].id == a


class TestRemergeIdPreservation:
    def test_fused_segment_keeps_first_id(self) -> None:
        first_id = uuid.uuid4()
        seg1 = Wire(
            id=first_id,
            path=[Point(0, 0), Point(1, 0)],
            insulated=False,
            side="top",
        )
        seg2 = Wire(
            id=uuid.uuid4(),
            path=[Point(1, 0), Point(2, 0)],
            insulated=False,
            side="top",
        )
        out = _remerge([seg1, seg2])
        assert len(out) == 1
        assert out[0].id == first_id
        assert out[0].path == [Point(0, 0), Point(1, 0), Point(2, 0)]

    def test_attribute_break_prevents_fuse(self) -> None:
        seg1 = Wire(
            id=uuid.uuid4(),
            path=[Point(0, 0), Point(1, 0)],
            insulated=False,
            side="top",
        )
        seg2 = Wire(
            id=uuid.uuid4(),
            path=[Point(1, 0), Point(2, 0)],
            insulated=False,
            side="bottom",
        )
        out = _remerge([seg1, seg2])
        assert len(out) == 2
        assert out[0].id == seg1.id
        assert out[1].id == seg2.id
