# SPDX-License-Identifier: MIT
"""Net and PinRef — kept in one module to avoid circular imports."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PinRef:
    """Reference to a specific pin of a specific component instance."""

    component_id: uuid.UUID
    pin_number: int

    def __post_init__(self) -> None:
        if self.pin_number < 1:
            raise ValueError(f"pin_number must be >= 1; got {self.pin_number}")


@dataclass
class Net:
    """A logical network: the set of pins that must be electrically connected."""

    id: uuid.UUID
    name: str
    pins: set[PinRef] = field(default_factory=set)
    color: str | None = None  # hex color string e.g. "#E03030"; None = auto from palette

    def __hash__(self) -> int:
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Net):
            return NotImplemented
        return self.id == other.id

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Net.name must not be empty")
