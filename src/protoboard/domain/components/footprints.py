# SPDX-License-Identifier: MIT
"""Footprint templates — generate pin positions on the raster grid.

Convention: pin 1 lies at (0, 0); other pins have non-negative coordinates
for an unrotated footprint. Rotation is applied later by the Layout layer.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from protoboard.geometry import Point


def _validate_positive(value: float, name: str) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be > 0; got {value}")


def _validate_positive_int(value: int, name: str) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be > 0; got {value}")


class FootprintTemplate(ABC):
    """Abstract base for footprint generators.

    Subclasses implement `pin_positions` (a list of `Point`s, indexed by
    pin number minus one) and `pin_count`.
    """

    @abstractmethod
    def pin_positions(self) -> list[Point]: ...

    @abstractmethod
    def pin_count(self) -> int: ...


@dataclass(frozen=True, slots=True)
class SingleRow(FootprintTemplate):
    """N pins along a single column (e.g. SIL-N, TO-92)."""

    n: int
    pitch: float = 1.0

    def __post_init__(self) -> None:
        _validate_positive_int(self.n, "n")
        _validate_positive(self.pitch, "pitch")

    def pin_count(self) -> int:
        return self.n

    def pin_positions(self) -> list[Point]:
        return [Point(0.0, i * self.pitch) for i in range(self.n)]


@dataclass(frozen=True, slots=True)
class DualRow(FootprintTemplate):
    """Two parallel columns of n pins each (e.g. DIP-N).

    Numbering goes 1..n down the left column, then n+1..2n up the right.
    """

    n_per_row: int
    pitch: float = 1.0
    rows_spacing: float = 3.0

    def __post_init__(self) -> None:
        _validate_positive_int(self.n_per_row, "n_per_row")
        _validate_positive(self.pitch, "pitch")
        _validate_positive(self.rows_spacing, "rows_spacing")

    def pin_count(self) -> int:
        return self.n_per_row * 2

    def pin_positions(self) -> list[Point]:
        left = [Point(0.0, i * self.pitch) for i in range(self.n_per_row)]
        # Right column counts up from the bottom: pin n+1 at (rs, (n-1)*pitch),
        # pin 2n at (rs, 0).
        right = [
            Point(self.rows_spacing, (self.n_per_row - 1 - i) * self.pitch)
            for i in range(self.n_per_row)
        ]
        return left + right


@dataclass(frozen=True, slots=True)
class GridFootprint(FootprintTemplate):
    """rows × cols grid of pins, numbered row-major."""

    rows: int
    cols: int
    pitch: float = 1.0

    def __post_init__(self) -> None:
        _validate_positive_int(self.rows, "rows")
        _validate_positive_int(self.cols, "cols")
        _validate_positive(self.pitch, "pitch")

    def pin_count(self) -> int:
        return self.rows * self.cols

    def pin_positions(self) -> list[Point]:
        return [
            Point(c * self.pitch, r * self.pitch)
            for r in range(self.rows)
            for c in range(self.cols)
        ]


@dataclass(frozen=True, slots=True)
class QuadRow(FootprintTemplate):
    """Four parallel columns of n pins each (QIL / quad in-line).

    Intended for wide development modules that expose two pairs of pin rows
    (outer + inner on each side), such as the WeAct STM32G4 CoreBoard.

    Column layout (left to right):

    ::

        col 0        col 1         col 2          col 3
        (L outer)    (L inner)     (R inner)      (R outer)
        |            |             |              |
        x=0          x=inner_spacing  x=inner_spacing+outer_gap  x=inner_spacing+outer_gap+inner_spacing

    The ``col_spacings`` tuple gives the *gap between adjacent columns* in
    raster units, so for a [1, 8, 1] board:
    ``col_spacings=(1, 8, 1)`` → column x-positions: 0, 1, 9, 10.

    Pin numbering (CW from top-left):

    * Pins ``1 … n``: left-outer column, top → bottom.
    * Pins ``n+1 … 2n``: left-inner column, top → bottom.
    * Pins ``2n+1 … 3n``: right-inner column, bottom → top.
    * Pins ``3n+1 … 4n``: right-outer column, bottom → top.
    """

    n_per_col: int
    col_spacings: tuple[float, float, float] = (1.0, 8.0, 1.0)
    pitch: float = 1.0

    def __post_init__(self) -> None:
        _validate_positive_int(self.n_per_col, "n_per_col")
        _validate_positive(self.pitch, "pitch")
        if len(self.col_spacings) != 3:
            raise ValueError("col_spacings must have exactly 3 values")
        for s in self.col_spacings:
            _validate_positive(s, "col_spacings entry")

    def pin_count(self) -> int:
        return self.n_per_col * 4

    def pin_positions(self) -> list[Point]:
        n = self.n_per_col
        p = self.pitch
        s0, s1, s2 = self.col_spacings
        x0, x1, x2, x3 = 0.0, s0, s0 + s1, s0 + s1 + s2

        left_outer = [Point(x0, i * p) for i in range(n)]
        left_inner = [Point(x1, i * p) for i in range(n)]
        right_inner = [Point(x2, (n - 1 - i) * p) for i in range(n)]
        right_outer = [Point(x3, (n - 1 - i) * p) for i in range(n)]
        return left_outer + left_inner + right_inner + right_outer


# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------


def DIP(n: int, pitch: float = 1.0, rows_spacing: float = 3.0) -> DualRow:
    """DIP-N package (must have an even pin count)."""
    if n <= 0 or n % 2 != 0:
        raise ValueError(f"DIP requires a positive even pin count; got {n}")
    return DualRow(n_per_row=n // 2, pitch=pitch, rows_spacing=rows_spacing)


def SIL(n: int, pitch: float = 1.0) -> SingleRow:
    """SIL-N (single in-line row of N pins)."""
    return SingleRow(n=n, pitch=pitch)


@dataclass(frozen=True, slots=True)
class TO220Footprint(SingleRow):
    """TO-220 package — same 3-pin SIL geometry as TO-92, but distinct type
    so renderers can draw the wider metal-tab body."""


# Mechanical constants — same pin geometry, different package types.
TO92: SingleRow = SIL(3)
TO220: TO220Footprint = TO220Footprint(n=3)


__all__ = [
    "DIP",
    "SIL",
    "TO92",
    "TO220",
    "TO220Footprint",
    "DualRow",
    "FootprintTemplate",
    "GridFootprint",
    "QuadRow",
    "SingleRow",
]
