# SPDX-License-Identifier: MIT
"""Per-project configuration of column/row index labels rendered around the board.

The two axes (columns and rows) are configured independently.  Each axis
picks one of four formats:

- ``DISABLED``   — that axis renders nothing
- ``NUMERIC_0``  — 0, 1, 2, …
- ``NUMERIC_1``  — 1, 2, 3, …
- ``ALPHABETIC`` — a, b, …, z, aa, ab, …, zz, aaa, …  (Excel-style)

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

    columns: AxisLabelFormat = AxisLabelFormat.DISABLED
    rows: AxisLabelFormat = AxisLabelFormat.DISABLED

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
    """Excel-style 0-based alphabetic index: 0→a, 25→z, 26→aa, 701→zz, 702→aaa."""
    if i < 0:
        raise ValueError(f"axis index must be non-negative; got {i}")
    chars: list[str] = []
    n = i
    while True:
        chars.append(chr(ord("a") + n % 26))
        n = n // 26 - 1
        if n < 0:
            break
    return "".join(reversed(chars))


__all__ = ["AxisLabelFormat", "AxisLabelSettings", "format_index"]
