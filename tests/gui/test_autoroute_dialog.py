# SPDX-License-Identifier: MIT
"""Tests for the AutorouteDialog router-picker dropdown."""

from __future__ import annotations

from protoboard.domain.routing_settings import RoutingSettings
from protoboard.gui.dialogs.autoroute_dialog import AutorouteDialog


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
