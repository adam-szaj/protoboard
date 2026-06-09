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
    # When True, route group-internal nets first (bottom-up over the
    # group tree) before tackling cross-group / global connections.
    # Lets the group hierarchy drive routing strategy — exported pins
    # become the natural waypoints for top-level routes.  Requires that
    # the project use groups; on a flat schematic this just runs a
    # single top-level pass (same as ``hierarchical=False``).
    hierarchical: bool = False
    # When True, run a post-route simplify pass.
    enable_simplify_pass: bool = True
    # When True, run simplify on each wire immediately after it is routed
    # (in addition to / instead of the final pass).  Lets later wires in
    # the same autoroute pass see the shortened geometry — useful for
    # dense boards where a long bare path would otherwise block a parallel
    # neighbour from finding its diagonal shortcut.  Trades autoroute time
    # for routability + per-pass quality.
    simplify_after_each_wire: bool = False
    # When True, run a full iterative simplify-all pass at the very end
    # of autoroute (after every net routed and any embedded simplify).
    # Iterates over all wires until one full pass produces no change,
    # bounded by ``simplify_all_max_iterations``.  Cleans up "trapez
    # without baseline" shapes that the per-net simplify and the
    # bare-diagonal-only legacy pass miss.  Default ON because
    # ``simplify_wires_generic`` is conservative (only shortens, never
    # extends) so the worst case is a no-op cost.
    simplify_all_after_autoroute: bool = True
    # Hard upper bound on the number of full simplify-all iterations.
    # Each iteration re-scans every wire; the loop stops earlier as soon
    # as one full pass changes nothing.  50 is a defensive cap — in
    # practice convergence is reached in 2-5 iterations on dense boards.
    simplify_all_max_iterations: int = 50
    # When True, the unweave operation may flip a wire to the opposite
    # board side as a last-resort move when topological swap + overlap
    # separation cannot reduce visual tangling.  Useful for cross-side
    # wires whose 2D projection weaves through one another even though
    # they don't physically conflict.  Off by default — opt-in feature
    # because a flip changes a structural attribute of the wire
    # (compared to swap which is path-only).  Validation: combined
    # gate — shared_pads and crossings on the 2D projection must
    # both not worsen, and at least one must strictly improve.
    visual_unweave_side_flip: bool = False
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
        # crossing by setting all 4 granular ib_* knobs to inf.
        if not self.allow_jumpers:
            weights["crossing_ib_at_pad_same_side"] = float("inf")
            weights["crossing_ib_at_pad_opposite_side"] = float("inf")
            weights["crossing_ib_off_pad_same_side"] = float("inf")
            weights["crossing_ib_off_pad_opposite_side"] = float("inf")
        return RoutingCostModel(weights=weights)


ROUTING_DEFAULTS = RoutingSettings()
