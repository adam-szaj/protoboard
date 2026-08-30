# SPDX-License-Identifier: MIT
"""Factory functions for common board types."""

from __future__ import annotations

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import (
    BoardGrid,
    Box,
    GapAtom,
    HBox,
    PadAtom,
    SizePolicy,
    StripAtom,
    VBox,
)
from protoboard.domain.board.pads import PadLayers

_HUG = SizePolicy.HUG
_BOTTOM = PadLayers(top=False, bottom=True, plated_through=False)


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


def make_breadboard(cols: int = 30) -> Board:
    """Full-size solderless breadboard layout.

    Structure (top → bottom):
      power rail +   (horizontal strip, cols holes)
      power rail −   (horizontal strip, cols holes)
      ½-raster gap
      terminal block A: 5 rows × cols columns of vertical 5-hole strips
      2-raster DIP gap
      terminal block B: 5 rows × cols columns of vertical 5-hole strips
      ½-raster gap
      power rail −   (horizontal strip)
      power rail +   (horizontal strip)
    """

    def _power_rail(label: str) -> HBox:
        seg = StripAtom(
            width_policy=_HUG,
            height_policy=_HUG,
            length=cols,
            orientation="horizontal",
            layers=_BOTTOM,
            label=label,
        )
        return HBox(width_policy=_HUG, height_policy=_HUG, children=[seg], gap=0.0)

    def _terminal_block() -> VBox:
        """5 rows of HBoxes, each holding *cols* vertical StripAtoms of length 5."""
        # Each *column* is one StripAtom(vertical, length=5).
        # Arranged as 5 HBoxes of *cols* StripAtoms (row = one hole per column).
        # We model this as a Grid-like VBox where each row is one hole-row,
        # but since StripAtom spans 5 rows we need a different approach:
        # put all *cols* StripAtoms in a single HBox (they each occupy 5 rows).
        atoms: list[Box] = [
            StripAtom(
                width_policy=_HUG,
                height_policy=_HUG,
                length=5,
                orientation="vertical",
                layers=_BOTTOM,
            )
            for _ in range(cols)
        ]
        return VBox(
            width_policy=_HUG,
            height_policy=_HUG,
            children=[
                HBox(width_policy=_HUG, height_policy=_HUG, children=atoms, gap=0.0)
            ],
            gap=0.0,
        )

    def _gap(height: float) -> GapAtom:
        from protoboard.domain.board.boxes import SizePolicy as SP

        return GapAtom(width_policy=SP.HUG, height_policy=SP.FIXED, height=height)

    children: list[HBox | VBox | GapAtom] = [
        _power_rail("+"),
        _power_rail("−"),
        _gap(0.5),
        _terminal_block(),
        _gap(2.0),
        _terminal_block(),
        _gap(0.5),
        _power_rail("−"),
        _power_rail("+"),
    ]
    root = VBox(
        width_policy=_HUG,
        height_policy=_HUG,
        children=list(children),
        gap=0.0,
    )
    return Board(root=root)


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
