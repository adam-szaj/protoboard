# SPDX-License-Identifier: MIT
"""Per-pin bending mechanics owned by a package."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from protoboard.geometry import Point, Vector


@dataclass(frozen=True, slots=True)
class PinBendSpec:
    """Specification of one package pin's free two-dimensional bend."""

    min_reach: float = 0.0
    max_reach: float = 0.0
    default_offset: Vector = field(default_factory=lambda: Vector(0.0, 0.0))

    def is_free(self) -> bool:
        return self.max_reach > 0.0

    def is_within_bend_disc(
        self, pin_default_world: Point, target_world: Point
    ) -> bool:
        distance = math.hypot(
            target_world.x - pin_default_world.x,
            target_world.y - pin_default_world.y,
        )
        return distance <= self.max_reach

    def is_reachable_from_pin_default(
        self, pin_default_world: Point, target_world: Point
    ) -> bool:
        distance = math.hypot(
            target_world.x - pin_default_world.x,
            target_world.y - pin_default_world.y,
        )
        return self.min_reach <= distance <= self.max_reach
