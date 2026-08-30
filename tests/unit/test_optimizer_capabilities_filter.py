# SPDX-License-Identifier: MIT
"""Pure-function tests for incompatibility_reasons() — capability gating."""

from __future__ import annotations

from protoboard.domain.routing_settings import RoutingSettings
from protoboard.services.routing.optimizer import (
    OptimizerCapabilities,
    incompatibility_reasons,
)


class TestNoCapabilitiesNoConflict:
    def test_empty_capabilities_always_compatible(self) -> None:
        """An optimizer that declares no capabilities is always compatible —
        capability gating only applies to plugins that have opted in."""
        caps = OptimizerCapabilities()
        # Even the most restrictive settings must not gate an empty-caps optimizer.
        rs = RoutingSettings(
            allow_layer_change=False,
            allow_jumpers=False,
            wire_mode="insulated",
        )
        assert incompatibility_reasons(caps, rs) == []

    def test_default_settings_accept_all_capabilities(self) -> None:
        """Defaults allow layer change + jumpers + auto wire-mode → nothing gated."""
        caps = OptimizerCapabilities(
            rebalances_sides=True,
            inserts_jumpers=True,
            removes_jumpers=True,
            insulated_to_bare=True,
            bare_to_insulated=True,
        )
        rs = RoutingSettings()  # defaults
        assert incompatibility_reasons(caps, rs) == []


class TestLayerChangeGating:
    """allow_layer_change=False makes layer-dependent capabilities pointless."""

    def test_rebalances_sides_needs_layer_change(self) -> None:
        caps = OptimizerCapabilities(rebalances_sides=True)
        rs = RoutingSettings(allow_layer_change=False)
        reasons = incompatibility_reasons(caps, rs)
        assert len(reasons) == 1
        assert "allow_layer_change" in reasons[0]
        assert "rebalanc" in reasons[0].lower()

    def test_inserts_jumpers_needs_layer_change(self) -> None:
        caps = OptimizerCapabilities(inserts_jumpers=True)
        rs = RoutingSettings(allow_layer_change=False, allow_jumpers=True)
        reasons = incompatibility_reasons(caps, rs)
        # Only the layer-change reason applies; allow_jumpers is fine here.
        assert any("allow_layer_change" in r for r in reasons)

    def test_removes_jumpers_needs_layer_change(self) -> None:
        caps = OptimizerCapabilities(removes_jumpers=True)
        rs = RoutingSettings(allow_layer_change=False)
        reasons = incompatibility_reasons(caps, rs)
        assert any("allow_layer_change" in r for r in reasons)


class TestJumperGating:
    def test_inserts_jumpers_needs_allow_jumpers(self) -> None:
        caps = OptimizerCapabilities(inserts_jumpers=True)
        rs = RoutingSettings(allow_layer_change=True, allow_jumpers=False)
        reasons = incompatibility_reasons(caps, rs)
        assert any("allow_jumpers" in r for r in reasons)

    def test_removes_jumpers_independent_of_allow_jumpers(self) -> None:
        """Removing jumpers is still useful even when new jumpers are forbidden —
        the project may already have jumpers from a previous route."""
        caps = OptimizerCapabilities(removes_jumpers=True)
        rs = RoutingSettings(allow_jumpers=False)  # but layer_change default True
        # No jumper-related reason expected; only the layer-change one if any.
        reasons = incompatibility_reasons(caps, rs)
        assert not any("allow_jumpers" in r for r in reasons)


class TestWireModeGating:
    def test_insulated_mode_blocks_bare_conversions(self) -> None:
        caps = OptimizerCapabilities(
            insulated_to_bare=True,
            bare_to_insulated=True,
        )
        rs = RoutingSettings(wire_mode="insulated")
        reasons = incompatibility_reasons(caps, rs)
        # Both conversions blocked because pure insulated mode has no bare wires.
        assert len(reasons) == 2

    def test_bare_diagonal_mode_allows_conversions(self) -> None:
        caps = OptimizerCapabilities(insulated_to_bare=True, bare_to_insulated=True)
        rs = RoutingSettings(wire_mode="bare_diagonal")
        assert incompatibility_reasons(caps, rs) == []

    def test_auto_mode_allows_conversions(self) -> None:
        caps = OptimizerCapabilities(insulated_to_bare=True, bare_to_insulated=True)
        rs = RoutingSettings(wire_mode="auto")
        assert incompatibility_reasons(caps, rs) == []


class TestMultipleReasonsAccumulate:
    def test_multiple_conflicts_report_separately(self) -> None:
        caps = OptimizerCapabilities(
            rebalances_sides=True,
            inserts_jumpers=True,
            insulated_to_bare=True,
        )
        rs = RoutingSettings(
            allow_layer_change=False,
            allow_jumpers=False,
            wire_mode="insulated",
        )
        reasons = incompatibility_reasons(caps, rs)
        # rebalances_sides: needs layer_change
        # inserts_jumpers: needs layer_change AND allow_jumpers (two reasons)
        # insulated_to_bare: needs non-insulated wire_mode
        assert len(reasons) >= 3
