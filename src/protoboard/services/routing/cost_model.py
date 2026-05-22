# SPDX-License-Identifier: MIT
"""RoutingCostModel — per-step cost parameters for the A* router.

The cost model is stored internally as a ``dict[str, float]`` so that routers
can declare which keys they support (see ``Router.supported_cost_keys``) and
presets / UI / persistence can iterate cost knobs uniformly without touching
this file when new keys are added.

For backward compatibility every legacy named field is still accessible as a
``@property`` that delegates to the underlying weights dict.  New code should
prefer ``cost_model.get("key_name", default)`` over attribute access.
"""

from __future__ import annotations

import math
from dataclasses import FrozenInstanceError
from typing import Any


# Canonical documentation of every cost key recognised by any built-in router.
# Used by PreferencesDialog to render labels/tooltips and by persistence to
# validate user-supplied YAML. Routers declare which subset they actually
# consume via ``Router.supported_cost_keys()``.
COST_KEY_DOCS: dict[str, str] = {
    "free_pad": "Base cost per orthogonal step on a free pad.",
    "same_net_pad": "Cost per step onto a pad of the active net (discount).",
    "occupied_by_other_net": "Cost per step onto a pin of another net (usually inf).",
    "direction_change": "Penalty per turn (fallback when bend_45/90/135 are unset).",
    "diagonal_multiplier": "Cost multiplier for diagonal steps (default √2).",
    "crossing_bare_bare": "Cost when a bare wire crosses another bare wire (usually inf).",
    "crossing_bare_bare_diagonal": "Cost when two bare diagonals cross at a mesh-cell centre.",
    "crossing_insulated_bare": "[deprecated] flat fallback for insulated × bare crossings — superseded by the 8 crossing_ib_* / crossing_ii_* keys.",
    # New granular crossing classification:
    #   ib = insulated × bare,   ii = insulated × insulated
    #   at_pad / off_pad         where the crossing geometrically intersects
    #   same_side / opposite     relative to the new wire's side
    # All default to 0.0 (legacy crossing_insulated_bare carries the flat
    # fallback until B4 drops it).
    "crossing_ib_at_pad_same_side":
        "Insulated × bare crossing on the same side, intersection on a pad.",
    "crossing_ib_at_pad_opposite_side":
        "Insulated × bare crossing on opposite sides, intersection on a pad.",
    "crossing_ib_off_pad_same_side":
        "Insulated × bare crossing on the same side, intersection between pads.",
    "crossing_ib_off_pad_opposite_side":
        "Insulated × bare crossing on opposite sides, intersection between pads.",
    "crossing_ii_at_pad_same_side":
        "Insulated × insulated crossing on the same side, intersection on a pad.",
    "crossing_ii_at_pad_opposite_side":
        "Insulated × insulated crossing on opposite sides, intersection on a pad.",
    "crossing_ii_off_pad_same_side":
        "Insulated × insulated crossing on the same side, intersection between pads.",
    "crossing_ii_off_pad_opposite_side":
        "Insulated × insulated crossing on opposite sides, intersection between pads.",
    "layer_change": "Cost per via (top↔bottom transition on a plated-through pad).",
    "under_component_top": "Extra cost per step on the top layer beneath a component body.",
    "bare_diagonal_mode": "When non-zero, enables strict parity-filter for bare diagonals.",
    "at_pad_insulated_insulated_same_side": "Cost when step lands on a pad with another insulated wire's vertex (same side).",
    "at_pad_insulated_bare_same_side": "Cost when step lands on a pad with a bare wire's vertex (mixed-insulation, same side).",
    "parallel_opposite_side": "Cost when the step overlaps an existing wire segment on the opposite side.",
    "max_reroute_factor": "Push-and-shove reroute ceiling as a multiple of the original cost.",
    "same_net_wire_reuse_cost": "Cost when touching an existing wire of the active net (0 = free same-net reuse).",
    # ------------------------------------------------------------------
    # New in the "clean routing" effort (defaults all 0.0 — opt-in via preset).
    # ------------------------------------------------------------------
    "bend_45": "Penalty per 45° bend in path (falls back to direction_change).",
    "bend_90": "Penalty per 90° bend in path (falls back to direction_change).",
    "bend_135": "Penalty per 135° bend in path (falls back to direction_change).",
    "diagonal_preferred_family": "Bonus/cost per diagonal step in the preferred (parity,side)→family direction.",
    "diagonal_dispreferred_family": "Cost per diagonal step in the dispreferred family.",
    "wire_over_foreign_wire_pad": "Cost per step into a cell occupied by another net's wire vertex.",
    "via_on_foreign_pad": "Cost per via on a pad where another net already has a via.",
    "redundant_layer_change": "Cost per via that could have been avoided (also drives zebra suppression).",
}


# Subset of cost keys exposed in the UI (presets dialog + RoutingPage).
# Keys not in this set are either:
# - physical constants (free_pad=1, diagonal_multiplier=√2)
# - hard electrical bans (crossing_bare_bare=inf, occupied_by_other_net=inf)
# - implementation details (same_net_pad)
# Modifying them rarely makes sense and showing them invites mistakes.
USER_TUNABLE_COST_KEYS: frozenset[str] = frozenset({
    "direction_change",
    "layer_change",
    "under_component_top",
    "crossing_insulated_bare",
    "crossing_bare_bare_diagonal",
    "bare_diagonal_mode",
    "at_pad_insulated_insulated_same_side",
    "at_pad_insulated_bare_same_side",
    "parallel_opposite_side",
    "max_reroute_factor",
    "same_net_wire_reuse_cost",
    "bend_45",
    "bend_90",
    "bend_135",
    "diagonal_preferred_family",
    "diagonal_dispreferred_family",
    "wire_over_foreign_wire_pad",
    "via_on_foreign_pad",
    "redundant_layer_change",
    # 8 granular crossing knobs (insulated × {bare, insulated}) × {at_pad,
    # off_pad} × {same_side, opposite_side}.
    "crossing_ib_at_pad_same_side",
    "crossing_ib_at_pad_opposite_side",
    "crossing_ib_off_pad_same_side",
    "crossing_ib_off_pad_opposite_side",
    "crossing_ii_at_pad_same_side",
    "crossing_ii_at_pad_opposite_side",
    "crossing_ii_off_pad_same_side",
    "crossing_ii_off_pad_opposite_side",
})


# Legacy default values — preserved verbatim so that constructing
# ``RoutingCostModel()`` with no args produces the same numeric weights as
# the original frozen-dataclass implementation.
_LEGACY_DEFAULTS: dict[str, float] = {
    "free_pad": 1.0,
    "same_net_pad": 0.1,
    "occupied_by_other_net": float("inf"),
    "direction_change": 1.0,
    "diagonal_multiplier": math.sqrt(2),
    "crossing_bare_bare": float("inf"),
    "crossing_bare_bare_diagonal": float("inf"),
    "crossing_insulated_bare": 5.0,
    # New granular crossing knobs — all 0.0 so existing projects (and the
    # legacy crossing_insulated_bare fallback) keep their current behaviour
    # until presets / project overrides explicitly set them.
    "crossing_ib_at_pad_same_side": 0.0,
    "crossing_ib_at_pad_opposite_side": 0.0,
    "crossing_ib_off_pad_same_side": 0.0,
    "crossing_ib_off_pad_opposite_side": 0.0,
    "crossing_ii_at_pad_same_side": 0.0,
    "crossing_ii_at_pad_opposite_side": 0.0,
    "crossing_ii_off_pad_same_side": 0.0,
    "crossing_ii_off_pad_opposite_side": 0.0,
    "layer_change": 2.0,
    "under_component_top": 3.0,
    "bare_diagonal_mode": 0.0,  # boolean encoded as 0.0 / non-zero
    "at_pad_insulated_insulated_same_side": 0.0,
    "at_pad_insulated_bare_same_side": 0.0,
    "parallel_opposite_side": 0.0,
    "max_reroute_factor": 0.0,
    "same_net_wire_reuse_cost": 0.0,
}


class RoutingCostModel:
    """Configurable cost weights for A* routing.

    Stored as ``dict[str, float]`` (``weights``).  Routers should query weights
    via ``get(key, default)``; legacy attribute access (``cost.direction_change``)
    is preserved via ``@property`` for backward compatibility.

    Construction accepts either ``RoutingCostModel(weights={...})`` or legacy
    keyword args ``RoutingCostModel(direction_change=1.0, layer_change=2.0)``.
    Both forms are equivalent; the latter is the historical idiom.
    """

    __slots__ = ("weights",)

    def __init__(self, **kwargs: Any) -> None:
        # Two construction modes, both supported:
        #   RoutingCostModel(weights={"direction_change": 1.0, ...})
        #   RoutingCostModel(direction_change=1.0, layer_change=2.0)
        # The first is canonical; the second is the legacy idiom from
        # before the dict refactor.  Mixing them is allowed: explicit kwargs
        # override entries in ``weights``.
        weights_arg = kwargs.pop("weights", None)
        provided: dict[str, float] = dict(weights_arg) if weights_arg else {}
        for k, v in kwargs.items():
            if isinstance(v, bool):
                provided[k] = 1.0 if v else 0.0
            else:
                provided[k] = float(v)
        merged = {**_LEGACY_DEFAULTS, **provided}
        object.__setattr__(self, "weights", merged)

    def __setattr__(self, name: str, value: Any) -> None:
        raise FrozenInstanceError(
            "RoutingCostModel is immutable; use with_overrides() to change weights"
        )

    def __eq__(self, other: object) -> bool:
        return isinstance(other, RoutingCostModel) and self.weights == other.weights

    def __hash__(self) -> int:
        return hash(tuple(sorted(self.weights.items())))

    def __repr__(self) -> str:
        return f"RoutingCostModel(weights={self.weights!r})"

    def get(self, key: str, default: float = 0.0) -> float:
        return self.weights.get(key, default)

    def with_overrides(self, **overrides: float) -> "RoutingCostModel":
        """Return a new instance with selected weights overridden."""
        return RoutingCostModel(weights={**self.weights, **overrides})

    @classmethod
    def from_legacy(cls, **kwargs: Any) -> "RoutingCostModel":
        """Build a cost model from legacy keyword args (backward-compat shim)."""
        # Normalise bool → float for bare_diagonal_mode
        normalised: dict[str, float] = {}
        for k, v in kwargs.items():
            if isinstance(v, bool):
                normalised[k] = 1.0 if v else 0.0
            else:
                normalised[k] = float(v)
        return cls(weights=normalised)

    # ------------------------------------------------------------------
    # Legacy attribute access — every named field becomes a @property
    # reading from ``weights``.  Preserves existing call-sites verbatim.
    # ------------------------------------------------------------------
    @property
    def free_pad(self) -> float: return self.weights["free_pad"]
    @property
    def same_net_pad(self) -> float: return self.weights["same_net_pad"]
    @property
    def occupied_by_other_net(self) -> float: return self.weights["occupied_by_other_net"]
    @property
    def direction_change(self) -> float: return self.weights["direction_change"]
    @property
    def diagonal_multiplier(self) -> float: return self.weights["diagonal_multiplier"]
    @property
    def crossing_bare_bare(self) -> float: return self.weights["crossing_bare_bare"]
    @property
    def crossing_bare_bare_diagonal(self) -> float: return self.weights["crossing_bare_bare_diagonal"]
    @property
    def crossing_insulated_bare(self) -> float: return self.weights["crossing_insulated_bare"]
    @property
    def layer_change(self) -> float: return self.weights["layer_change"]
    @property
    def under_component_top(self) -> float: return self.weights["under_component_top"]
    @property
    def bare_diagonal_mode(self) -> bool: return bool(self.weights["bare_diagonal_mode"])
    @property
    def at_pad_insulated_insulated_same_side(self) -> float: return self.weights["at_pad_insulated_insulated_same_side"]
    @property
    def at_pad_insulated_bare_same_side(self) -> float: return self.weights["at_pad_insulated_bare_same_side"]
    @property
    def parallel_opposite_side(self) -> float: return self.weights["parallel_opposite_side"]
    @property
    def max_reroute_factor(self) -> float: return self.weights["max_reroute_factor"]
    @property
    def same_net_wire_reuse_cost(self) -> float: return self.weights["same_net_wire_reuse_cost"]
