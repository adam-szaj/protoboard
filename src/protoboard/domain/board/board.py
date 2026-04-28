# SPDX-License-Identifier: MIT
"""Board — wrapper around a box tree with cached layout."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import cached_property

from protoboard.domain.board.boxes import (
    BoardGrid,
    Box,
    GapAtom,
    HBox,
    PadAtom,
    StripAtom,
    VBox,
)
from protoboard.domain.board.layout_engine import LayoutNode, layout
from protoboard.geometry import Point, Rect, Size
from protoboard.units import DEFAULT_PITCH_MM, Millimeter


@dataclass
class Board:
    """A physical board: a box tree plus a raster pitch in millimeters."""

    root: Box
    raster_pitch_mm: float = DEFAULT_PITCH_MM
    _: dict[str, object] = field(default_factory=dict, repr=False, compare=False)

    @cached_property
    def layout(self) -> LayoutNode:
        return layout(self.root)

    @property
    def bounds(self) -> Rect:
        node = self.layout
        return Rect(
            x=node.x - node.width / 2.0,
            y=node.y - node.height / 2.0,
            width=node.width,
            height=node.height,
        )

    @property
    def size_mm(self) -> Size:
        size = Size(
            self.bounds.width * self.raster_pitch_mm,
            self.bounds.height * self.raster_pitch_mm,
        )
        # Mark the unused import for typing inspectors that the conversion
        # ultimately produces millimeter-typed values.
        _ = Millimeter  # noqa: F841
        return size

    def atoms(self) -> Iterator[tuple[Box, Point]]:
        """Yield (atom, world_center_position) pairs in DFS order."""
        yield from _walk_atoms(self.layout)


def _walk_atoms(node: LayoutNode) -> Iterator[tuple[Box, Point]]:
    if isinstance(node.box, (PadAtom, StripAtom, GapAtom)):
        yield node.box, Point(node.x, node.y)
        return
    if isinstance(node.box, (HBox, VBox, BoardGrid)):
        for child in node.children:
            yield from _walk_atoms(child)
