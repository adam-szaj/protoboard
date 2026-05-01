# SPDX-License-Identifier: MIT
"""WireSegment — physical wire on the board."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from protoboard.geometry import Point


@dataclass
class WireSegment:
    id: uuid.UUID
    path: list[Point]
    insulated: bool
    side: Literal["top", "bottom"]
    color: str | None = None

    def __hash__(self) -> int:
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, WireSegment):
            return NotImplemented
        return self.id == other.id

    def __post_init__(self) -> None:
        if self.side not in ("top", "bottom"):
            raise ValueError(f"side must be 'top' or 'bottom'; got {self.side!r}")
        if len(self.path) < 2:
            raise ValueError("WireSegment path must have at least 2 points")
