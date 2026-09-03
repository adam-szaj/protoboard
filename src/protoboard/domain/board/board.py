# SPDX-License-Identifier: MIT
"""Board — wrapper around a box tree with cached layout."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING, cast

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
from protoboard.instrumentation.call_counters import counted
from protoboard.units import DEFAULT_PITCH_MM, Millimeter

if TYPE_CHECKING:
    from protoboard.domain.board.pad_state import Pad
    from protoboard.domain.board.pads import PadLayers


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
        return Rect.of(
            x=node.center.x - node.size.lx / 2.0,
            y=node.center.y - node.size.ly / 2.0,
            lx=node.size.lx,
            ly=node.size.ly,
        )

    @property
    def size_mm(self) -> Size:
        size = Size(
            self.bounds.lx * self.raster_pitch_mm,
            self.bounds.ly * self.raster_pitch_mm,
        )
        # Mark the unused import for typing inspectors that the conversion
        # ultimately produces millimeter-typed values.
        _ = Millimeter  # noqa: F841
        return size

    @counted("Board.atoms")
    def atoms(self) -> Iterator[tuple[Box, Point]]:
        """Yield (atom, world_center_position) pairs in DFS order.

        Result is cached on the board's private dict — the box tree is
        immutable for the session's lifetime, so successive walks
        replay a pre-materialised list instead of recursing through the
        layout tree again.  Profiler put 28s of own time in
        ``_walk_atoms`` before the cache; consumers like
        ``board_pads_for_side``, ``MoveComponentCommand`` pad sanity
        check, and the connectivity graph all called it on every
        operation.
        """
        cached = self._.get("_atoms_list")
        if cached is None:
            return iter(self._build_atoms_list())
        return iter(cast(tuple[tuple[Box, Point], ...], cached))

    def pad_positions(self) -> list[Point]:
        """All pad centres (raster units), in atom-walk order.

        A strip expands to one centre per hole; a plain pad yields its own
        centre; gaps contribute nothing.  This is the single source of truth
        the snap grid is built from — the GUI view's pad cache and the Qt-free
        :meth:`EditController.snap` both call it, so they can never diverge.
        """
        positions: list[Point] = []
        for atom, center in self.atoms():
            if isinstance(atom, StripAtom):
                positions.extend(atom.hole_positions(center, fill_length=0))
            elif not isinstance(atom, GapAtom):
                positions.append(center)
        return positions

    @counted("Board._build_atoms_list")
    def _build_atoms_list(self) -> tuple[tuple[Box, Point], ...]:
        """The cache-miss branch of :meth:`atoms`.

        Split out so the call counter records only the actual
        DFS rebuilds; cache hits return without invoking this
        method.  Production should see exactly one
        ``Board._build_atoms_list`` call per Board instance.
        """
        built = tuple(_walk_atoms(self.layout))
        self._["_atoms_list"] = built
        return built

    def make_pad(
        self,
        position: Point,
        layers: PadLayers,
        strip_group_id: int | None = None,
    ) -> Pad:
        """Construct a Pad of the right subclass for *layers*.

        - ``plated_through=True`` → ``ConnectedDoubleSidedBoardPad`` (top and
          bottom share one electrical node).
        - both ``top`` and ``bottom`` (not plated) →
          ``IsolatedDoubleSidedBoardPad`` (independent sides).
        - one side only → ``SingleSidedBoardPad``.

        The factory is stateless aside from its arguments; ``Board`` does
        not store a board-level "single vs double sided" flag because
        mixed boards are possible.
        """
        from protoboard.domain.board.pad_state import (
            ConnectedDoubleSidedBoardPad,
            IsolatedDoubleSidedBoardPad,
            SingleSidedBoardPad,
        )

        if layers.plated_through:
            return ConnectedDoubleSidedBoardPad(
                position=position,
                layers=layers,
                strip_group_id=strip_group_id,
            )
        if layers.top and layers.bottom:
            return IsolatedDoubleSidedBoardPad(
                position=position,
                layers=layers,
                strip_group_id=strip_group_id,
            )
        return SingleSidedBoardPad(
            position=position,
            layers=layers,
            strip_group_id=strip_group_id,
        )


def _walk_atoms(node: LayoutNode) -> Iterator[tuple[Box, Point]]:
    if isinstance(node.box, (PadAtom, StripAtom, GapAtom)):
        yield node.box, Point(node.center.x, node.center.y)
        return
    if isinstance(node.box, (HBox, VBox, BoardGrid)):
        for child in node.children:
            yield from _walk_atoms(child)
