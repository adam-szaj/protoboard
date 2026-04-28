# SPDX-License-Identifier: MIT
"""RoutingCostModel — per-step cost parameters for the A* router."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RoutingCostModel:
    """Configurable cost weights for A* routing.

    All values are additive costs per step.  `inf` means the step is
    forbidden.
    """

    free_pad: float = 1.0
    same_net_pad: float = 0.1  # discount for pads already on the target net
    occupied_by_other_net: float = float("inf")  # collision
    direction_change: float = 0.2  # mild penalty for turns (aesthetics)
