# SPDX-License-Identifier: MIT
"""Factory functions for common board types."""

from __future__ import annotations

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import (
    BoardGrid,
    HBox,
    PadAtom,
    SizePolicy,
    StripAtom,
    VBox,
)
from protoboard.domain.board.pads import PadLayers

_HUG = SizePolicy.HUG


def make_perfboard(rows: int, cols: int) -> Board:
    """Perfboard: regular grid of plated-through holes."""
    grid = BoardGrid(
        width_policy=_HUG,
        height_policy=_HUG,
        cell=PadAtom(
            width_policy=_HUG,
            height_policy=_HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        ),
        rows=rows,
        cols=cols,
        row_gap=0.0,
        col_gap=0.0,
    )
    return Board(root=grid)


def _strip_row(cols: int) -> HBox:
    """One horizontal strip row: HBox(gap=0) with a single StripAtom segment."""
    segment = StripAtom(
        width_policy=_HUG,
        height_policy=_HUG,
        length=cols,
        orientation="horizontal",
        layers=PadLayers(top=False, bottom=True, plated_through=False),
    )
    return HBox(
        width_policy=_HUG,
        height_policy=_HUG,
        children=[segment],
        gap=0.0,
    )


def make_stripboard(rows: int, cols: int) -> Board:
    """Stripboard: horizontal copper strips, each *cols* holes long.

    Each row is an ``HBox(gap=0)`` containing one or more ``StripAtom``
    segments. Cuts split a segment into two (or three when cut on a pad),
    all within the same ``HBox``.
    """
    row_boxes: list[HBox] = [_strip_row(cols) for _ in range(rows)]
    vbox = VBox(
        width_policy=_HUG,
        height_policy=_HUG,
        children=list(row_boxes),
        gap=0.0,
    )
    return Board(root=vbox)
