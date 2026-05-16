# SPDX-License-Identifier: MIT
"""Tests for diagonal-parity bare-wire routing mode in A*.

The mode restricts diagonal moves to a single 45° family determined by the
source pad's parity, so two bare paths from pads of different parity can
safely cross at a mesh-cell centre (where they are guaranteed perpendicular).
"""

from __future__ import annotations

from protoboard.geometry import Point
from protoboard.services.routing.astar import _astar_parallel
from protoboard.services.routing.cost_model import RoutingCostModel


def _empty_board(n: int = 10) -> dict[str, set[Point]]:
    """n × n grid of pads at half-integer positions on both layers."""
    pads = {Point(c + 0.5, r + 0.5) for c in range(n) for r in range(n)}
    return {"top": pads, "bottom": pads}


def _step_kinds(path: list) -> list[str]:
    """Classify each step in a path: 'ortho', 'diag_pos' (/), 'diag_neg' (\\)."""
    kinds = []
    pts = [step[0] for step in path]
    for a, b in zip(pts, pts[1:]):
        dx, dy = b.x - a.x, b.y - a.y
        if dx == 0.0 or dy == 0.0:
            kinds.append("ortho")
        elif dx * dy > 0.0:
            kinds.append("diag_pos")
        else:
            kinds.append("diag_neg")
    return kinds


class TestParityFilter:
    def test_even_parity_source_uses_pos_family(self) -> None:
        """Pad at (0.5, 0.5): floor sum = 0+0 = 0 → even → '/' family only."""
        cm = RoutingCostModel(
            bare_diagonal_mode=True, crossing_bare_bare_diagonal=2.0
        )
        result = _astar_parallel(
            valid_nodes=_empty_board(),
            source=Point(0.5, 0.5),
            source_side="top",
            target_set={Point(4.5, 4.5)},
            cost_model=cm,
            insulated=False,
            occupied={},
            same_net={},
            near_pins=set(),
            near_pin_cost=0.0,
            bare_idx={},
            vias=set(),
            forbidden_edges=set(),
            penalised={},
        )
        assert result is not None
        kinds = _step_kinds(result.path)
        # Must contain at least one diagonal (since target is diagonal from src).
        assert any(k.startswith("diag") for k in kinds)
        # No diag_neg steps allowed — that's the wrong family for parity 0.
        assert "diag_neg" not in kinds

    def test_odd_parity_source_uses_neg_family(self) -> None:
        """Pad at (1.5, 0.5): floor sum = 1+0 = 1 → odd → '\\' family only."""
        cm = RoutingCostModel(
            bare_diagonal_mode=True, crossing_bare_bare_diagonal=2.0
        )
        result = _astar_parallel(
            valid_nodes=_empty_board(),
            source=Point(1.5, 0.5),
            source_side="top",
            target_set={Point(5.5, 4.5)},
            cost_model=cm,
            insulated=False,
            occupied={},
            same_net={},
            near_pins=set(),
            near_pin_cost=0.0,
            bare_idx={},
            vias=set(),
            forbidden_edges=set(),
            penalised={},
        )
        assert result is not None
        kinds = _step_kinds(result.path)
        # Source parity is odd → '\' family only.  But moving from (1.5, 0.5)
        # toward (5.5, 4.5) is a +x +y diagonal which is the '/' family —
        # router must compensate with orthogonal steps, not flip families.
        assert "diag_pos" not in kinds

    def test_parity_filter_disabled_when_mode_off(self) -> None:
        """Without bare_diagonal_mode the filter is bypassed entirely."""
        cm = RoutingCostModel()  # default — bare_diagonal_mode = False
        result = _astar_parallel(
            valid_nodes=_empty_board(),
            source=Point(0.5, 0.5),
            source_side="top",
            target_set={Point(4.5, 4.5)},
            cost_model=cm,
            insulated=False,
            occupied={},
            same_net={},
            near_pins=set(),
            near_pin_cost=0.0,
            bare_idx={},
            vias=set(),
            forbidden_edges=set(),
            penalised={},
        )
        assert result is not None
        # Direct diagonal path is allowed — straightest route.
        kinds = _step_kinds(result.path)
        assert kinds.count("diag_pos") + kinds.count("diag_neg") >= 1


class TestCrossings:
    def test_two_perpendicular_paths_can_cross(self) -> None:
        """Even-parity '/' path and odd-parity '\\' path are perpendicular at any meeting cell.

        We pre-seed the bare-segment index with the '\' wire already placed,
        and verify the '/' path is found (would have been blocked in the
        regular bare mode by crossing_bare_bare = inf).
        """
        from protoboard.services.routing.astar import _build_bare_seg_index

        # Existing bare segment: '\' from (1.5, 2.5) to (2.5, 1.5).
        bare = _build_bare_seg_index({(Point(1.5, 2.5), Point(2.5, 1.5), "top")})

        cm = RoutingCostModel(
            bare_diagonal_mode=True, crossing_bare_bare_diagonal=2.0
        )
        # Route a '/' path from (1.5, 1.5) to (2.5, 2.5) — the two diagonals
        # cross at (2.0, 2.0) which is a mesh-cell centre.
        result = _astar_parallel(
            valid_nodes=_empty_board(),
            source=Point(1.5, 1.5),
            source_side="top",
            target_set={Point(2.5, 2.5)},
            cost_model=cm,
            insulated=False,
            occupied={},
            same_net={},
            near_pins=set(),
            near_pin_cost=0.0,
            bare_idx=bare,
            vias=set(),
            forbidden_edges=set(),
            penalised={},
        )
        assert result is not None
        # The crossing cost (2.0) is incurred — total cost should reflect it.
        # Direct diagonal would be √2 ≈ 1.414 free + 2.0 crossing = ~3.414.
        # Allow some slack for tie-breaking.
        assert result.cost < 10.0
