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
    "SingleRow",
]
