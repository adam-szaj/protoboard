# SPDX-License-Identifier: MIT
"""Component definitions — name + footprint + pin name mapping."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from protoboard.domain.components.footprints import FootprintTemplate


@dataclass(frozen=True, slots=True)
class ComponentDefinition:
    """Static definition of a component (a "type")."""

    name: str
    footprint: FootprintTemplate
    pin_names: dict[int, str]
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("ComponentDefinition.name must not be empty")
        for pin_number in self.pin_names:
            if pin_number < 1 or pin_number > self.footprint.pin_count():
                raise ValueError(
                    f"pin_names references pin {pin_number} which is outside "
                    f"the footprint's pin range (1..{self.footprint.pin_count()})"
                )

    @property
    def pin_count(self) -> int:
        return self.footprint.pin_count()

    def pin_name(self, pin_number: int) -> str:
        """Return the user-facing name of the given pin (1-indexed)."""
        if pin_number < 1 or pin_number > self.pin_count:
            raise ValueError(f"pin {pin_number} out of range (1..{self.pin_count})")
        return self.pin_names.get(pin_number, f"P{pin_number}")
