# SPDX-License-Identifier: MIT
"""RoutingSettings — per-project autorouter configuration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from protoboard.services.routing.cost_model import RoutingCostModel


@dataclass(frozen=True)
class RoutingSettings:
    """User-facing autorouter parameters stored per project.

    Only the subset of RoutingCostModel fields that make physical sense for
    users to adjust are exposed here.  Fields that are physically fixed
    (bare×bare = short → inf) or implementation details (free_pad, same_net_pad,
    diagonal_multiplier) are omitted.
    """

    preferred_side: Literal["top", "bottom"] = "bottom"
    allow_layer_change: bool = True
    max_rip_up_passes: int = 3
    max_optimize_iterations: int = 10
    direction_change: float = 1.0
    crossing_insulated_bare: float = 5.0
    layer_change: float = 2.0
    under_component_top: float = 3.0

    def cost_model(self) -> RoutingCostModel:
        """Build a RoutingCostModel from these settings."""
        return RoutingCostModel(
            direction_change=self.direction_change,
            crossing_insulated_bare=self.crossing_insulated_bare,
            layer_change=self.layer_change,
            under_component_top=self.under_component_top,
        )


ROUTING_DEFAULTS = RoutingSettings()
