# SPDX-License-Identifier: MIT
"""Factory functions for common board types."""

from __future__ import annotations

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import (
    BoardGrid,
    PadAtom,
    SizePolicy,
    StripAtom,
    VBox,
)
from protoboard.domain.board.pads import PadLayers


def make_perfboard(rows: int, cols: int) -> Board:
    """Perfboard: regular grid of plated-through holes."""
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


def make_stripboard(rows: int, cols: int) -> Board:
    """Stripboard: horizontal copper strips, each *cols* holes long."""
    strip_layers = PadLayers(top=False, bottom=True, plated_through=False)
    children = [
        StripAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            length=cols,
            orientation="horizontal",
            layers=strip_layers,
        )
        for _ in range(rows)
    ]
    vbox = VBox(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        children=children,  # type: ignore[arg-type]
        gap=1.0,
    )
    return Board(root=vbox)
