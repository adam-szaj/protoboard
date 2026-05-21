# SPDX-License-Identifier: MIT
"""Tests for the AutorouteDialog router-picker dropdown and plugin chain UI."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QListWidgetItem

from protoboard.domain.routing_settings import RoutingSettings
from protoboard.gui.dialogs.autoroute_dialog import AutorouteDialog
from protoboard.services.routing.optimizer import (
    OptimizerCapabilities,
    registry as optimizer_registry,
)


def _item_for(dlg: AutorouteDialog, opt_name: str) -> QListWidgetItem | None:
    """Find the QListWidgetItem with the given optimizer name as UserRole data."""
    lst = dlg._optimizer_list
    if lst is None:
        return None
    for i in range(lst.count()):
        item = lst.item(i)
        if item is not None and item.data(Qt.ItemDataRole.UserRole) == opt_name:
            return item
    return None


def _row_order(dlg: AutorouteDialog) -> list[str]:
    """Return optimizer names in the list's current top-to-bottom order."""
    lst = dlg._optimizer_list
    if lst is None:
        return []
    out: list[str] = []
    for i in range(lst.count()):
        item = lst.item(i)
        if item is not None:
            name = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(name, str):
                out.append(name)
    return out


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


class _AnotherFakeOptimizer:
    name = "fake-jumper-min"
    display_name = "Jumper minimiser (fake)"

    def optimize(self, request, result, sink):
        return result

    def capabilities(self) -> OptimizerCapabilities:
        return OptimizerCapabilities(removes_jumpers=True, notes="kills jumpers")


class TestOptimizerChainList:
    """The plugin chain is rendered as a drag-reorderable QListWidget."""

    def test_no_plugins_returns_none_for_selected(self, qtbot) -> None:
        dlg = AutorouteDialog(RoutingSettings())
        qtbot.addWidget(dlg)
        assert dlg.selected_optimizers() is None

    def test_plugin_appears_as_row_with_tooltip(self, qtbot) -> None:
        optimizer_registry.register(_FakePluginOptimizer())
        try:
            dlg = AutorouteDialog(RoutingSettings())
            qtbot.addWidget(dlg)
            item = _item_for(dlg, "fake-side-balancer")
            assert item is not None
            assert item.text() == "Side balancer (fake)"
            assert "redistributes" in item.toolTip()
            assert item.checkState() == Qt.CheckState.Unchecked  # opt-in
        finally:
            optimizer_registry.optimizers.pop("fake-side-balancer", None)

    def test_selected_optimizers_reflects_checked_rows(self, qtbot) -> None:
        optimizer_registry.register(_FakePluginOptimizer())
        try:
            dlg = AutorouteDialog(RoutingSettings())
            qtbot.addWidget(dlg)
            # Unchecked → empty list (NOT None — None is reserved for
            # "no plugins registered at all").
            assert dlg.selected_optimizers() == []

            _item_for(dlg, "fake-side-balancer").setCheckState(Qt.CheckState.Checked)  # type: ignore[union-attr]
            assert dlg.selected_optimizers() == ["fake-side-balancer"]
        finally:
            optimizer_registry.optimizers.pop("fake-side-balancer", None)

    def test_builtin_simplify_not_listed(self, qtbot) -> None:
        """The legacy 'simplify' built-in is exposed via the dedicated
        checkbox above; do not duplicate it in the plugin section."""
        import protoboard.services.routing.builtin_optimizers  # noqa: F401
        dlg = AutorouteDialog(RoutingSettings())
        qtbot.addWidget(dlg)
        assert _item_for(dlg, "simplify") is None


class TestPersistedChain:
    """The list must round-trip the persisted ``RoutingSettings.optimizer_chain``."""

    def test_persisted_optimizer_is_checked_at_open(self, qtbot) -> None:
        optimizer_registry.register(_FakePluginOptimizer())
        try:
            rs = RoutingSettings(optimizer_chain=("fake-side-balancer",))
            dlg = AutorouteDialog(rs)
            qtbot.addWidget(dlg)
            item = _item_for(dlg, "fake-side-balancer")
            assert item is not None
            assert item.checkState() == Qt.CheckState.Checked
        finally:
            optimizer_registry.optimizers.pop("fake-side-balancer", None)

    def test_unpersisted_optimizer_is_unchecked(self, qtbot) -> None:
        optimizer_registry.register(_FakePluginOptimizer())
        try:
            dlg = AutorouteDialog(RoutingSettings())
            qtbot.addWidget(dlg)
            item = _item_for(dlg, "fake-side-balancer")
            assert item is not None
            assert item.checkState() == Qt.CheckState.Unchecked
        finally:
            optimizer_registry.optimizers.pop("fake-side-balancer", None)

    def test_routing_settings_emits_chain_from_checked_rows(self, qtbot) -> None:
        optimizer_registry.register(_FakePluginOptimizer())
        try:
            dlg = AutorouteDialog(RoutingSettings())
            qtbot.addWidget(dlg)
            _item_for(dlg, "fake-side-balancer").setCheckState(Qt.CheckState.Checked)  # type: ignore[union-attr]
            rs = dlg.routing_settings()
            assert rs.optimizer_chain == ("fake-side-balancer",)
        finally:
            optimizer_registry.optimizers.pop("fake-side-balancer", None)

    def test_chain_survives_open_close_open(self, qtbot) -> None:
        optimizer_registry.register(_FakePluginOptimizer())
        try:
            dlg1 = AutorouteDialog(RoutingSettings())
            qtbot.addWidget(dlg1)
            _item_for(dlg1, "fake-side-balancer").setCheckState(Qt.CheckState.Checked)  # type: ignore[union-attr]
            rs1 = dlg1.routing_settings()

            dlg2 = AutorouteDialog(rs1)
            qtbot.addWidget(dlg2)
            item = _item_for(dlg2, "fake-side-balancer")
            assert item is not None
            assert item.checkState() == Qt.CheckState.Checked
        finally:
            optimizer_registry.optimizers.pop("fake-side-balancer", None)


class TestReorderableChain:
    """User can drag rows to reorder the chain; the emitted chain follows."""

    def test_persisted_order_drives_row_layout(self, qtbot) -> None:
        """Row order at open == saved chain order, with unchecked extras at the bottom."""
        optimizer_registry.register(_FakePluginOptimizer())
        optimizer_registry.register(_AnotherFakeOptimizer())
        try:
            # Persist the chain in a non-alphabetical order so we can prove
            # the dialog respects the saved sequence (not just `sorted()`).
            rs = RoutingSettings(
                optimizer_chain=("fake-side-balancer", "fake-jumper-min"),
            )
            dlg = AutorouteDialog(rs)
            qtbot.addWidget(dlg)
            order = _row_order(dlg)
            assert order == ["fake-side-balancer", "fake-jumper-min"]
        finally:
            optimizer_registry.optimizers.pop("fake-side-balancer", None)
            optimizer_registry.optimizers.pop("fake-jumper-min", None)

    def test_reverse_order_persists_through_chain(self, qtbot) -> None:
        """Saving with the reversed chain and reloading must reproduce that order."""
        optimizer_registry.register(_FakePluginOptimizer())
        optimizer_registry.register(_AnotherFakeOptimizer())
        try:
            rs = RoutingSettings(
                optimizer_chain=("fake-jumper-min", "fake-side-balancer"),
            )
            dlg = AutorouteDialog(rs)
            qtbot.addWidget(dlg)
            # Both rows are pre-checked by the persisted chain.
            assert _row_order(dlg) == ["fake-jumper-min", "fake-side-balancer"]
            chain = dlg.selected_optimizers()
            assert chain == ["fake-jumper-min", "fake-side-balancer"]
        finally:
            optimizer_registry.optimizers.pop("fake-side-balancer", None)
            optimizer_registry.optimizers.pop("fake-jumper-min", None)

    def test_unregistered_plugins_are_appended_unchecked(self, qtbot) -> None:
        """A persisted name that no longer matches a registered plugin must
        not break the dialog — and any plugin that *is* registered but not
        in the chain appears unchecked below the persisted ones."""
        optimizer_registry.register(_FakePluginOptimizer())
        optimizer_registry.register(_AnotherFakeOptimizer())
        try:
            rs = RoutingSettings(optimizer_chain=("fake-jumper-min",))
            dlg = AutorouteDialog(rs)
            qtbot.addWidget(dlg)
            # Persisted row first.
            assert _row_order(dlg)[0] == "fake-jumper-min"
            # The other plugin is present, but unchecked.
            other = _item_for(dlg, "fake-side-balancer")
            assert other is not None
            assert other.checkState() == Qt.CheckState.Unchecked
            # Only the persisted one shows up in the emitted chain.
            assert dlg.selected_optimizers() == ["fake-jumper-min"]
        finally:
            optimizer_registry.optimizers.pop("fake-side-balancer", None)
            optimizer_registry.optimizers.pop("fake-jumper-min", None)

    def test_internal_move_drag_flag_set(self, qtbot) -> None:
        """The list must be configured for internal drag-and-drop reorder."""
        from PySide6.QtWidgets import QAbstractItemView
        optimizer_registry.register(_FakePluginOptimizer())
        try:
            dlg = AutorouteDialog(RoutingSettings())
            qtbot.addWidget(dlg)
            assert dlg._optimizer_list is not None
            assert (
                dlg._optimizer_list.dragDropMode()
                == QAbstractItemView.DragDropMode.InternalMove
            )
        finally:
            optimizer_registry.optimizers.pop("fake-side-balancer", None)

    def test_incompatible_row_is_disabled_and_unchecked(self, qtbot) -> None:
        """When routing settings make a plugin a no-op, the row must be
        disabled, force-unchecked, and the conflict reason in the tooltip."""
        class _JumperPlugin:
            name = "jumper-inserter"
            display_name = "Jumper inserter"
            def optimize(self, *_a, **_k): ...
            def capabilities(self):
                return OptimizerCapabilities(inserts_jumpers=True)

        optimizer_registry.register(_JumperPlugin())  # type: ignore[arg-type]
        try:
            # Persist the chain to also exercise the "incompatible but
            # persisted" path — the row should still be force-unchecked.
            rs = RoutingSettings(
                allow_jumpers=False,
                optimizer_chain=("jumper-inserter",),
            )
            dlg = AutorouteDialog(rs)
            qtbot.addWidget(dlg)
            item = _item_for(dlg, "jumper-inserter")
            assert item is not None
            assert not (item.flags() & Qt.ItemFlag.ItemIsEnabled)
            assert item.checkState() == Qt.CheckState.Unchecked
            assert "allow_jumpers" in item.toolTip()
            # And the chain emitted from the dialog must not include it.
            assert dlg.selected_optimizers() == []
        finally:
            optimizer_registry.optimizers.pop("jumper-inserter", None)

    def test_compatible_row_remains_enabled(self, qtbot) -> None:
        """Same plugin, settings that permit jumpers — row must be clickable."""
        class _JumperPlugin:
            name = "jumper-inserter"
            display_name = "Jumper inserter"
            def optimize(self, *_a, **_k): ...
            def capabilities(self):
                return OptimizerCapabilities(inserts_jumpers=True)

        optimizer_registry.register(_JumperPlugin())  # type: ignore[arg-type]
        try:
            rs = RoutingSettings(allow_layer_change=True, allow_jumpers=True)
            dlg = AutorouteDialog(rs)
            qtbot.addWidget(dlg)
            item = _item_for(dlg, "jumper-inserter")
            assert item is not None
            assert item.flags() & Qt.ItemFlag.ItemIsEnabled
            assert item.flags() & Qt.ItemFlag.ItemIsUserCheckable
        finally:
            optimizer_registry.optimizers.pop("jumper-inserter", None)

    def test_chain_reflects_manually_moved_row(self, qtbot) -> None:
        """Simulate a drag by mutating the list directly — selected_optimizers()
        must reflect the new top-to-bottom order."""
        optimizer_registry.register(_FakePluginOptimizer())
        optimizer_registry.register(_AnotherFakeOptimizer())
        try:
            rs = RoutingSettings(
                optimizer_chain=("fake-side-balancer", "fake-jumper-min"),
            )
            dlg = AutorouteDialog(rs)
            qtbot.addWidget(dlg)
            assert dlg.selected_optimizers() == [
                "fake-side-balancer", "fake-jumper-min",
            ]

            # Simulate user dragging the second row above the first.
            lst = dlg._optimizer_list
            assert lst is not None
            top_item = lst.takeItem(1)
            lst.insertItem(0, top_item)

            assert dlg.selected_optimizers() == [
                "fake-jumper-min", "fake-side-balancer",
            ]
        finally:
            optimizer_registry.optimizers.pop("fake-side-balancer", None)
            optimizer_registry.optimizers.pop("fake-jumper-min", None)
