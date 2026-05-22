# SPDX-License-Identifier: MIT
"""RoutingSettings — per-project autorouter configuration.

All cost-model weights live in the ``cost_preset`` (named bundle from
AppSettings) + ``cost_overrides`` (per-project diff) pair.  This module
only carries non-cost logistics: which router to use, what wire mode,
how many rip-up passes, which optimizers run after routing, etc.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping, TYPE_CHECKING

from protoboard.services.routing.cost_model import RoutingCostModel
from protoboard.services.routing.cost_presets import resolve_weights

if TYPE_CHECKING:
    from protoboard.services.routing.postprocess import SimplifyMode


def _default_simplify_mode() -> "SimplifyMode":
    """Late-import default for SimplifyMode to avoid a cycle with postprocess."""
    from protoboard.services.routing.postprocess import SimplifyMode
    return SimplifyMode.BARE_PARITY


@dataclass(frozen=True)
class RoutingSettings:
    """Per-project autorouter logistics (non-cost knobs only).

    For cost weights see ``cost_preset`` (name pointing at an entry in
    AppSettings.cost_presets or a built-in) and ``cost_overrides``
    (per-project diff merged on top of the preset).
    """

    # ---- Layer & topology -------------------------------------------
    preferred_side: Literal["top", "bottom"] = "bottom"
    allow_layer_change: bool = True
    # ---- Iteration budgets ------------------------------------------
    max_rip_up_passes: int = 3
    max_optimize_iterations: int = 10
    # ---- Wire strategy ----------------------------------------------
    # "bare"          — always route as bare wire (no insulation)
    # "insulated"     — always route as insulated wire
    # "auto"          — try bare first; fall back to insulated if needed
    # "bare_diagonal" — bare wire restricted to diagonal-parity family;
    #                   permits perpendicular crossings between mesh cells
    wire_mode: Literal["bare", "insulated", "auto", "bare_diagonal"] = "auto"
    # Registry name of the router plugin to use.  Plain str so plugins
    # discovered at runtime can be referenced.  Falls back to "astar" when
    # a saved name no longer maps to a registered router.
    router_name: str = "astar"
    # When False, insulated wires may not cross bare wires at all.  At
    # cost-model build time this is implemented by setting all four
    # ``crossing_ib_*`` knobs to inf.
    allow_jumpers: bool = True
    # Maximum allowed path length relative to Manhattan distance.
    # 0.0 means no limit.  E.g. 3.0 rejects any path longer than 3×
    # Manhattan distance from source to nearest target.
    max_detour_factor: float = 0.0
    # When True, skip the pre-flight dialog on next autoroute (reuse last
    # settings).
    skip_autoroute_dialog: bool = False
    # When True, nudge blocking wires sideways before ripping them up.
    push_and_shove: bool = True
    # When True, run a post-route simplify pass.
    enable_simplify_pass: bool = True
    # Simplify pass strategy.  BARE_PARITY preserves anti-short invariant.
    simplify_mode: "SimplifyMode" = field(  # type: ignore[name-defined]
        default_factory=lambda: _default_simplify_mode()
    )
    # Per-project WireOptimizer plugin chain.  Empty tuple = legacy
    # behaviour (autoroute()'s embedded simplify pass fires per
    # ``enable_simplify_pass``).  Non-empty = the named optimizers run in
    # this exact order; the embedded simplify pass is suppressed.
    optimizer_chain: tuple[str, ...] = field(default_factory=tuple)
    # ---- Cost weights (via preset + overrides) ----------------------
    # ``cost_preset`` names a built-in ("default", "clean_routing") or
    # user-defined preset in AppSettings.cost_presets.
    # ``cost_overrides`` is a per-project diff layered on top.
    cost_preset: str = "default"
    cost_overrides: Mapping[str, float] = field(default_factory=dict)

    def cost_model(
        self,
        user_presets: Mapping[str, Mapping[str, float]] | None = None,
    ) -> RoutingCostModel:
        """Build a RoutingCostModel from this settings object.

        Resolution order (later wins):
          1. Built-in defaults from cost_presets._LEGACY_DEFAULTS
          2. The named ``cost_preset``
          3. Per-project ``cost_overrides``
          4. ``wire_mode`` / ``allow_jumpers`` overrides applied last so the
             logistic flags retain priority over preset values.
        """
        weights = resolve_weights(
            self.cost_preset, dict(self.cost_overrides), user_presets
        )
        # wire_mode adjustments
        bare_diag = self.wire_mode == "bare_diagonal"
        weights["bare_diagonal_mode"] = 1.0 if bare_diag else 0.0
        if not bare_diag:
            # Outside bare_diagonal mode, perpendicular bare-diagonal
            # crossings are not safe → forbid via inf.
            weights["crossing_bare_bare_diagonal"] = float("inf")
        # allow_jumpers: when False, forbid every kind of insulated×bare
        # crossing by setting the granular ib knobs to inf.  Also pin the
        # legacy crossing_insulated_bare fallback so any plugin still
        # reading it gets the same answer.
        if not self.allow_jumpers:
            weights["crossing_insulated_bare"] = float("inf")
            weights["crossing_ib_at_pad_same_side"] = float("inf")
            weights["crossing_ib_at_pad_opposite_side"] = float("inf")
            weights["crossing_ib_off_pad_same_side"] = float("inf")
            weights["crossing_ib_off_pad_opposite_side"] = float("inf")
        return RoutingCostModel(weights=weights)


ROUTING_DEFAULTS = RoutingSettings()
