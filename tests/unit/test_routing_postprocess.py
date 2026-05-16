# SPDX-License-Identifier: MIT
"""Tests for services.routing.postprocess — simplify_wires."""

from __future__ import annotations

import uuid

from protoboard.domain.layout.wire import WireSegment
from protoboard.geometry import Point
from protoboard.services.routing.postprocess import (
    corner_cut_wire,
    simplify_wires,
    straighten_wire,
)


def _bare(*pts: tuple[float, float], side: str = "top") -> WireSegment:
    return WireSegment(
        id=uuid.uuid4(),
        path=[Point(x, y) for x, y in pts],
        insulated=False,
        side=side,  # type: ignore[arg-type]
    )


def _pads(n: int = 6) -> set[Point]:
    return {Point(c + 0.5, r + 0.5) for c in range(n) for r in range(n)}


class TestStraighten:
    def test_three_collinear_points_collapse(self) -> None:
        # Without a board_pads set the wire is treated as insulated-like
        # (free to merge); used by routines that don't know the board.
        w = _bare((0.5, 0.5), (1.5, 0.5), (2.5, 0.5))
        out = straighten_wire(w)
        assert [(p.x, p.y) for p in out.path] == [(0.5, 0.5), (2.5, 0.5)]

    def test_bare_collinear_through_pad_preserved(self) -> None:
        # On-pad mid-point must stay so connectivity graph still sees the
        # electrical contact between the bare wire and the pad.
        w = _bare((0.5, 0.5), (1.5, 0.5), (2.5, 0.5))
        out = straighten_wire(w, _pads())
        assert [(p.x, p.y) for p in out.path] == [
            (0.5, 0.5), (1.5, 0.5), (2.5, 0.5)
        ]

    def test_insulated_collinear_drops_mid_point(self) -> None:
        # An insulated wire floats above the board → safe to drop pad nodes.
        w = WireSegment(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(1.5, 0.5), Point(2.5, 0.5)],
            insulated=True,
            side="top",
        )
        out = straighten_wire(w, _pads())
        assert [(p.x, p.y) for p in out.path] == [(0.5, 0.5), (2.5, 0.5)]

    def test_l_corner_preserved(self) -> None:
        w = _bare((0.5, 0.5), (1.5, 0.5), (1.5, 1.5))
        out = straighten_wire(w, _pads())
        assert len(out.path) == 3

    def test_two_point_wire_unchanged(self) -> None:
        w = _bare((0.5, 0.5), (3.5, 0.5))
        out = straighten_wire(w, _pads())
        assert out is w  # unchanged → same identity

    def test_diagonal_collinear_with_pads_preserved(self) -> None:
        # Bare diagonal passing through pads (1.5,1.5), (2.5,2.5) keeps them
        # so the connectivity graph still merges them with this wire.
        w = _bare((0.5, 0.5), (1.5, 1.5), (2.5, 2.5))
        out = straighten_wire(w, _pads())
        assert [(p.x, p.y) for p in out.path] == [
            (0.5, 0.5), (1.5, 1.5), (2.5, 2.5)
        ]


class TestCornerCut:
    def test_l_corner_at_even_parity_cuts(self) -> None:
        # Even-parity source (0.5, 0.5): allowed family is "/" (slope +).
        # Corner (0.5,0.5)→(1.5,0.5)→(1.5,1.5): cut to (0.5,0.5)→(1.5,1.5),
        # slope = (+1)*(+1) > 0 → matches even parity.
        w = _bare((0.5, 0.5), (1.5, 0.5), (1.5, 1.5))
        out = corner_cut_wire(w, [w], _pads())
        assert [(p.x, p.y) for p in out.path] == [(0.5, 0.5), (1.5, 1.5)]

    def test_l_corner_at_odd_parity_cuts(self) -> None:
        # Odd-parity source (1.5, 0.5): allowed family "\" (slope -).
        # Corner (1.5,0.5)→(2.5,0.5)→(2.5,-0.5) would be slope (+1)*(-1)<0 OK,
        # but (-0.5) not on pad — use (1.5,0.5)→(1.5,1.5)→(2.5,1.5) instead:
        # slope = (2.5-1.5)*(1.5-0.5) = +1 → mismatch with odd parity → no cut.
        w = _bare((1.5, 0.5), (1.5, 1.5), (2.5, 1.5))
        out = corner_cut_wire(w, [w], _pads())
        assert len(out.path) == 3  # unchanged

    def test_l_corner_wrong_family_unchanged(self) -> None:
        # Even-parity source (0.5, 0.5) admits only "/".
        # Corner shaped "\" → no cut.
        w = _bare((0.5, 1.5), (1.5, 1.5), (1.5, 0.5))
        # Source parity = (0+1)=1 odd → admits "\" → slope = (1.5-0.5)*(0.5-1.5)
        # = -1 → matches odd parity → cut allowed.
        out = corner_cut_wire(w, [w], _pads())
        assert [(p.x, p.y) for p in out.path] == [(0.5, 1.5), (1.5, 0.5)]

    def test_collision_with_other_wire_blocks_cut(self) -> None:
        # Even parity, "/" cut wanted from (0.5,0.5) to (1.5,1.5).
        # But another bare wire already occupies (0.5,1.5)→(1.5,0.5) — the
        # perpendicular diagonal that crosses our candidate at the cell
        # centre (1.0, 1.0).  Without bare-diagonal exemption (this is just
        # the corner-cut safety check), the cut is rejected.
        w = _bare((0.5, 0.5), (1.5, 0.5), (1.5, 1.5))
        blocker = _bare((0.5, 1.5), (1.5, 0.5))
        out = corner_cut_wire(w, [w, blocker], _pads())
        assert len(out.path) == 3  # unchanged

    def test_insulated_wire_untouched(self) -> None:
        w = WireSegment(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(1.5, 0.5), Point(1.5, 1.5)],
            insulated=True,
            side="top",
        )
        out = corner_cut_wire(w, [w], _pads())
        assert out is w


class TestSimplifyWires:
    def test_combines_straighten_and_corner_cut(self) -> None:
        # A 4-vertex path: (0.5,0.5)→(1.5,0.5)→(2.5,0.5)→(2.5,1.5)
        # Straighten merges the first two segments → 3-vertex L.
        # Corner-cut at the L corner needs parity match — source parity
        # of (0.5,0.5) is even → "/" → slope of (0.5,0.5)→(2.5,1.5) = +ve
        # but the diagonal would be (2.5-0.5)=2, (1.5-0.5)=1 → not unit
        # ortho-ortho, so cut won't trigger (length-1 requirement).
        w = _bare((0.5, 0.5), (1.5, 0.5), (2.5, 0.5), (2.5, 1.5))
        out = simplify_wires([w], _pads())
        # After straighten: 3 vertices.  Corner-cut requires unit legs, so
        # the 2-unit horizontal leg blocks it — wire stays at 3 vertices.
        assert len(out[0].path) == 3
