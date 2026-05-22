# SPDX-License-Identifier: MIT
"""Tests for cost-model presets (built-in + resolution)."""

from __future__ import annotations

import math

import pytest

from protoboard.services.routing.cost_model import COST_KEY_DOCS, RoutingCostModel
from protoboard.services.routing.cost_presets import (
    builtin_preset_names,
    get_builtin_preset,
    resolve_weights,
)


class TestBuiltinPresets:
    def test_default_and_clean_routing_present(self) -> None:
        names = builtin_preset_names()
        assert "default" in names
        assert "clean_routing" in names

    def test_default_matches_legacy_routing_cost_model(self) -> None:
        preset = get_builtin_preset("default")
        assert preset is not None
        # Legacy defaults — what RoutingCostModel() returns with no args.
        assert preset["direction_change"] == 1.0
        assert preset["layer_change"] == 2.0
        # Legacy crossing_insulated_bare replaced by granular ib_* knobs.
        assert preset["crossing_ib_off_pad_same_side"] == 5.0
        assert preset["crossing_ib_at_pad_same_side"] == 5.0
        assert preset["free_pad"] == 1.0
        assert preset["same_net_pad"] == pytest.approx(0.1)
        assert preset["diagonal_multiplier"] == pytest.approx(math.sqrt(2))

    def test_clean_routing_has_active_clean_knobs(self) -> None:
        preset = get_builtin_preset("clean_routing")
        assert preset is not None
        # Angle-aware bends scaled so 135° is heavily discouraged.
        assert preset["bend_45"] < preset["bend_90"] < preset["bend_135"]
        # Diagonal parity bias is positive on the dispreferred family.
        assert preset["diagonal_dispreferred_family"] > 0.0
        # Foreign-pad knobs are non-zero (the whole point of the preset).
        assert preset["wire_over_foreign_wire_pad"] > 0.0
        assert preset["via_on_foreign_pad"] > 0.0
        assert preset["redundant_layer_change"] > 0.0

    def test_get_unknown_returns_none(self) -> None:
        assert get_builtin_preset("nonexistent") is None


class TestResolveWeights:
    def test_default_no_overrides(self) -> None:
        w = resolve_weights("default", {})
        assert w["direction_change"] == 1.0
        # Knobs not in "default" preset get the canonical default (0.0)
        # for opt-in knobs.
        assert w.get("bend_45", 0.0) == 0.0

    def test_clean_routing_no_overrides(self) -> None:
        w = resolve_weights("clean_routing", {})
        assert w["bend_45"] == 0.5
        assert w["wire_over_foreign_wire_pad"] == 0.5

    def test_override_beats_preset(self) -> None:
        w = resolve_weights("clean_routing", {"bend_45": 99.0})
        assert w["bend_45"] == 99.0

    def test_user_preset_overrides_resolved_when_unknown_name(self) -> None:
        custom = {"my_custom": {"bend_45": 7.5}}
        w = resolve_weights("my_custom", {}, user_presets=custom)
        assert w["bend_45"] == 7.5

    def test_unknown_preset_no_user_falls_back_to_default(self) -> None:
        # Falls back to defaults — legacy knobs present, new knobs zero.
        w = resolve_weights("nonexistent", {})
        assert w["direction_change"] == 1.0
        assert w.get("bend_45", 0.0) == 0.0

    def test_all_known_keys_have_docs(self) -> None:
        # Every key emitted by either built-in preset must have a doc entry
        # so the UI can render a tooltip.
        for name in builtin_preset_names():
            preset = get_builtin_preset(name)
            assert preset is not None
            for k in preset:
                assert k in COST_KEY_DOCS, f"preset {name!r} key {k!r} missing from COST_KEY_DOCS"


class TestCostModelFromPreset:
    def test_clean_routing_via_cost_model(self) -> None:
        w = resolve_weights("clean_routing", {})
        cm = RoutingCostModel(weights=w)
        assert cm.get("bend_45") == 0.5
        assert cm.get("via_on_foreign_pad") == 3.0
        # Legacy attribute path still works.
        assert cm.direction_change == 1.0
        assert cm.layer_change == 2.0
