# SPDX-License-Identifier: MIT
"""Tests for wire_utils — insert_pad_nodes and board pad enumeration."""

from __future__ import annotations

import pytest

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.layout.wire import WireSegment
from protoboard.domain.layout.wire_utils import (
    board_pads_for_side,
    find_skewed_wires,
    insert_pad_nodes,
    plated_through_pads,
    wire_has_skewed_segment,
)
from protoboard.geometry import Point


def _perfboard(rows: int, cols: int) -> Board:
    grid = BoardGrid(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        ),
        rows=rows,
        cols=cols,
        row_gap=0.0,
        col_gap=0.0,
    )
    return Board(root=grid)


class TestInsertPadNodes:
    def test_no_intermediate_pads(self) -> None:
        path = [Point(0.5, 0.5), Point(2.5, 0.5)]
        pads: set[Point] = set()
        assert insert_pad_nodes(path, pads) == path

    def test_single_interior_pad_horizontal(self) -> None:
        path = [Point(0.5, 0.5), Point(4.5, 0.5)]
        pads = {Point(2.5, 0.5)}
        result = insert_pad_nodes(path, pads)
        assert result == [Point(0.5, 0.5), Point(2.5, 0.5), Point(4.5, 0.5)]

    def test_multiple_interior_pads_ordered(self) -> None:
        path = [Point(0.5, 0.5), Point(4.5, 0.5)]
        pads = {Point(3.5, 0.5), Point(1.5, 0.5), Point(2.5, 0.5)}
        result = insert_pad_nodes(path, pads)
        assert result == [
            Point(0.5, 0.5), Point(1.5, 0.5), Point(2.5, 0.5),
            Point(3.5, 0.5), Point(4.5, 0.5),
        ]

    def test_vertical_segment(self) -> None:
        path = [Point(0.5, 0.5), Point(0.5, 3.5)]
        pads = {Point(0.5, 1.5), Point(0.5, 2.5)}
        result = insert_pad_nodes(path, pads)
        assert result == [
            Point(0.5, 0.5), Point(0.5, 1.5), Point(0.5, 2.5), Point(0.5, 3.5),
        ]

    def test_diagonal_segment(self) -> None:
        path = [Point(0.5, 0.5), Point(3.5, 3.5)]
        pads = {Point(1.5, 1.5), Point(2.5, 2.5)}
        result = insert_pad_nodes(path, pads)
        assert result == [
            Point(0.5, 0.5), Point(1.5, 1.5), Point(2.5, 2.5), Point(3.5, 3.5),
        ]

    def test_pad_at_endpoint_not_duplicated(self) -> None:
        path = [Point(0.5, 0.5), Point(2.5, 0.5)]
        pads = {Point(0.5, 0.5), Point(2.5, 0.5)}
        result = insert_pad_nodes(path, pads)
        assert result == [Point(0.5, 0.5), Point(2.5, 0.5)]

    def test_off_axis_pad_not_inserted(self) -> None:
        path = [Point(0.5, 0.5), Point(4.5, 0.5)]
        pads = {Point(2.5, 1.5)}  # Off the line
        result = insert_pad_nodes(path, pads)
        assert result == [Point(0.5, 0.5), Point(4.5, 0.5)]

    def test_multi_segment_path(self) -> None:
        path = [Point(0.5, 0.5), Point(4.5, 0.5), Point(4.5, 3.5)]
        pads = {Point(2.5, 0.5), Point(4.5, 1.5), Point(4.5, 2.5)}
        result = insert_pad_nodes(path, pads)
        assert result == [
            Point(0.5, 0.5), Point(2.5, 0.5), Point(4.5, 0.5),
            Point(4.5, 1.5), Point(4.5, 2.5), Point(4.5, 3.5),
        ]

    def test_single_point_path_unchanged(self) -> None:
        path = [Point(0.5, 0.5)]
        result = insert_pad_nodes(path, {Point(0.5, 0.5)})
        assert result == [Point(0.5, 0.5)]

    def test_empty_path_unchanged(self) -> None:
        result = insert_pad_nodes([], set())
        assert result == []

    def test_pad_already_in_path_not_duplicated(self) -> None:
        path = [Point(0.5, 0.5), Point(2.5, 0.5), Point(4.5, 0.5)]
        pads = {Point(2.5, 0.5)}
        result = insert_pad_nodes(path, pads)
        assert result == path  # unchanged — pad already a node


class TestBoardPadsForSide:
    def test_perfboard_top_pads(self) -> None:
        board = _perfboard(3, 3)
        pads = board_pads_for_side(board, "top")
        assert len(pads) == 9
        assert Point(0.5, 0.5) in pads
        assert Point(2.5, 2.5) in pads

    def test_perfboard_bottom_pads(self) -> None:
        board = _perfboard(3, 3)
        pads = board_pads_for_side(board, "bottom")
        assert len(pads) == 9

    def test_plated_through_pads(self) -> None:
        board = _perfboard(2, 2)
        pads = plated_through_pads(board)
        assert len(pads) == 4
        assert Point(0.5, 0.5) in pads


def _wire(path: list[Point]) -> WireSegment:
    import uuid as _uuid
    return WireSegment(id=_uuid.uuid4(), path=path, insulated=True, side="top")


class TestWireHasSkewedSegment:
    def test_orthogonal_horizontal_clear(self) -> None:
        w = _wire([Point(0.5, 0.5), Point(3.5, 0.5)])
        assert wire_has_skewed_segment(w) is False

    def test_orthogonal_vertical_clear(self) -> None:
        w = _wire([Point(0.5, 0.5), Point(0.5, 4.5)])
        assert wire_has_skewed_segment(w) is False

    def test_diagonal_45_clear(self) -> None:
        w = _wire([Point(0.5, 0.5), Point(3.5, 3.5)])
        assert wire_has_skewed_segment(w) is False

    def test_diagonal_negative_slope_clear(self) -> None:
        w = _wire([Point(0.5, 3.5), Point(3.5, 0.5)])
        assert wire_has_skewed_segment(w) is False

    def test_multi_segment_orthogonal_diagonal_mix_clear(self) -> None:
        # L-shape with diagonal continuation: ortho → diag is fine
        w = _wire([Point(0.5, 0.5), Point(0.5, 2.5), Point(3.5, 5.5)])
        assert wire_has_skewed_segment(w) is False

    def test_near_45_within_rounding_tol_clear(self) -> None:
        # dx = 3.0, dy = 3.0 + 1e-9 → within tol, considered diagonal
        w = _wire([Point(0.5, 0.5), Point(3.5, 3.5 + 1e-9)])
        assert wire_has_skewed_segment(w) is False

    def test_skewed_2_to_3_detected(self) -> None:
        # Classic post-drag artifact: dx=2, dy=3
        w = _wire([Point(0.5, 0.5), Point(2.5, 3.5)])
        assert wire_has_skewed_segment(w) is True

    def test_skewed_fractional_detected(self) -> None:
        w = _wire([Point(0.5, 0.5), Point(2.5, 1.5)])
        assert wire_has_skewed_segment(w) is True

    def test_skewed_in_middle_of_path_detected(self) -> None:
        # First segment ortho, second segment skewed
        w = _wire([Point(0.5, 0.5), Point(0.5, 2.5), Point(2.5, 5.5)])
        assert wire_has_skewed_segment(w) is True

    def test_zero_length_segment_ignored(self) -> None:
        # Duplicate point — zero-length segment, neither skewed nor ortho-required
        w = _wire([Point(0.5, 0.5), Point(0.5, 0.5 + 1e-12), Point(3.5, 0.5)])
        assert wire_has_skewed_segment(w) is False


class TestFindSkewedWires:
    def test_filters_by_moved_ids(self) -> None:
        good = _wire([Point(0.5, 0.5), Point(3.5, 0.5)])
        bad = _wire([Point(0.5, 0.5), Point(2.5, 3.5)])
        # bad wire skewed but not in moved set
        ids = find_skewed_wires([good, bad], moved_wire_ids={good.id})
        assert ids == []

    def test_returns_only_skewed_within_moved_set(self) -> None:
        good = _wire([Point(0.5, 0.5), Point(3.5, 0.5)])
        bad = _wire([Point(0.5, 0.5), Point(2.5, 3.5)])
        ids = find_skewed_wires([good, bad], moved_wire_ids={good.id, bad.id})
        assert ids == [bad.id]

    def test_none_filter_inspects_all(self) -> None:
        good = _wire([Point(0.5, 0.5), Point(3.5, 0.5)])
        bad = _wire([Point(0.5, 0.5), Point(2.5, 3.5)])
        ids = find_skewed_wires([good, bad], moved_wire_ids=None)
        assert ids == [bad.id]
