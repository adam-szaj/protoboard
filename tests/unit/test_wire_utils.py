# SPDX-License-Identifier: MIT
"""Tests for wire_utils — insert_pad_nodes and board pad enumeration."""

from __future__ import annotations

import pytest

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.layout.wire_utils import (
    board_pads_for_side,
    insert_pad_nodes,
    plated_through_pads,
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
