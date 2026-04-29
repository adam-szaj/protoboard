# SPDX-License-Identifier: MIT
"""Box model for board geometry.

A board is described as a tree of boxes. Containers (`HBox`, `VBox`,
`BoardGrid`) arrange children; atoms (`PadAtom`, `StripAtom`, `GapAtom`)
are leaves. Each box declares per-axis sizing policy:

- `FIXED`: explicit size
- `HUG`:   fits the content
- `FILL`:  fills available space (resolved during the layout pass)

Margins (`Insets`) are per-instance and may be negative — useful for
overlapping rails on breadboards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from protoboard.domain.board.pads import PadLayers
from protoboard.geometry import Insets


class SizePolicy(Enum):
    FIXED = "fixed"
    HUG = "hug"
    FILL = "fill"


@dataclass(frozen=True, slots=True)
class Box:
    """Base class for all box types.

    Subclasses must be dataclasses with `frozen=True`. Validation that is
    common to every box (policy/dimension consistency) happens here.
    """

    width_policy: SizePolicy
    height_policy: SizePolicy
    width: float | None = None
    height: float | None = None
    margin: Insets = field(default_factory=Insets.zero)

    def __post_init__(self) -> None:
        self._validate_axis("width", self.width_policy, self.width)
        self._validate_axis("height", self.height_policy, self.height)

    @staticmethod
    def _validate_axis(name: str, policy: SizePolicy, value: float | None) -> None:
        if policy is SizePolicy.FIXED and value is None:
            raise ValueError(f"{name} required when {name}_policy=FIXED")
        if policy is not SizePolicy.FIXED and value is not None:
            raise ValueError(f"{name} must be None when {name}_policy is HUG or FILL")


# ---------------------------------------------------------------------------
# Atoms (leaves)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PadAtom(Box):
    """A single hole. Intrinsic size is 1×1 raster unit."""

    layers: PadLayers = field(default_factory=PadLayers)
    label: str | None = None


@dataclass(frozen=True, slots=True)
class StripAtom(Box):
    """A continuous strip of N galvanically connected holes.

    When *isolated* is True the strip segment is electrically disconnected
    from the board (the copper was drilled/scraped out). It still appears as
    a physical hole and can be used for mechanical anchoring or to route a
    wire through to the other layer.
    """

    length: int | Literal["fill"] = 1
    orientation: Literal["horizontal", "vertical"] = "horizontal"
    layers: PadLayers = field(default_factory=PadLayers)
    cuttable: bool = True
    isolated: bool = False
    label: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.orientation not in ("horizontal", "vertical"):
            raise ValueError(
                f"orientation must be 'horizontal' or 'vertical'; "
                f"got {self.orientation!r}"
            )
        if isinstance(self.length, int) and self.length <= 0:
            raise ValueError(f"length must be > 0 or 'fill'; got {self.length}")
        if isinstance(self.length, str) and self.length != "fill":
            raise ValueError(f"string length must be 'fill'; got {self.length!r}")


@dataclass(frozen=True, slots=True)
class GapAtom(Box):
    """Empty space — no pads, just takes up area in the layout."""


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HBox(Box):
    """Horizontal stack of children."""

    children: list[Box] = field(default_factory=list)
    gap: float = 1.0


@dataclass(frozen=True, slots=True)
class VBox(Box):
    """Vertical stack of children."""

    children: list[Box] = field(default_factory=list)
    gap: float = 1.0


@dataclass(frozen=True, slots=True)
class BoardGrid(Box):
    """Regular row × col grid of identical cells (the cell is a Box template)."""

    cell: Box | None = None
    rows: int = 1
    cols: int = 1
    row_gap: float = 1.0
    col_gap: float = 1.0

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.cell is None:
            raise ValueError("BoardGrid requires a cell template")
        if self.rows < 1:
            raise ValueError(f"rows must be >= 1; got {self.rows}")
        if self.cols < 1:
            raise ValueError(f"cols must be >= 1; got {self.cols}")
