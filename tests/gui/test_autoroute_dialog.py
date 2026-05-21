# SPDX-License-Identifier: MIT
"""Tests for the AutorouteDialog router-picker dropdown."""

from __future__ import annotations

from protoboard.domain.routing_settings import RoutingSettings
from protoboard.gui.dialogs.autoroute_dialog import AutorouteDialog
from protoboard.services.routing.optimizer import (
    OptimizerCapabilities,
    registry as optimizer_registry,
)


class TestRouterDropdown:
    def test_dropdown_lists_builtin_astar(self, qtbot) -> None:
        dlg = AutorouteDialog(RoutingSettings())
        qtbot.addWidget(dlg)
        names = [
            dlg._combo_router.itemData(i)
            for i in range(dlg._combo_router.count())
        ]
        assert "astar" in names

    def test_dropdown_defaults_to_astar(self, qtbot) -> None:
        dlg = AutorouteDialog(RoutingSettings())
        qtbot.addWidget(dlg)
        assert dlg.router_name() == "astar"

    def test_router_name_reflects_selection(self, qtbot) -> None:
        dlg = AutorouteDialog(RoutingSettings())
        qtbot.addWidget(dlg)
        # Switching to the 'astar' entry (the only one in the default test
        # environment) must keep the accessor in sync.
        idx = dlg._combo_router.findData("astar")
        assert idx >= 0
        dlg._combo_router.setCurrentIndex(idx)
        assert dlg.router_name() == "astar"


class _FakePluginOptimizer:
    name = "fake-side-balancer"
    display_name = "Side balancer (fake)"

    def optimize(self, request, result, sink):
        return result

    def capabilities(self) -> OptimizerCapabilities:
        return OptimizerCapabilities(
            rebalances_sides=True, notes="redistributes wires between layers"
        )


class TestOptimizerCheckboxes:
    """The dialog must surface registered plugin optimizers as checkboxes."""

    def test_no_plugins_returns_none_for_selected(self, qtbot) -> None:
        """Default environment has only the built-in 'simplify', which is
        hidden from the picker — so selected_optimizers() returns None."""
        dlg = AutorouteDialog(RoutingSettings())
        qtbot.addWidget(dlg)
        assert dlg.selected_optimizers() is None

    def test_plugin_appears_as_checkbox(self, qtbot) -> None:
        optimizer_registry.register(_FakePluginOptimizer())
        try:
            dlg = AutorouteDialog(RoutingSettings())
            qtbot.addWidget(dlg)
            assert "fake-side-balancer" in dlg._optimizer_checks
            box = dlg._optimizer_checks["fake-side-balancer"]
            assert box.text() == "Side balancer (fake)"
            # Tooltip propagated from capabilities.notes.
            assert "redistributes" in box.toolTip()
            assert not box.isChecked()  # opt-in
        finally:
            optimizer_registry.optimizers.pop("fake-side-balancer", None)

    def test_selected_optimizers_reflects_checked_boxes(self, qtbot) -> None:
        optimizer_registry.register(_FakePluginOptimizer())
        try:
            dlg = AutorouteDialog(RoutingSettings())
            qtbot.addWidget(dlg)
            # Unchecked → empty list (NOT None — None is reserved for
            # "no plugins registered at all").
            assert dlg.selected_optimizers() == []

            dlg._optimizer_checks["fake-side-balancer"].setChecked(True)
            assert dlg.selected_optimizers() == ["fake-side-balancer"]
        finally:
            optimizer_registry.optimizers.pop("fake-side-balancer", None)

    def test_builtin_simplify_not_listed(self, qtbot) -> None:
        """The legacy 'simplify' built-in is exposed via the dedicated
        checkbox above; do not duplicate it in the plugin section."""
        import protoboard.services.routing.builtin_optimizers  # noqa: F401
        dlg = AutorouteDialog(RoutingSettings())
        qtbot.addWidget(dlg)
        assert "simplify" not in dlg._optimizer_checks
