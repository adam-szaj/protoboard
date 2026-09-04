# SPDX-License-Identifier: MIT
"""Tests for RoutingSettings domain model and persistence round-trip."""

from __future__ import annotations

from protoboard.domain.routing.simplify_mode import (
    SimplifyMode,
    simplify_mode_from_value,
)
from protoboard.domain.routing_settings import RoutingSettings
from protoboard.services.persistence.project_yaml import (
    _routing_settings_from_yaml,
    _routing_settings_to_yaml,
)


def test_routing_settings_use_domain_owned_types() -> None:
    from protoboard.domain.routing.cost_model import RoutingCostModel
    from protoboard.domain.routing.simplify_mode import SimplifyMode

    settings = RoutingSettings()
    assert isinstance(settings.cost_model(), RoutingCostModel)
    assert settings.simplify_mode is SimplifyMode.BARE_PARITY


def test_simplify_mode_from_value_accepts_known_values() -> None:
    assert simplify_mode_from_value("chebyshev") is SimplifyMode.CHEBYSHEV
    assert simplify_mode_from_value("bare_parity") is SimplifyMode.BARE_PARITY


def test_simplify_mode_from_value_defaults_for_unknown_values() -> None:
    assert simplify_mode_from_value("unknown") is SimplifyMode.BARE_PARITY
    assert simplify_mode_from_value(None) is SimplifyMode.BARE_PARITY


class TestRoutingSettingsModel:
    def test_defaults_are_sane(self) -> None:
        s = RoutingSettings()
        assert s.preferred_side == "bottom"
        assert s.allow_layer_change is True
        assert s.max_rip_up_passes == 3
        assert s.max_optimize_iterations == 10
        # Cost weights now live in cost_preset + cost_overrides.
        assert s.cost_preset == "default"
        assert dict(s.cost_overrides) == {}
        # Resolved cost model still reflects historical defaults.
        cm = s.cost_model()
        assert cm.direction_change == 1.0
        assert cm.layer_change == 2.0
        assert cm.under_component_top == 3.0

    def test_cost_model_applies_overrides(self) -> None:
        s = RoutingSettings(
            cost_overrides={"direction_change": 2.5, "layer_change": 4.0}
        )
        cm = s.cost_model()
        assert cm.direction_change == 2.5
        assert cm.layer_change == 4.0
        # Unchanged knob keeps its preset default — granular ib_*
        # off-pad-same-side carries the historical crossing cost.
        assert cm.get("crossing_ib_off_pad_same_side") == 5.0

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
            cost_overrides={"direction_change": 2.0, "layer_change": 1.5},
        )
        yaml_text = _routing_settings_to_yaml(s)
        s2 = _routing_settings_from_yaml(yaml_text)
        assert s == s2

    def test_missing_keys_use_defaults(self) -> None:
        s = _routing_settings_from_yaml("{}")
        assert s == RoutingSettings()

    def test_legacy_cost_model_block_folds_into_overrides(self) -> None:
        # Old-format YAML still loads — cost_model: {...} entries become
        # cost_overrides so the project keeps its tuned values.
        yaml_text = "cost_model:\n  direction_change: 3.0\n"
        s = _routing_settings_from_yaml(yaml_text)
        cm = s.cost_model()
        assert cm.direction_change == 3.0
        assert cm.layer_change == 2.0  # default

    def test_yaml_contains_routing_section(self) -> None:
        s = RoutingSettings(preferred_side="top")
        text = _routing_settings_to_yaml(s)
        assert "preferred_side" in text
        assert "cost_preset" in text
        assert "top" in text

    def test_invalid_yaml_uses_defaults(self) -> None:
        s = _routing_settings_from_yaml("null")
        assert s == RoutingSettings()


class TestOptimizerChainPersistence:
    """The optimizer plugin chain must round-trip through routing.yaml."""

    def test_empty_chain_omitted_from_yaml(self) -> None:
        """Defaults stay clean — no optimizer_chain key for vanilla projects."""
        s = RoutingSettings()
        text = _routing_settings_to_yaml(s)
        assert "optimizer_chain" not in text

    def test_non_empty_chain_persists(self) -> None:
        s = RoutingSettings(optimizer_chain=("side-balancer", "jumper-min"))
        text = _routing_settings_to_yaml(s)
        assert "optimizer_chain" in text
        s2 = _routing_settings_from_yaml(text)
        assert s2.optimizer_chain == ("side-balancer", "jumper-min")

    def test_chain_order_preserved(self) -> None:
        original = ("c", "b", "a")
        s = RoutingSettings(optimizer_chain=original)
        s2 = _routing_settings_from_yaml(_routing_settings_to_yaml(s))
        assert s2.optimizer_chain == original

    def test_missing_chain_key_loads_empty(self) -> None:
        """An older project file with no optimizer_chain key still loads cleanly."""
        s = _routing_settings_from_yaml("preferred_side: bottom\n")
        assert s.optimizer_chain == ()

    def test_malformed_chain_falls_back_to_empty(self) -> None:
        """Defensive — if someone hand-edits routing.yaml with garbage."""
        s = _routing_settings_from_yaml("optimizer_chain: 42\n")
        assert s.optimizer_chain == ()
        s2 = _routing_settings_from_yaml("optimizer_chain: ['ok', 99, 'still']\n")
        assert s2.optimizer_chain == ()  # mixed list — reject wholesale


class TestRouterNamePersistence:
    """The user's last router selection is persisted per project."""

    def test_router_name_roundtrip(self) -> None:
        s = RoutingSettings(router_name="astar_enhanced")
        loaded = _routing_settings_from_yaml(_routing_settings_to_yaml(s))
        assert loaded.router_name == "astar_enhanced"

    def test_missing_router_name_key_defaults_to_astar(self) -> None:
        """Older projects (or hand-edited YAML) without the key still load."""
        s = _routing_settings_from_yaml("preferred_side: bottom\n")
        assert s.router_name == "astar"

    def test_unknown_router_name_persists_verbatim(self) -> None:
        """Plugins extend the registry at runtime — the loader must not
        reject names it doesn't recognise.  The dialog handles missing
        plugins by falling back to 'astar' at display time, not here."""
        s = RoutingSettings(router_name="future_plugin")
        loaded = _routing_settings_from_yaml(_routing_settings_to_yaml(s))
        assert loaded.router_name == "future_plugin"

    def test_garbage_router_name_coerced_to_string(self) -> None:
        """Defensive parity with wire_mode's parsing — if someone hand-
        edits the YAML to put an int there, the loader coerces."""
        s = _routing_settings_from_yaml("router_name: 42\n")
        assert s.router_name == "42"
