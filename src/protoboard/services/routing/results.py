# SPDX-License-Identifier: MIT
"""Value objects returned by routing services."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from protoboard.domain.layout.wire import Wire


@dataclass
class NetStats:
    """Per-net routing statistics for one pass."""

    net_name: str
    routed: bool
    nodes_expanded: int
    over_budget_hits: int
    rip_up_attempts: int
    path_hops_min: int
    path_hops_max: int
    path_hops_final: int
    segments_found: int


@dataclass
class PassStats:
    """Statistics for one full routing pass."""

    pass_index: int
    seed: int
    routed_count: int
    unrouted_count: int
    total_wires: int
    total_nodes_expanded: int
    total_over_budget_hits: int
    total_rip_up_attempts: int
    nets: list[NetStats] = field(default_factory=list)


@dataclass
class AutorouteResult:
    """Result returned by the autorouter."""

    wires: list[Wire] = field(default_factory=list)
    unrouted_nets: list[str] = field(default_factory=list)
    pass_stats: list[PassStats] = field(default_factory=list)
