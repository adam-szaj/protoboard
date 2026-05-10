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
    # Wire type strategy.
    # "bare"      — always route as bare wire (no insulation)
    # "insulated" — always route as insulated wire (legacy behaviour)
    # "auto"      — try bare first; fall back to insulated if no bare path exists
    wire_mode: Literal["bare", "insulated", "auto"] = "auto"
    # When False, insulated wires may not cross bare wires (crossing_insulated_bare=inf).
    allow_jumpers: bool = True
    # Maximum allowed path length relative to Manhattan distance.
    # 0.0 means no limit.  E.g. 3.0 rejects any path longer than 3× Manhattan.
    max_detour_factor: float = 0.0
    # When True, skip the pre-flight dialog on next autoroute (use last settings).
    skip_autoroute_dialog: bool = False

    def cost_model(self) -> RoutingCostModel:
        """Build a RoutingCostModel from these settings."""
        cib = self.crossing_insulated_bare if self.allow_jumpers else float("inf")
        return RoutingCostModel(
            direction_change=self.direction_change,
            crossing_insulated_bare=cib,
            layer_change=self.layer_change,
            under_component_top=self.under_component_top,
        )


ROUTING_DEFAULTS = RoutingSettings()
