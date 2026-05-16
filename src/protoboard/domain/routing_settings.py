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
    # Cost to cross two bare diagonals at a mesh-cell centre — only consulted
    # when wire_mode == "bare_diagonal".  Default 2.0 is roughly the cost of a
    # short corner-cut, encouraging the router to use crossings sparingly.
    crossing_bare_bare_diagonal: float = 2.0
    layer_change: float = 2.0
    under_component_top: float = 3.0
    # Wire type strategy.
    # "bare"          — always route as bare wire (no insulation)
    # "insulated"     — always route as insulated wire (legacy behaviour)
    # "auto"          — try bare first; fall back to insulated if no bare path exists
    # "bare_diagonal" — bare wire restricted to diagonal-parity family;
    #                   permits perpendicular crossings between mesh cells
    wire_mode: Literal["bare", "insulated", "auto", "bare_diagonal"] = "auto"
    # When False, insulated wires may not cross bare wires (crossing_insulated_bare=inf).
    allow_jumpers: bool = True
    # Maximum allowed path length relative to Manhattan distance.
    # 0.0 means no limit.  E.g. 3.0 rejects any path longer than 3× Manhattan.
    max_detour_factor: float = 0.0
    # When True, skip the pre-flight dialog on next autoroute (use last settings).
    skip_autoroute_dialog: bool = False
    # When True, try to nudge blocking wires sideways before ripping them up.
    push_and_shove: bool = True
    # When True (and wire_mode == "bare_diagonal"), run a post-route simplify
    # pass: collinear-segment merge + ortho-ortho-corner → diagonal corner-cut.
    enable_simplify_pass: bool = True

    def cost_model(self) -> RoutingCostModel:
        """Build a RoutingCostModel from these settings."""
        cib = self.crossing_insulated_bare if self.allow_jumpers else float("inf")
        bare_diag = self.wire_mode == "bare_diagonal"
        cbbd = self.crossing_bare_bare_diagonal if bare_diag else float("inf")
        return RoutingCostModel(
            direction_change=self.direction_change,
            crossing_insulated_bare=cib,
            crossing_bare_bare_diagonal=cbbd,
            layer_change=self.layer_change,
            under_component_top=self.under_component_top,
            bare_diagonal_mode=bare_diag,
        )


ROUTING_DEFAULTS = RoutingSettings()
