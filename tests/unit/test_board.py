# SPDX-License-Identifier: MIT
"""Tests for Board — wrapper around a box tree with cached layout."""

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import (
    BoardGrid,
    PadAtom,
    SizePolicy,
)
from protoboard.domain.board.pads import PadLayers
from protoboard.geometry import Rect


def _pad() -> PadAtom:
    return PadAtom(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        layers=PadLayers(),
    )


def _make_perfboard_10x10() -> Board:
    grid = BoardGrid(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        cell=_pad(),
        rows=10,
        cols=10,
        row_gap=0.0,
        col_gap=0.0,
    )
    return Board(root=grid)


class TestBoardConstruction:
    def test_default_pitch_is_2_54(self) -> None:
        board = _make_perfboard_10x10()
        assert board.raster_pitch_mm == 2.54

    def test_custom_pitch(self) -> None:
        grid = BoardGrid(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            cell=_pad(),
            rows=2,
            cols=2,
        )
        board = Board(root=grid, raster_pitch_mm=2.0)
        assert board.raster_pitch_mm == 2.0


class TestBoardLayoutCache:
    def test_layout_is_cached(self) -> None:
        board = _make_perfboard_10x10()
        first = board.layout
        second = board.layout
        assert first is second  # cached → same object identity


class TestBoardAtoms:
    def test_atoms_returns_all_atoms(self) -> None:
        board = _make_perfboard_10x10()
        atoms = list(board.atoms())
        assert len(atoms) == 100

    def test_atoms_in_dfs_order(self) -> None:
        board = _make_perfboard_10x10()
        atoms = list(board.atoms())
        # First atom: top-left, last atom: bottom-right.
        first_atom, first_pos = atoms[0]
        last_atom, last_pos = atoms[-1]
        assert isinstance(first_atom, PadAtom)
        assert isinstance(last_atom, PadAtom)
        assert (first_pos.x, first_pos.y) == (0.5, 0.5)
        assert (last_pos.x, last_pos.y) == (9.5, 9.5)


class TestBoardBounds:
    def test_perfboard_10x10_bounds(self) -> None:
        board = _make_perfboard_10x10()
        assert board.bounds == Rect(x=0.0, y=0.0, width=10.0, height=10.0)


class TestBoardSizeMm:
    def test_size_mm_uses_pitch(self) -> None:
        board = _make_perfboard_10x10()
        size = board.size_mm
        # 10 raster × 2.54 mm/raster = 25.4 mm
        assert abs(float(size.width) - 25.4) < 1e-9
        assert abs(float(size.height) - 25.4) < 1e-9

    def test_size_mm_with_custom_pitch(self) -> None:
        grid = BoardGrid(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            cell=_pad(),
            rows=10,
            cols=10,
            row_gap=0.0,
            col_gap=0.0,
        )
        board = Board(root=grid, raster_pitch_mm=2.0)
        size = board.size_mm
        assert abs(float(size.width) - 20.0) < 1e-9
