# SPDX-License-Identifier: MIT
"""Tests for RoutingSettings domain model and persistence round-trip."""

from __future__ import annotations

from protoboard.domain.routing_settings import ROUTING_DEFAULTS, RoutingSettings
from protoboard.services.persistence.project_yaml import (
    _routing_settings_from_yaml,
    _routing_settings_to_yaml,
)


class TestRoutingSettingsModel:
    def test_defaults_are_sane(self) -> None:
        s = RoutingSettings()
        assert s.preferred_side == "bottom"
        assert s.allow_layer_change is True
        assert s.max_rip_up_passes == 3
        assert s.max_optimize_iterations == 10
        assert s.direction_change == 1.0
        assert s.crossing_insulated_bare == 5.0
        assert s.layer_change == 2.0
        assert s.under_component_top == 3.0

    def test_module_constant_equals_defaults(self) -> None:
        assert ROUTING_DEFAULTS == RoutingSettings()

    def test_cost_model_applies_fields(self) -> None:
        s = RoutingSettings(direction_change=2.5, layer_change=4.0)
        cm = s.cost_model()
        assert cm.direction_change == 2.5
        assert cm.layer_change == 4.0
        assert cm.crossing_insulated_bare == 5.0

    def test_cost_model_preserves_fixed_fields(self) -> None:
        import math
        s = RoutingSettings()
        cm = s.cost_model()
        assert cm.free_pad == 1.0
        assert cm.same_net_pad == 0.1
        assert math.isinf(cm.occupied_by_other_net)
        assert math.isinf(cm.crossing_bare_bare)

    def test_preferred_side_top(self) -> None:
        s = RoutingSettings(preferred_side="top")
        assert s.preferred_side == "top"


class TestRoutingSettingsPersistence:
    def test_defaults_roundtrip(self) -> None:
        s = RoutingSettings()
        yaml_text = _routing_settings_to_yaml(s)
        s2 = _routing_settings_from_yaml(yaml_text)
        assert s == s2

    def test_custom_values_roundtrip(self) -> None:
        s = RoutingSettings(
            preferred_side="top",
            allow_layer_change=False,
            max_rip_up_passes=5,
            max_optimize_iterations=3,
            direction_change=2.0,
            crossing_insulated_bare=8.5,
            layer_change=1.5,
            under_component_top=6.0,
        )
        yaml_text = _routing_settings_to_yaml(s)
        s2 = _routing_settings_from_yaml(yaml_text)
        assert s == s2

    def test_missing_keys_use_defaults(self) -> None:
        s = _routing_settings_from_yaml("{}")
        assert s == RoutingSettings()

    def test_partial_cost_model_uses_defaults(self) -> None:
        yaml_text = "cost_model:\n  direction_change: 3.0\n"
        s = _routing_settings_from_yaml(yaml_text)
        assert s.direction_change == 3.0
        assert s.layer_change == 2.0  # default

    def test_yaml_contains_routing_section(self) -> None:
        s = RoutingSettings(preferred_side="top")
        text = _routing_settings_to_yaml(s)
        assert "preferred_side" in text
        assert "cost_model" in text
        assert "top" in text

    def test_invalid_yaml_uses_defaults(self) -> None:
        s = _routing_settings_from_yaml("null")
        assert s == RoutingSettings()
