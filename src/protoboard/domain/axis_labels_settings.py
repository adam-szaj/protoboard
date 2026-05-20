# SPDX-License-Identifier: MIT
"""Per-project configuration of column/row index labels rendered around the board.

The two axes (columns and rows) are configured independently.  Each axis
picks one of four formats:

- ``DISABLED``   — that axis renders nothing
- ``NUMERIC_0``  — 0, 1, 2, …
- ``NUMERIC_1``  — 1, 2, 3, …
- ``ALPHABETIC`` — a, b, …, z, a, b, …  (cycles every 26 positions)

Layer visibility (FULL / GHOST / HIDDEN) is handled separately by
``LayerOverrides.axis_labels`` — this dataclass only carries the *format*
choice, not whether anything is drawn.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class AxisLabelFormat(str, Enum):
    DISABLED   = "disabled"
    NUMERIC_0  = "numeric_0"
    NUMERIC_1  = "numeric_1"
    ALPHABETIC = "alphabetic"


@dataclass(frozen=True)
class AxisLabelSettings:
    """Independent format choice for column and row index labels."""

    columns: AxisLabelFormat = AxisLabelFormat.NUMERIC_1
    rows: AxisLabelFormat = AxisLabelFormat.ALPHABETIC

    def any_enabled(self) -> bool:
        return (
            self.columns != AxisLabelFormat.DISABLED
            or self.rows != AxisLabelFormat.DISABLED
        )


def format_index(fmt: AxisLabelFormat, i: int) -> str:
    """Return the label string for 0-based position *i* under *fmt*.

    Returns an empty string when *fmt* is :attr:`AxisLabelFormat.DISABLED`.
    """
    if fmt == AxisLabelFormat.DISABLED:
        return ""
    if fmt == AxisLabelFormat.NUMERIC_0:
        return str(i)
    if fmt == AxisLabelFormat.NUMERIC_1:
        return str(i + 1)
    if fmt == AxisLabelFormat.ALPHABETIC:
        return _alphabetic(i)
    raise ValueError(f"Unknown AxisLabelFormat: {fmt!r}")


def _alphabetic(i: int) -> str:
    """Single-letter cyclic alphabetic index: 0→a, 25→z, 26→a, 27→b, …

    Boards rarely exceed 26 rows in practice, so a simple modulo cycle is
    more readable than Excel-style ``aa``/``ab`` growth — the labels are
    intended as a positional hint, not a unique identifier.
    """
    if i < 0:
        raise ValueError(f"axis index must be non-negative; got {i}")
    return chr(ord("a") + i % 26)


__all__ = ["AxisLabelFormat", "AxisLabelSettings", "format_index"]
