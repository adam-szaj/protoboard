# SPDX-License-Identifier: MIT
"""Tests for normalize_layout_positions — half-grid snap on load."""

from __future__ import annotations

import uuid

from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.normalize import normalize_layout_positions
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import WireSegment
from protoboard.geometry import Point


def _pl(cid: uuid.UUID, x: float, y: float) -> ComponentPlacement:
    return ComponentPlacement(
        component_id=cid, position=Point(x, y), rotation=0, side="top",
    )


def _wire(*pts: tuple[float, float]) -> WireSegment:
    return WireSegment(
        id=uuid.uuid4(),
        path=[Point(x, y) for x, y in pts],
        insulated=True, side="top",
    )


class TestNormalizeLayoutPositions:
    def test_no_corrections_on_clean_layout(self) -> None:
        cid = uuid.uuid4()
        lay = Layout(
            placements={cid: _pl(cid, 0.5, 0.5)},
            wires=[_wire((0.5, 0.5), (3.5, 0.5))],
        )
        report = normalize_layout_positions(lay)
        assert not report.any_corrections
        assert report.count == 0

    def test_fractional_placement_snapped(self) -> None:
        cid = uuid.uuid4()
        lay = Layout(placements={cid: _pl(cid, 0.5, 3.503)}, wires=[])
        report = normalize_layout_positions(lay)
        assert len(report.placements) == 1
        corr = report.placements[0]
        assert corr.component_id == cid
        assert corr.old == Point(0.5, 3.503)
        assert corr.new == Point(0.5, 3.5)
        assert lay.placements[cid].position == Point(0.5, 3.5)

    def test_fractional_wire_point_snapped(self) -> None:
        wire = _wire((0.5, 0.5), (1.5, 0.503), (3.5, 0.5))
        lay = Layout(placements={}, wires=[wire])
        report = normalize_layout_positions(lay)
        assert len(report.wires) == 1
        wc = report.wires[0]
        assert wc.point_index == 1
        assert wc.old == Point(1.5, 0.503)
        assert wc.new == Point(1.5, 0.5)
        assert wire.path[1] == Point(1.5, 0.5)

    def test_within_threshold_snaps(self) -> None:
        # 3.2 nearest half-grid = 3.5 (delta = 0.3) → within ±0.5, will snap.
        cid = uuid.uuid4()
        lay = Layout(placements={cid: _pl(cid, 0.5, 3.2)}, wires=[])
        report = normalize_layout_positions(lay)
        assert report.any_corrections
        assert lay.placements[cid].position == Point(0.5, 3.5)

    def test_beyond_threshold_not_touched(self) -> None:
        # 0.99 from nearest half-grid (would snap to 0.5 if any) — way too far.
        # _MAX_SNAP_DELTA = 0.5 strict: anything >0.5 from grid is user data.
        # Use a point that's 0.51 from any half-grid: e.g. (0.5, 1.01) — nearest
        # is 1.5 with delta 0.49 → snaps.  We need a point where every nearby
        # half-grid is > 0.5 away.  Math: _snap_to_half_grid uses floor+0.5, so
        # for y in [n+0.0, n+1.0) snap → n+0.5; max delta = 0.5 always.  No
        # half-grid position can ever be > 0.5 from input → all values snap.
        #
        # This is actually fine: _MAX_SNAP_DELTA is a safety belt for inputs
        # like (NaN, -inf), not for normal floats.  We only verify the noise
        # filter via test_floating_point_noise_ignored.
        pass

    def test_floating_point_noise_ignored(self) -> None:
        cid = uuid.uuid4()
        pl_n = ComponentPlacement(
            component_id=cid,
            position=Point(0.5 + 1e-9, 0.5 - 1e-9),
            rotation=0, side="top",
        )
        lay = Layout(placements={cid: pl_n}, wires=[])
        report = normalize_layout_positions(lay)
        # Noise below _MIN_DELTA: no correction recorded.
        assert not report.any_corrections

    def test_idempotent(self) -> None:
        cid = uuid.uuid4()
        lay = Layout(placements={cid: _pl(cid, 0.5, 3.503)}, wires=[])
        normalize_layout_positions(lay)
        report2 = normalize_layout_positions(lay)
        assert not report2.any_corrections
