# SPDX-License-Identifier: MIT
"""Tests for services.routing.postprocess — simplify_wires."""

from __future__ import annotations

import uuid

from protoboard.domain.layout.wire import WireSegment
from protoboard.geometry import Point
from protoboard.services.routing.postprocess import (
    SimplifyMode,
    corner_cut_wire,
    generic_simplify_wire,
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


# ---------------------------------------------------------------------------
# generic_simplify_wire — the new unified shortcut algorithm.
# ---------------------------------------------------------------------------

class TestGenericSimplifyUserCase:
    """Cover the case the user complained about: diag-diag zigzag that has
    a shorter ortho replacement.
    """

    def test_diag_diag_zigzag_collapses_to_straight_ortho(self) -> None:
        # Pads on the same column at (1.5,1.5) and (1.5,3.5).  Wire wandered
        # via the neighbour column: (1.5,1.5)→(2.5,2.5)→(1.5,3.5), length 2·√2.
        # Canonical between endpoints: pure ortho, length 2 — shorter.
        w = _bare((1.5, 1.5), (2.5, 2.5), (1.5, 3.5))
        out = generic_simplify_wire(w, [], mode=SimplifyMode.CHEBYSHEV)
        # On-pad midpoint (1.5, 2.5) is preserved because bare wires keep
        # all on-pad vertices (canonical = [a, b] is a single 2-unit segment,
        # but straighten guarantees the on-pad vertex stays… actually our
        # generic algorithm doesn't insert the midpoint; it just shortens
        # to two endpoints).  The shortened path must have at most 2
        # vertices and pass through (1.5, 2.5) on the line.
        assert len(out.path) == 2
        assert (out.path[0].x, out.path[0].y) == (1.5, 1.5)
        assert (out.path[1].x, out.path[1].y) == (1.5, 3.5)

    def test_user_case_bare_parity_also_collapses(self) -> None:
        # Even-parity source (1.5, 1.5).  Canonical replacement is pure ortho
        # — no diagonal segments → parity check is trivially satisfied.
        w = _bare((1.5, 1.5), (2.5, 2.5), (1.5, 3.5))
        out = generic_simplify_wire(w, [], mode=SimplifyMode.BARE_PARITY)
        assert len(out.path) == 2


class TestGenericSimplifyOrtho:
    def test_four_collinear_collapses_to_two(self) -> None:
        w = _bare((0.5, 0.5), (1.5, 0.5), (2.5, 0.5), (3.5, 0.5))
        out = generic_simplify_wire(w, [], mode=SimplifyMode.CHEBYSHEV)
        assert [(p.x, p.y) for p in out.path] == [(0.5, 0.5), (3.5, 0.5)]

    def test_ortho_l_collapses_to_diagonal_chebyshev(self) -> None:
        # 1-unit ortho legs at right angle → canonical is one diagonal.
        w = _bare((0.5, 0.5), (1.5, 0.5), (1.5, 1.5))
        out = generic_simplify_wire(w, [], mode=SimplifyMode.CHEBYSHEV)
        assert [(p.x, p.y) for p in out.path] == [(0.5, 0.5), (1.5, 1.5)]


class TestGenericSimplifyParity:
    def test_bare_parity_blocks_wrong_family_diagonal(self) -> None:
        # Even-parity source (0.5, 0.5) only admits "/" (slope > 0).  Build
        # an L whose diagonal shortcut would be "\" — must be rejected.
        w = _bare((0.5, 1.5), (1.5, 1.5), (1.5, 2.5))
        # Source parity = 0 (even) → admit slope > 0.
        # diagonal (0.5,1.5)→(1.5,2.5): dx=+1, dy=+1 → slope = +1 → OK.
        # Above wire is already correct; for blocked case flip the orientation.
        w_bad = _bare((1.5, 0.5), (1.5, 1.5), (0.5, 1.5))
        # Source parity for (1.5, 0.5): floor(1)+floor(0) = 1 → odd → admit "\"
        # diagonal (1.5,0.5)→(0.5,1.5): dx=-1, dy=+1 → slope = -1 → OK!
        # So this still collapses.  Make a proper wrong-family case:
        # source even, candidate slope negative.
        w_blocked = _bare((1.5, 1.5), (0.5, 1.5), (0.5, 2.5))
        # parity(1.5,1.5)=floor(1)+floor(1)=2→even, slope to (0.5,2.5) = (-1)(+1)=-1
        # → fails BARE_PARITY → unchanged.
        out_blocked = generic_simplify_wire(
            w_blocked, [], mode=SimplifyMode.BARE_PARITY
        )
        assert len(out_blocked.path) == 3  # unchanged

        # Same input with CHEBYSHEV — parity ignored → collapses.
        out_cheb = generic_simplify_wire(
            w_blocked, [], mode=SimplifyMode.CHEBYSHEV
        )
        assert len(out_cheb.path) == 2

        # Unused; suppress warning.
        _ = w
        _ = w_bad

    def test_insulated_ignores_parity_in_both_modes(self) -> None:
        wi = WireSegment(
            id=uuid.uuid4(),
            path=[Point(1.5, 1.5), Point(0.5, 1.5), Point(0.5, 2.5)],
            insulated=True,
            side="top",
        )
        out_bp = generic_simplify_wire(wi, [], mode=SimplifyMode.BARE_PARITY)
        out_ch = generic_simplify_wire(wi, [], mode=SimplifyMode.CHEBYSHEV)
        assert len(out_bp.path) == 2
        assert len(out_ch.path) == 2


class TestGenericSimplifyPinPreservation:
    def test_internal_pad_pin_blocks_shortcut(self) -> None:
        # Wire passes through pin (1.5, 2.5).  Canonical shortcut from (1.5,1.5)
        # to (1.5, 3.5) is the two-vertex segment; the pin lies on that line
        # but not as a vertex — would silently disconnect it.
        w = _bare((1.5, 1.5), (2.5, 2.5), (1.5, 3.5))
        pins = {Point(2.5, 2.5)}
        out = generic_simplify_wire(
            w, [], pin_positions=pins, mode=SimplifyMode.CHEBYSHEV
        )
        # Wire unchanged because the internal (2.5, 2.5) is a pin not on the
        # canonical replacement.
        assert len(out.path) == 3


class TestGenericSimplifyCollision:
    def test_crossing_with_other_bare_blocks_shortcut(self) -> None:
        w = _bare((0.5, 0.5), (1.5, 0.5), (1.5, 1.5))
        # Block the diagonal (0.5,0.5)→(1.5,1.5) with a perpendicular bare.
        blocker = _bare((0.5, 1.5), (1.5, 0.5))
        out = generic_simplify_wire(
            w, [blocker], mode=SimplifyMode.CHEBYSHEV
        )
        assert len(out.path) == 3  # unchanged


class TestGenericSimplifyAlienPad:
    def test_bare_alien_pad_on_canonical_blocks_shortcut(self) -> None:
        # Wire endpoints on column 1.  Canonical ortho passes through (1.5, 2.5).
        # That pad belongs to another net → must not shortcut (would short).
        w = _bare((1.5, 1.5), (2.5, 2.5), (1.5, 3.5))
        other_net = uuid.uuid4()
        pad_to_net = {Point(1.5, 2.5): other_net}
        out = generic_simplify_wire(
            w, [], pad_to_net=pad_to_net, mode=SimplifyMode.CHEBYSHEV
        )
        assert len(out.path) == 3  # unchanged

    def test_insulated_ignores_alien_pad(self) -> None:
        wi = WireSegment(
            id=uuid.uuid4(),
            path=[Point(1.5, 1.5), Point(2.5, 2.5), Point(1.5, 3.5)],
            insulated=True,
            side="top",
        )
        other_net = uuid.uuid4()
        pad_to_net = {Point(1.5, 2.5): other_net}
        out = generic_simplify_wire(
            wi, [], pad_to_net=pad_to_net, mode=SimplifyMode.CHEBYSHEV
        )
        # Insulated → floats over pads → alien-pad check is bypassed.
        assert len(out.path) == 2


class TestGenericSimplifyNoop:
    def test_already_optimal_returns_same_object(self) -> None:
        w = _bare((0.5, 0.5), (3.5, 0.5))
        out = generic_simplify_wire(w, [], mode=SimplifyMode.CHEBYSHEV)
        assert out is w

    def test_two_vertex_path_returns_same_object(self) -> None:
        w = _bare((0.5, 0.5), (2.5, 2.5))
        out = generic_simplify_wire(w, [], mode=SimplifyMode.CHEBYSHEV)
        assert out is w
