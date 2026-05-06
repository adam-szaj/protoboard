# SPDX-License-Identifier: MIT
"""RoutingCostModel — per-step cost parameters for the A* router."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RoutingCostModel:
    """Configurable cost weights for A* routing.

    All values are additive costs per step.  `inf` means the step is
    forbidden.

    Diagonal moves (8-connectivity) use `free_pad * diagonal_multiplier`
    as their base cost.  The multiplier defaults to √2 so that diagonal
    and straight steps preserve Euclidean distance ratios.

    Wire-crossing semantics:
    - A bare wire crossing another bare wire is a short → `crossing_bare_bare = inf`.
    - An insulated wire crossing a bare wire is a jumper → `crossing_insulated_bare`.
    - An insulated wire crossing another insulated wire is fine → cost 0 extra.
    """

    free_pad: float = 1.0
    same_net_pad: float = 0.1          # discount for pads on the target net
    occupied_by_other_net: float = float("inf")   # pin of another net — blocked
    direction_change: float = 1.0      # penalty per turn (discourages detours)
    diagonal_multiplier: float = math.sqrt(2)     # cost factor for diagonal step
    # Crossing costs — applied when a wire passes over an existing wire segment.
    crossing_bare_bare: float = float("inf")      # bare × bare = short
    crossing_insulated_bare: float = 5.0          # insulated × bare = jumper OK
    # Layer-change cost — paid once per top↔bottom via on a plated-through pad.
    layer_change: float = 2.0
    # Extra cost per step on the top layer beneath a component body.
    # Routing under a component on top is legal but undesirable — it makes
    # physical inspection and rework harder.  A bottom-layer detour is
    # preferred when it is not significantly longer.
    under_component_top: float = 3.0
