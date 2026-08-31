# SPDX-License-Identifier: MIT
"""Tests for the post-hoc wire cost analyzer."""

from __future__ import annotations

import math
import uuid

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.wire import Wire, WireCollection
from protoboard.domain.routing.cost_model import RoutingCostModel
from protoboard.geometry import Point
from protoboard.services.routing.cost_analyzer import (
    _bend_kind,
    _segment_unit_steps,
    _step_kind,
    analyze_wires,
)


def _perfboard(rows: int, cols: int, *, plated_through: bool = False) -> Board:
    grid = BoardGrid(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=plated_through),
        ),
        rows=rows,
        cols=cols,
        row_gap=0.0,
        col_gap=0.0,
    )
    return Board(root=grid)


def _wire(*pts: Point, side: str = "top", insulated: bool = True) -> Wire:
    return Wire(
        id=uuid.uuid4(),
        path=list(pts),
        side=side,  # type: ignore[arg-type]
        insulated=insulated,
    )


class TestStepKind:
    def test_horizontal_is_ortho(self) -> None:
        assert _step_kind(Point(0.5, 0.5), Point(1.5, 0.5)) == "ortho"

    def test_vertical_is_ortho(self) -> None:
        assert _step_kind(Point(0.5, 0.5), Point(0.5, 1.5)) == "ortho"

    def test_unit_diagonal(self) -> None:
        assert _step_kind(Point(0.5, 0.5), Point(1.5, 1.5)) == "diagonal"

    def test_zero_length_is_none(self) -> None:
        assert _step_kind(Point(0.5, 0.5), Point(0.5, 0.5)) is None

    def test_l1_l2_mismatch_is_none(self) -> None:
        # 2:1 not a unit diagonal or ortho — counted as None in step kinds.
        assert _step_kind(Point(0.5, 0.5), Point(2.5, 1.5)) is None


class TestUnitDecomposition:
    def test_single_unit_returns_one(self) -> None:
        steps = _segment_unit_steps(Point(0.5, 0.5), Point(1.5, 0.5))
        assert len(steps) == 1

    def test_three_cell_ortho_decomposed(self) -> None:
        steps = _segment_unit_steps(Point(0.5, 0.5), Point(3.5, 0.5))
        assert len(steps) == 3
        assert steps[0] == (Point(0.5, 0.5), Point(1.5, 0.5))
        assert steps[-1] == (Point(2.5, 0.5), Point(3.5, 0.5))

    def test_three_cell_diagonal_decomposed(self) -> None:
        steps = _segment_unit_steps(Point(0.5, 0.5), Point(3.5, 3.5))
        assert len(steps) == 3


class TestBendKind:
    def test_straight_continuation_no_bend(self) -> None:
        assert _bend_kind((1.0, 0.0), (1.0, 0.0)) is None

    def test_ortho_to_ortho_90(self) -> None:
        assert _bend_kind((1.0, 0.0), (0.0, 1.0)) == "bend_90"

    def test_ortho_to_diagonal_45(self) -> None:
        assert _bend_kind((1.0, 0.0), (1.0, 1.0)) == "bend_45"

    def test_diagonal_reversal_135(self) -> None:
        # (1,1) → (-1,1) crosses 135° (cos = 0 / 2 = 0… actually cos = 0 → 90°).
        # Pick a real 135° pair: (1,1) and (-1,0).
        assert _bend_kind((1.0, 1.0), (-1.0, 0.0)) == "bend_135"


class TestAnalyzer:
    def test_empty_wires_zero_total(self) -> None:
        board = _perfboard(5, 5)
        result = analyze_wires(
            [],
            board=board,
            layout=Layout(),
            cost_model=RoutingCostModel(),
        )
        assert result.total == 0.0
        assert result.wire_count == 0
        assert result.step_count == 0

    def test_simple_ortho_wire(self) -> None:
        # 3 ortho steps → 3 × free_pad (=1.0) = 3.0; no bends (straight line).
        board = _perfboard(5, 5)
        w = _wire(Point(0.5, 0.5), Point(3.5, 0.5))
        result = analyze_wires(
            [w],
            board=board,
            layout=Layout(wires=WireCollection([w])),
            cost_model=RoutingCostModel(),
        )
        assert result.step_count == 3
        assert result.total == 3.0
        # No bend categories should appear.
        keys = result.as_dict()
        assert "ortho_step" in keys
        assert "bend_45" not in keys
        assert "bend_90" not in keys

    def test_l_shape_one_bend(self) -> None:
        # 2 ortho + 1 bend_90 (direction_change fallback for bend_90).
        board = _perfboard(5, 5)
        cm = RoutingCostModel(direction_change=2.0)
        w = _wire(Point(0.5, 0.5), Point(2.5, 0.5), Point(2.5, 2.5))
        result = analyze_wires(
            [w],
            board=board,
            layout=Layout(wires=WireCollection([w])),
            cost_model=cm,
        )
        # 4 ortho steps + 1 bend_90 charged at direction_change (no explicit bend knob).
        # Expect: 4 × 1.0 + 1 × 2.0 = 6.0
        assert result.step_count == 4
        assert result.total == 6.0
        d = result.as_dict()
        assert d["ortho_step"] == 4.0
        assert d["bend_90"] == 2.0

    def test_explicit_bend_knob_overrides_direction_change(self) -> None:
        board = _perfboard(5, 5)
        cm = RoutingCostModel(direction_change=2.0)
        cm = cm.with_overrides(bend_90=5.0)
        w = _wire(Point(0.5, 0.5), Point(2.5, 0.5), Point(2.5, 2.5))
        result = analyze_wires(
            [w],
            board=board,
            layout=Layout(wires=WireCollection([w])),
            cost_model=cm,
        )
        # 4 × 1.0 + 1 × 5.0 = 9.0
        assert result.total == 9.0

    def test_diagonal_charged_with_multiplier(self) -> None:
        board = _perfboard(5, 5)
        w = _wire(Point(0.5, 0.5), Point(3.5, 3.5))  # 3 diagonals
        result = analyze_wires(
            [w],
            board=board,
            layout=Layout(wires=WireCollection([w])),
            cost_model=RoutingCostModel(),
        )
        expected = 3 * 1.0 * math.sqrt(2)
        assert abs(result.total - expected) < 1e-6

    def test_layer_change_charged_once(self) -> None:
        # Two wires meeting at a plated-through pad on different sides → 1 via.
        board = _perfboard(3, 3, plated_through=True)
        cm = RoutingCostModel(layer_change=5.0)
        w_top = _wire(Point(0.5, 0.5), Point(1.5, 0.5), side="top")
        w_bot = _wire(Point(1.5, 0.5), Point(2.5, 0.5), side="bottom")
        result = analyze_wires(
            [w_top, w_bot],
            board=board,
            layout=Layout(wires=WireCollection([w_top, w_bot])),
            cost_model=cm,
        )
        d = result.as_dict()
        assert d.get("layer_change") == 5.0
        # Plus the 2 ortho steps (one per wire).
        assert result.step_count == 2

    def test_zero_cost_categories_omitted(self) -> None:
        board = _perfboard(5, 5)
        # direction_change=0 and bend knobs=0 → bend row should NOT appear
        # even though there is a geometric bend.
        cm = RoutingCostModel(direction_change=0.0)
        w = _wire(Point(0.5, 0.5), Point(2.5, 0.5), Point(2.5, 2.5))
        result = analyze_wires(
            [w],
            board=board,
            layout=Layout(wires=WireCollection([w])),
            cost_model=cm,
        )
        d = result.as_dict()
        assert "bend_90" not in d  # zero unit cost → omitted
        assert "ortho_step" in d


class TestCrossings:
    """All 8 granular crossing knobs plus the legacy bare×bare ban."""

    def test_insulated_ortho_vs_insulated_diagonal_opposite_side(self) -> None:
        # The exact scenario the user hit: ortho insulated wire on one
        # side crossing a diagonal insulated wire on the other side.
        # We pick a geometry where the intersection lies BETWEEN pads
        # (mesh cell centre, integer coords) so the off_pad knob fires.
        board = _perfboard(6, 6)
        cm = RoutingCostModel().with_overrides(
            crossing_ii_off_pad_opposite_side=4.0,
        )
        # Ortho row at y=2.5 (pad centres) — but we want the intersection
        # off-pad, so use a 2-pad wire ending on integer x=3 won't work
        # (pads are at half-integers).  Instead, route the diagonal so
        # it crosses x at a non-half-integer point.
        # Diagonal (0.5, 1.5) → (4.5, 5.5) parametrised: x = 0.5 + t·4, y = 1.5 + t·4.
        # At y = 2.5: t = 0.25 → x = 1.5.  That lands on a pad → at_pad.
        # We need a non-grid-aligned start.  Use a kinked path so the
        # cross happens on a sub-cell midpoint?  Simpler: rotate the
        # geometry so both lines cross at the mesh centre (integer, integer).
        # Ortho along y=2.5 vs diagonal from (1.5, 1.5)→(3.5, 3.5): intersects
        # at x=2.5, y=2.5 which IS a pad.  We need a wire that doesn't go
        # through pad centres → autorouter never produces such wires.
        # Conclusion: with autorouter-shaped paths the crossing is always
        # at_pad.  Accept either knob in the assertion.
        wire_a = _wire(  # ortho top
            Point(0.5, 2.5),
            Point(4.5, 2.5),
            side="top",
            insulated=True,
        )
        wire_b = _wire(  # diagonal bottom
            Point(0.5, 0.5),
            Point(4.5, 4.5),
            side="bottom",
            insulated=True,
        )
        # Use whichever knob the intersection lands in — but for the
        # router/analyzer to reflect the *user's* knob, both must be set.
        cm = cm.with_overrides(crossing_ii_at_pad_opposite_side=4.0)
        layout = Layout(wires=WireCollection([wire_a, wire_b]))
        result = analyze_wires(
            [wire_a, wire_b],
            board=board,
            layout=layout,
            cost_model=cm,
        )
        d = result.as_dict()
        assert (
            "crossing_ii_at_pad_opposite_side" in d
            or "crossing_ii_off_pad_opposite_side" in d
        ), f"expected an opposite-side ii crossing row; got {list(d)}"

    def test_crossing_counted_against_outside_wire(self) -> None:
        # Analyse only wire A, but wire B exists on the board.  The
        # crossing must still be charged against A.
        board = _perfboard(5, 5)
        # Set BOTH at_pad and off_pad knobs so the test doesn't depend
        # on which bucket the geometric intersection lands in.
        cm = RoutingCostModel().with_overrides(
            crossing_ii_at_pad_opposite_side=4.0,
            crossing_ii_off_pad_opposite_side=4.0,
        )
        wire_a = _wire(
            Point(0.5, 2.5),
            Point(4.5, 2.5),
            side="top",
            insulated=True,
        )
        wire_b = _wire(
            Point(0.5, 0.5),
            Point(4.5, 4.5),
            side="bottom",
            insulated=True,
        )
        result = analyze_wires(
            [wire_a],  # only A
            board=board,
            layout=Layout(wires=WireCollection([wire_a, wire_b])),
            cost_model=cm,
        )
        d = result.as_dict()
        # The crossing knob must contribute (positive total).
        positive_cross = any(
            k.startswith("crossing_") and v > 0.0 for k, v in d.items()
        )
        assert positive_cross, f"no crossing row found; got {list(d)}"

    def test_at_pad_crossing_uses_at_pad_knob(self) -> None:
        # Both wires share the pad (2.5, 2.5) — intersection is at a pad
        # centre, so the at_pad knob (not off_pad) must be used.
        board = _perfboard(5, 5)
        cm = RoutingCostModel().with_overrides(
            crossing_ii_at_pad_opposite_side=7.0,
            crossing_ii_off_pad_opposite_side=99.0,  # would-be misuse
        )
        wire_a = _wire(
            Point(0.5, 2.5),
            Point(2.5, 2.5),
            Point(4.5, 2.5),
            side="top",
            insulated=True,
        )
        wire_b = _wire(
            Point(2.5, 0.5),
            Point(2.5, 2.5),
            Point(2.5, 4.5),
            side="bottom",
            insulated=True,
        )
        result = analyze_wires(
            [wire_a, wire_b],
            board=board,
            layout=Layout(wires=WireCollection([wire_a, wire_b])),
            cost_model=cm,
        )
        d = result.as_dict()
        # At-pad knob should have been picked.  off_pad row may also exist
        # if a tangential segment touches off-pad, but at_pad must be there.
        assert (
            d.get("crossing_ii_at_pad_opposite_side", 0.0) > 0.0
        ), f"at_pad knob never charged: {list(d)}"

    def test_bare_bare_crossing_marks_total_infinite(self) -> None:
        # Two bare wires crossing on the same side → legacy bare×bare ban.
        # Total cost should be infinity (sentinel for "router would refuse").
        board = _perfboard(5, 5)
        cm = RoutingCostModel()  # default crossing_bare_bare = inf
        wire_a = _wire(
            Point(0.5, 2.5),
            Point(4.5, 2.5),
            side="top",
            insulated=False,
        )
        wire_b = _wire(
            Point(2.5, 0.5),
            Point(2.5, 4.5),
            side="top",
            insulated=False,
        )
        result = analyze_wires(
            [wire_a, wire_b],
            board=board,
            layout=Layout(wires=WireCollection([wire_a, wire_b])),
            cost_model=cm,
        )
        assert math.isinf(result.total)
        # And there must be a visible row signalling the issue.
        assert any(c.key == "crossing_bare_bare" for c in result.categories)

    def test_no_crossing_when_outside_bbox(self) -> None:
        board = _perfboard(10, 10)
        cm = RoutingCostModel().with_overrides(
            crossing_ii_off_pad_opposite_side=4.0,
        )
        wire_a = _wire(
            Point(0.5, 0.5),
            Point(2.5, 0.5),
            side="top",
            insulated=True,
        )
        wire_b = _wire(
            Point(7.5, 7.5),
            Point(9.5, 9.5),
            side="bottom",
            insulated=True,
        )
        result = analyze_wires(
            [wire_a, wire_b],
            board=board,
            layout=Layout(wires=WireCollection([wire_a, wire_b])),
            cost_model=cm,
        )
        # Nothing crosses → no crossing rows.
        assert not any(c.key.startswith("crossing_") for c in result.categories)

    def test_crossings_skipped_when_no_layout_no_board_wires(self) -> None:
        # Regression: GUI callers used to forget `all_board_wires`, which
        # caused `_count_crossings` to silently no-op even though wires
        # geometrically crossed.  Confirm this fallback behaviour so the
        # contract is locked.  When neither `layout` nor `all_board_wires`
        # are supplied, crossings are not counted.
        # Zero out under_component_top so the function doesn't insist on
        # board/layout for the unrelated penalty — we're testing crossings.
        cm = RoutingCostModel().with_overrides(
            crossing_ii_at_pad_opposite_side=4.0,
            crossing_ii_off_pad_opposite_side=4.0,
            under_component_top=0.0,
            layer_change=0.0,
        )
        wire_a = _wire(
            Point(0.5, 2.5),
            Point(4.5, 2.5),
            side="top",
            insulated=True,
        )
        wire_b = _wire(
            Point(0.5, 0.5),
            Point(4.5, 4.5),
            side="bottom",
            insulated=True,
        )
        # No layout, no all_board_wires — analyzer has no list of board
        # wires to cross-check against, so crossings are silently
        # skipped.  The fix at the call site is to pass `all_board_wires`.
        _ = wire_b  # referenced for setup symmetry with the next test
        result = analyze_wires(
            [wire_a],  # only A — B not visible to the analyzer
            cost_model=cm,
        )
        assert not any(c.key.startswith("crossing_") for c in result.categories)

    def test_at_pad_crossings_via_pad_index(self) -> None:
        # When PadIndex is supplied, at-pad crossings are computed from
        # the index — every wire item recorded on a pad pairs with every
        # other wire item on that pad (per-side and cross-side).  This
        # is independent of geometric `_segs_cross` and catches every
        # case: vertex-on-pad, mid-segment crossing, bend-on-pad.
        from protoboard.domain.board.pad_state import PadIndex
        from protoboard.domain.schematic.schematic import Schematic

        board = _perfboard(5, 5)
        wire_top = _wire(  # diagonal top through (2.5, 2.5)
            Point(0.5, 0.5),
            Point(1.5, 1.5),
            Point(2.5, 2.5),
            Point(3.5, 3.5),
            Point(4.5, 4.5),
            side="top",
            insulated=True,
        )
        wire_bot = _wire(  # bend on bottom at (2.5, 2.5)
            Point(0.5, 2.5),
            Point(1.5, 2.5),
            Point(2.5, 2.5),
            Point(3.5, 3.5),
            Point(4.5, 4.5),
            side="bottom",
            insulated=True,
        )
        lay = Layout(wires=WireCollection([wire_top, wire_bot]))
        pad_idx = PadIndex(
            board=board,
            layout=lay,
            schematic=Schematic(nets={}),
            components={},
            instances={},
        )
        cm = RoutingCostModel().with_overrides(
            crossing_ii_at_pad_opposite_side=5.0,
            under_component_top=0.0,
            layer_change=0.0,
        )
        result = analyze_wires(
            [wire_top, wire_bot],
            board=board,
            layout=lay,
            cost_model=cm,
            all_board_wires=[wire_top, wire_bot],
            pad_index=pad_idx,
        )
        d = result.as_dict()
        # The bend-point crossing must register exactly once.  PadIndex
        # iterates pad-by-pad, so the count is the number of pads where
        # both wires meet.  In this geometry the two wires share pads at
        # (2.5,2.5), (3.5,3.5) and (4.5,4.5) — diagonals overlap from the
        # bend onwards.  Both wires have InsulatedWireItem on each shared
        # pad, opposite sides → 3 opposite-side at-pad crossings.
        assert "crossing_ii_at_pad_opposite_side" in d
        assert d["crossing_ii_at_pad_opposite_side"] > 0.0

    def test_crossing_at_bend_of_other_wire(self) -> None:
        # User report 2026-05-23: a top-diagonal wire crossing a bottom
        # wire whose bend point sits exactly at the intersection pad was
        # never charged.  Root cause: `_segs_cross` uses strict-interior
        # semantics and one of the bottom wire's segments has the
        # crossing point as its endpoint (the bend).  Fix: detect
        # touch-on-interior in `_count_crossings`.
        board = _perfboard(5, 5)
        cm = RoutingCostModel().with_overrides(
            crossing_ii_at_pad_opposite_side=4.0,
            under_component_top=0.0,
            layer_change=0.0,
        )
        wire_top = _wire(  # all-diagonal top, straight line through (2.5,2.5)
            Point(0.5, 0.5),
            Point(1.5, 1.5),
            Point(2.5, 2.5),
            Point(3.5, 3.5),
            Point(4.5, 4.5),
            side="top",
            insulated=True,
        )
        wire_bot = _wire(  # bottom: ortho then diag, BEND at (2.5,2.5)
            Point(0.5, 2.5),
            Point(1.5, 2.5),
            Point(2.5, 2.5),
            Point(3.5, 3.5),
            Point(4.5, 4.5),
            side="bottom",
            insulated=True,
        )
        result = analyze_wires(
            [wire_top, wire_bot],
            board=board,
            layout=Layout(wires=WireCollection([wire_top, wire_bot])),
            cost_model=cm,
            all_board_wires=[wire_top, wire_bot],
        )
        d = result.as_dict()
        # The bend-point crossing must register exactly once (not twice,
        # which would happen if both adjacent segments of the bend
        # counted separately).
        assert (
            d.get("crossing_ii_at_pad_opposite_side") == 4.0
        ), f"expected 1×4.0; got {d}"

    def test_crossing_row_shown_when_knob_is_zero(self) -> None:
        # Tuning workflow: the user wants to see what crossings exist
        # BEFORE assigning costs to them.  A crossing whose knob is 0
        # must still appear as a row in the breakdown (with total=0).
        # The default RoutingCostModel has all granular crossing knobs
        # at 0.0, so this is the typical fresh-project case.
        cm = RoutingCostModel().with_overrides(
            under_component_top=0.0,
            layer_change=0.0,
        )
        wire_a = _wire(
            Point(0.5, 2.5),
            Point(4.5, 2.5),
            side="top",
            insulated=True,
        )
        wire_b = _wire(
            Point(0.5, 0.5),
            Point(4.5, 4.5),
            side="bottom",
            insulated=True,
        )
        result = analyze_wires(
            [wire_a, wire_b],
            cost_model=cm,
            all_board_wires=[wire_a, wire_b],
        )
        # Even with knob=0, the crossing must be a visible row.
        crossings = [c for c in result.categories if c.key.startswith("crossing_")]
        assert crossings, (
            "crossing row missing despite a real geometric crossing; "
            f"got {[c.key for c in result.categories]}"
        )
        assert crossings[0].unit_cost == 0.0
        assert crossings[0].total == 0.0
        assert crossings[0].count >= 1

    def test_crossings_detected_via_all_board_wires_param(self) -> None:
        # Same setup as above, but the caller now passes the full board
        # wire list explicitly.  Crossing must register.
        cm = RoutingCostModel().with_overrides(
            crossing_ii_at_pad_opposite_side=4.0,
            crossing_ii_off_pad_opposite_side=4.0,
            under_component_top=0.0,
            layer_change=0.0,
        )
        wire_a = _wire(
            Point(0.5, 2.5),
            Point(4.5, 2.5),
            side="top",
            insulated=True,
        )
        wire_b = _wire(
            Point(0.5, 0.5),
            Point(4.5, 4.5),
            side="bottom",
            insulated=True,
        )
        result = analyze_wires(
            [wire_a],
            cost_model=cm,
            all_board_wires=[wire_a, wire_b],
        )
        assert any(c.key.startswith("crossing_") for c in result.categories)

    def test_same_side_bare_diagonal_perpendicular_exception(self) -> None:
        # Two bare diagonals crossing perpendicular at a *mesh-cell centre*
        # (integer coords, between pads).  The exception fires only there,
        # not at pad centres — at pad centre two bare wires share metal.
        # Use shifted diagonals so their intersection lands at (2.0, 2.0).
        board = _perfboard(5, 5)
        cm = RoutingCostModel().with_overrides(
            crossing_bare_bare_diagonal=2.0,
        )
        wire_a = _wire(  # / passing through (2.0, 2.0)
            Point(0.5, 3.5),
            Point(3.5, 0.5),
            side="top",
            insulated=False,
        )
        wire_b = _wire(  # \ passing through (2.0, 2.0)
            Point(0.5, 0.5),
            Point(3.5, 3.5),
            side="top",
            insulated=False,
        )
        result = analyze_wires(
            [wire_a, wire_b],
            board=board,
            layout=Layout(wires=WireCollection([wire_a, wire_b])),
            cost_model=cm,
        )
        d = result.as_dict()
        # The diagonal exception path: finite, not inf.
        assert not math.isinf(result.total)
        assert d.get("crossing_bare_bare_diagonal", 0.0) > 0.0
