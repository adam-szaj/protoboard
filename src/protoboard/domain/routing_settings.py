# SPDX-License-Identifier: MIT
"""RoutingSettings — per-project autorouter configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TYPE_CHECKING

from protoboard.services.routing.cost_model import RoutingCostModel

if TYPE_CHECKING:
    from protoboard.services.routing.postprocess import SimplifyMode


def _default_simplify_mode() -> "SimplifyMode":
    """Late-import default for SimplifyMode to avoid a cycle with postprocess."""
    from protoboard.services.routing.postprocess import SimplifyMode
    return SimplifyMode.BARE_PARITY


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
    # Registry name of the router plugin that should run next.  Persisted
    # so the AutorouteDialog re-opens with the user's last choice.  Plain
    # ``str`` (not ``Literal``) because plugins extend the registry at
    # runtime — the dialog falls back to "astar" when a saved name no
    # longer maps to a registered router.
    router_name: str = "astar"
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
    # Strategy used by the simplify pass.  See SimplifyMode for details.
    # Default BARE_PARITY preserves the anti-short invariant that
    # bare_diagonal routing relies on.
    simplify_mode: "SimplifyMode" = field(  # type: ignore[name-defined]
        default_factory=lambda: _default_simplify_mode()
    )
    # Per-project WireOptimizer plugin chain.  Empty tuple = legacy
    # behaviour (autoroute()'s embedded simplify pass fires per
    # ``enable_simplify_pass``).  Non-empty = the named optimizers run in
    # this exact order on the router's output and the embedded simplify
    # pass is suppressed.  Stored as tuple so the frozen dataclass stays
    # truly immutable.
    optimizer_chain: tuple[str, ...] = field(default_factory=tuple)
    # ------------------------------------------------------------------
    # Enhanced-router knobs (opt-in; off by default).  Surface the new
    # RoutingCostModel fields so they round-trip through the project
    # YAML.  The default ``astar`` router ignores them; the
    # ``astar_enhanced`` plugin reads them in its A* hot loop.
    # ------------------------------------------------------------------
    at_pad_insulated_insulated_same_side: float = 0.0
    at_pad_insulated_bare_same_side: float = 0.0
    parallel_opposite_side: float = 0.0
    # Mid-cell push-and-shove ceiling (Feature C).  0 disables; the
    # enhanced router treats values > 0 as "reroute a blocker via the
    # opposite parity family if the new cost is at most this factor
    # times the original".
    max_reroute_factor: float = 0.0

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
            at_pad_insulated_insulated_same_side=self.at_pad_insulated_insulated_same_side,
            at_pad_insulated_bare_same_side=self.at_pad_insulated_bare_same_side,
            parallel_opposite_side=self.parallel_opposite_side,
            max_reroute_factor=self.max_reroute_factor,
        )


ROUTING_DEFAULTS = RoutingSettings()
