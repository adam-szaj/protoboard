# SPDX-License-Identifier: MIT
"""Tests for astar._segs_cross / _crossing_cost — low-level segment intersection.

Regression coverage for the diagonal-crossing bug: the previous implementation
decomposed diagonal A* steps into two orthogonal half-steps and only ran an
ortho×ortho test, so two genuinely crossing diagonals (e.g. (2,2)→(3,3) and
(2,3)→(3,2)) were silently classified as non-crossing → bare wires shorted.
"""

from __future__ import annotations

import math

from protoboard.geometry import Point
from protoboard.services.routing.astar import (
    _build_bare_seg_index,
    _crossing_cost,
    _is_perpendicular_diagonal_crossing,
    _segs_cross,
)
from protoboard.services.routing.cost_model import RoutingCostModel


# ---------------------------------------------------------------------------
# _segs_cross — pure geometry
# ---------------------------------------------------------------------------

class TestSegsCross:
    def test_perpendicular_orthogonals_cross(self) -> None:
        # Horizontal (0,1)→(2,1) crosses vertical (1,0)→(1,2) at (1,1).
        assert _segs_cross(Point(0, 1), Point(2, 1), Point(1, 0), Point(1, 2))

    def test_parallel_orthogonals_dont_cross(self) -> None:
        assert not _segs_cross(
            Point(0, 0), Point(2, 0), Point(0, 1), Point(2, 1)
        )

    def test_two_perpendicular_diagonals_cross(self) -> None:
        # (2,2)→(3,3) (slope +1) and (2,3)→(3,2) (slope -1) cross at (2.5, 2.5).
        # This was the silent-short bug — now must be detected.
        assert _segs_cross(
            Point(2, 2), Point(3, 3), Point(2, 3), Point(3, 2)
        )

    def test_two_parallel_diagonals_dont_cross(self) -> None:
        # Both slope +1, offset by 1 → never meet.
        assert not _segs_cross(
            Point(0, 0), Point(3, 3), Point(0, 1), Point(3, 4)
        )

    def test_diagonal_crosses_orthogonal(self) -> None:
        # Diagonal (0,0)→(4,4) crosses horizontal (0,2)→(4,2) at (2,2).
        assert _segs_cross(
            Point(0, 0), Point(4, 4), Point(0, 2), Point(4, 2)
        )

    def test_endpoint_touch_is_not_crossing(self) -> None:
        # T-junction at endpoint: the orientation test returns 0 → not strict.
        assert not _segs_cross(
            Point(0, 0), Point(2, 0), Point(2, 0), Point(2, 2)
        )

    def test_collinear_overlap_is_not_crossing(self) -> None:
        # Collinear overlap is handled by the wire-occupancy map upstream;
        # _segs_cross deliberately returns False for it.
        assert not _segs_cross(
            Point(0, 0), Point(4, 0), Point(2, 0), Point(6, 0)
        )


# ---------------------------------------------------------------------------
# _crossing_cost — integrates with cost model + bare-seg index
# ---------------------------------------------------------------------------

def _idx(*segs: tuple[Point, Point, str]):
    return _build_bare_seg_index(set(segs))


class TestCrossingCost:
    def test_no_bare_segments_zero_cost(self) -> None:
        cm = RoutingCostModel()
        cost = _crossing_cost(
            Point(0, 0), Point(1, 0), "top", _idx(), cm, insulated=False
        )
        assert cost == 0.0

    def test_bare_x_bare_orthogonal_is_inf(self) -> None:
        cm = RoutingCostModel()
        bare = _idx((Point(1, 0), Point(1, 2), "top"))
        cost = _crossing_cost(
            Point(0, 1), Point(2, 1), "top", bare, cm, insulated=False
        )
        assert math.isinf(cost)

    def test_bare_x_bare_perpendicular_diagonals_default_inf(self) -> None:
        # Default cost_model has crossing_bare_bare_diagonal = inf, so even
        # the perpendicular-diagonal exception is forbidden until the bare-
        # diagonal mode lowers the cost.
        cm = RoutingCostModel()
        bare = _idx((Point(2, 3), Point(3, 2), "top"))
        cost = _crossing_cost(
            Point(2, 2), Point(3, 3), "top", bare, cm, insulated=False
        )
        assert math.isinf(cost)

    def test_bare_x_bare_perpendicular_diagonals_finite_in_bare_diagonal_mode(
        self,
    ) -> None:
        # In bare-diagonal routing mode the perpendicular crossing at a mesh
        # cell centre (int, int) is permitted with a finite cost.
        # Pads at (1.5,1.5), (2.5,2.5), (1.5,2.5), (2.5,1.5); the two diagonals
        # cross at (2.0, 2.0) — that is the mesh-cell centre between four pads.
        cm = RoutingCostModel(crossing_bare_bare_diagonal=2.0)
        bare = _idx((Point(1.5, 2.5), Point(2.5, 1.5), "top"))
        cost = _crossing_cost(
            Point(1.5, 1.5), Point(2.5, 2.5), "top", bare, cm, insulated=False
        )
        assert cost == 2.0

    def test_bare_x_bare_parallel_diagonals_in_bare_diagonal_mode_no_cost(
        self,
    ) -> None:
        # Parallel diagonals never intersect → no crossing, no cost.
        cm = RoutingCostModel(crossing_bare_bare_diagonal=2.0)
        bare = _idx((Point(0.5, 1.5), Point(3.5, 4.5), "top"))
        cost = _crossing_cost(
            Point(0.5, 0.5), Point(3.5, 3.5), "top", bare, cm, insulated=False
        )
        assert cost == 0.0

    def test_bare_x_bare_diag_through_pad_is_inf_in_bare_diagonal_mode(
        self,
    ) -> None:
        # Two diagonals crossing at a half-integer point (i.e. on a pad,
        # not at a mesh-cell centre): NOT exempt — must remain banned.
        cm = RoutingCostModel(crossing_bare_bare_diagonal=2.0)
        # Both diagonals pass through (1.5, 1.5):
        #   a: (0.5, 0.5) → (2.5, 2.5)
        #   b: (0.5, 2.5) → (2.5, 0.5)
        bare = _idx((Point(0.5, 2.5), Point(2.5, 0.5), "top"))
        cost = _crossing_cost(
            Point(0.5, 0.5), Point(2.5, 2.5), "top", bare, cm, insulated=False
        )
        assert math.isinf(cost)

    def test_insulated_x_bare_uses_jumper_cost(self) -> None:
        cm = RoutingCostModel()
        bare = _idx((Point(1, 0), Point(1, 2), "top"))
        cost = _crossing_cost(
            Point(0, 1), Point(2, 1), "top", bare, cm, insulated=True
        )
        assert cost == cm.crossing_insulated_bare

    def test_other_layer_segments_ignored(self) -> None:
        cm = RoutingCostModel()
        bare = _idx((Point(1, 0), Point(1, 2), "bottom"))
        cost = _crossing_cost(
            Point(0, 1), Point(2, 1), "top", bare, cm, insulated=False
        )
        assert cost == 0.0


# ---------------------------------------------------------------------------
# _is_perpendicular_diagonal_crossing — predicate used by _crossing_cost
# ---------------------------------------------------------------------------

class TestPerpendicularDiagonalCrossing:
    def test_classic_pair(self) -> None:
        # / and \ meeting at (2.5, 2.5) — wait, (2.5, 2.5) is not (int,int).
        # Use (2,2)→(3,3) and (2,3)→(3,2) which cross at (2.5, 2.5).
        # That's a half-integer point, not the mesh-cell centre.
        # Build a real mesh-cell-centre crossing: pads at (1.5, 1.5), (2.5, 2.5)
        # connected by a / diagonal, and pads at (1.5, 2.5), (2.5, 1.5)
        # connected by a \ diagonal.  They cross at (2.0, 2.0) — int.
        a1, a2 = Point(1.5, 1.5), Point(2.5, 2.5)
        b1, b2 = Point(1.5, 2.5), Point(2.5, 1.5)
        assert _is_perpendicular_diagonal_crossing(a1, a2, b1, b2)

    def test_orthogonal_pair_rejected(self) -> None:
        assert not _is_perpendicular_diagonal_crossing(
            Point(0, 1), Point(2, 1), Point(1, 0), Point(1, 2)
        )

    def test_parallel_diagonals_rejected(self) -> None:
        assert not _is_perpendicular_diagonal_crossing(
            Point(0, 0), Point(2, 2), Point(0, 1), Point(2, 3)
        )

    def test_diag_x_ortho_rejected(self) -> None:
        assert not _is_perpendicular_diagonal_crossing(
            Point(0, 0), Point(2, 2), Point(0, 1), Point(2, 1)
        )

    def test_intersection_at_pad_rejected(self) -> None:
        # Two perpendicular diagonals crossing at (1.5, 1.5) — a pad position.
        # Mesh-cell centres are at integer coordinates only.
        a1, a2 = Point(0.5, 0.5), Point(2.5, 2.5)
        b1, b2 = Point(0.5, 2.5), Point(2.5, 0.5)
        # They intersect at (1.5, 1.5) — non-integer → reject.
        assert not _is_perpendicular_diagonal_crossing(a1, a2, b1, b2)
