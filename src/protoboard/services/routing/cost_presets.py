# SPDX-License-Identifier: MIT
"""Cost-model presets — user-editable bundles of router cost weights.

A *preset* is a named ``dict[str, float]`` of cost-key → weight.  Projects
reference a preset by name (``RoutingSettings.cost_preset``) and may layer
per-project overrides on top (``RoutingSettings.cost_overrides``).  Final
weights = ``BUILTIN + user_presets[name] + overrides`` (right-most wins).

Built-in presets are read-only and always present.  User presets live in
``~/.config/protoboard/app_settings.yaml`` under the ``cost_presets`` key and
may be created/edited/deleted from PreferencesDialog.
"""

from __future__ import annotations

import math
from typing import Mapping


# ---------------------------------------------------------------------------
# Built-in presets — read-only, always available.
# ---------------------------------------------------------------------------

# "default" preset matches the historical RoutingSettings defaults — projects
# that do not specify a preset get bit-identical behaviour to pre-PR code.
_DEFAULT_PRESET: dict[str, float] = {
    "direction_change": 1.0,
    "layer_change": 2.0,
    "under_component_top": 3.0,
    # Legacy crossing_insulated_bare removed in B4 — granular ib_*
    # knobs handle it now.  Set baseline same-side off-pad to the
    # historical 5.0 so existing routes feel similar.
    "crossing_ib_off_pad_same_side": 5.0,
    "crossing_ib_at_pad_same_side": 5.0,
    "crossing_bare_bare_diagonal": 2.0,
    "free_pad": 1.0,
    "same_net_pad": 0.1,
    "diagonal_multiplier": math.sqrt(2),
    # All opt-in penalties default to 0.0 — preset just omits them.
}


# "clean_routing" preset — the recommended starting point for projects that
# want the new "nice path" penalties active.  Values are starting suggestions
# tunable per project via cost_overrides.
_CLEAN_ROUTING_PRESET: dict[str, float] = {
    **_DEFAULT_PRESET,
    # Angle-aware bends — gentle 45° bends are nearly free, sharp 135° are
    # heavily discouraged.
    "bend_45": 0.5,
    "bend_90": 1.5,
    "bend_135": 4.0,
    # Diagonal parity preference — diagonals in the "wrong" family on this
    # (parity, side) pay extra, biasing X-style mid-cell crossings.
    "diagonal_preferred_family": 0.0,
    "diagonal_dispreferred_family": 0.5,
    # Foreign-pad avoidance — wires should not casually loop through pads
    # owned by other nets.
    "wire_over_foreign_wire_pad": 0.5,
    "via_on_foreign_pad": 3.0,
    # Suppress unnecessary layer alternation ("zebra effect").
    "redundant_layer_change": 1.5,
}


_BUILTIN_PRESETS: dict[str, dict[str, float]] = {
    "default": _DEFAULT_PRESET,
    "clean_routing": _CLEAN_ROUTING_PRESET,
}


def builtin_preset_names() -> frozenset[str]:
    """Names of presets that ship with the app and cannot be edited/deleted."""
    return frozenset(_BUILTIN_PRESETS)


def get_builtin_preset(name: str) -> dict[str, float] | None:
    """Return a *copy* of the built-in preset, or None if unknown."""
    preset = _BUILTIN_PRESETS.get(name)
    return dict(preset) if preset is not None else None


def resolve_weights(
    preset_name: str,
    overrides: Mapping[str, float],
    user_presets: Mapping[str, Mapping[str, float]] | None = None,
) -> dict[str, float]:
    """Build the effective weight dict for a project.

    Resolution order (later wins):
      1. Built-in default preset (ensures every essential key has a value)
      2. The named preset (built-in or user-defined)
      3. Per-project overrides

    Unknown preset name falls back to "default" (with a warning logged by
    the caller if it wants to surface the misconfiguration).
    """
    weights: dict[str, float] = dict(_DEFAULT_PRESET)
    preset = _BUILTIN_PRESETS.get(preset_name)
    if preset is None and user_presets is not None:
        candidate = user_presets.get(preset_name)
        if candidate is not None:
            preset = dict(candidate)
    if preset is not None:
        weights.update(preset)
    weights.update(overrides)
    return weights


__all__ = [
    "builtin_preset_names",
    "get_builtin_preset",
    "resolve_weights",
]
