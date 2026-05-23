# SPDX-License-Identifier: MIT
"""Tests for TreePanelMixin and panels that use it."""

from __future__ import annotations

import uuid

import pytest
from pytestqt.qtbot import QtBot

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTreeWidgetItem, QWidget, QVBoxLayout, QTreeWidget

from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.geometry import Point
from protoboard.gui.panels.tree_panel_mixin import TreePanelMixin
from protoboard.gui.panels.netlist_panel import NetlistPanel
from protoboard.gui.panels.components_panel import ComponentsPanel
from protoboard.gui.panels.net_inspector_panel import NetInspectorPanel
from protoboard.gui.session import EditorSession


# ---------------------------------------------------------------------------
# Minimal concrete implementation for unit-testing the mixin in isolation
# ---------------------------------------------------------------------------

class _SimplePanel(TreePanelMixin, QWidget):
    """Minimal panel: one top-level item per component instance."""

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["ID"])
        layout.addWidget(self._tree)

    def _rebuild(self) -> None:
        self._tree.clear()
        if self._session is None:
            return
        for inst_id in self._session.instances:
            item = QTreeWidgetItem([str(inst_id)])
            item.setData(0, Qt.ItemDataRole.UserRole, str(inst_id))
            self._tree.addTopLevelItem(item)


def _make_instance(session: EditorSession, refdes: str = "R1") -> ComponentInstance:
    inst = ComponentInstance(
        id=uuid.uuid4(), definition_ref="R", refdes=refdes, value="10k"
    )
    session.instances[inst.id] = inst
    return inst


# ---------------------------------------------------------------------------
# TreePanelMixin unit tests
# ---------------------------------------------------------------------------

class TestTreePanelMixin:
    def test_refresh_calls_rebuild(self, qtbot: QtBot, session: EditorSession) -> None:
        panel = _SimplePanel()
        qtbot.addWidget(panel)
        assert panel._tree.topLevelItemCount() == 0

        _make_instance(session, "R1")
        panel.refresh(session)
        assert panel._tree.topLevelItemCount() == 1

    def test_refresh_stores_session(self, qtbot: QtBot, session: EditorSession) -> None:
        panel = _SimplePanel()
        qtbot.addWidget(panel)
        panel.refresh(session)
        assert panel._session is session

    def test_find_item_returns_none_when_empty(
        self, qtbot: QtBot, session: EditorSession
    ) -> None:
        panel = _SimplePanel()
        qtbot.addWidget(panel)
        panel.refresh(session)
        assert panel._find_item(str(uuid.uuid4())) is None

    def test_find_item_finds_top_level(
        self, qtbot: QtBot, session: EditorSession
    ) -> None:
        panel = _SimplePanel()
        qtbot.addWidget(panel)
        inst = _make_instance(session)
        panel.refresh(session)
        item = panel._find_item(str(inst.id))
        assert item is not None
        assert item.data(0, Qt.ItemDataRole.UserRole) == str(inst.id)

    def test_find_item_finds_nested(
        self, qtbot: QtBot, session: EditorSession
    ) -> None:
        panel = _SimplePanel()
        qtbot.addWidget(panel)
        panel.refresh(session)
        # Manually add a nested item with known UUID.
        child_id = uuid.uuid4()
        parent = QTreeWidgetItem(["parent"])
        child = QTreeWidgetItem(["child"])
        child.setData(0, Qt.ItemDataRole.UserRole, str(child_id))
        parent.addChild(child)
        panel._tree.addTopLevelItem(parent)

        found = panel._find_item(str(child_id))
        assert found is not None

    def test_sync_selection_selects_matching_row(
        self, qtbot: QtBot, session: EditorSession
    ) -> None:
        panel = _SimplePanel()
        qtbot.addWidget(panel)
        panel.show()
        inst = _make_instance(session)
        panel.refresh(session)

        panel.sync_selection(frozenset({inst.id}), frozenset(), None)
        selected = panel._tree.selectedItems()
        assert len(selected) == 1
        assert selected[0].data(0, Qt.ItemDataRole.UserRole) == str(inst.id)

    def test_sync_selection_clears_previous(
        self, qtbot: QtBot, session: EditorSession
    ) -> None:
        panel = _SimplePanel()
        qtbot.addWidget(panel)
        panel.show()
        inst1 = _make_instance(session, "R1")
        inst2 = _make_instance(session, "R2")
        panel.refresh(session)

        panel.sync_selection(frozenset({inst1.id}), frozenset(), None)
        assert len(panel._tree.selectedItems()) == 1

        panel.sync_selection(frozenset({inst2.id}), frozenset(), None)
        selected = panel._tree.selectedItems()
        assert len(selected) == 1
        assert selected[0].data(0, Qt.ItemDataRole.UserRole) == str(inst2.id)

    def test_sync_selection_noop_when_hidden(
        self, qtbot: QtBot, session: EditorSession
    ) -> None:
        panel = _SimplePanel()
        qtbot.addWidget(panel)
        # Panel is hidden (not shown) — sync should be a no-op.
        inst = _make_instance(session)
        panel.refresh(session)
        panel.sync_selection(frozenset({inst.id}), frozenset(), None)
        assert panel._tree.selectedItems() == []

    def test_syncing_flag_prevents_reentrance(
        self, qtbot: QtBot, session: EditorSession
    ) -> None:
        panel = _SimplePanel()
        qtbot.addWidget(panel)
        panel.show()
        inst = _make_instance(session)
        panel.refresh(session)

        reentrance_detected: list[bool] = []

        original_find = panel._find_item

        def _spy_find(id_str: str) -> QTreeWidgetItem | None:
            reentrance_detected.append(panel._syncing)
            return original_find(id_str)

        panel._find_item = _spy_find  # type: ignore[method-assign]
        panel.sync_selection(frozenset({inst.id}), frozenset(), None)
        assert reentrance_detected and all(reentrance_detected)


# ---------------------------------------------------------------------------
# NetlistPanel — tree building modes
# ---------------------------------------------------------------------------

class TestNetlistPanel:
    def test_symbol_mode_groups_by_definition(
        self, qtbot: QtBot, session: EditorSession
    ) -> None:
        panel = NetlistPanel()
        qtbot.addWidget(panel)
        _make_instance(session, "R1")
        _make_instance(session, "R2")
        panel.refresh(session)
        # In symbol mode (default=0), there should be one "R" group at top level.
        top_texts = [
            panel._tree.topLevelItem(i).text(0)
            for i in range(panel._tree.topLevelItemCount())
        ]
        assert any("R" in t for t in top_texts)

    def test_flat_mode_lists_each_instance(
        self, qtbot: QtBot, session: EditorSession
    ) -> None:
        panel = NetlistPanel()
        qtbot.addWidget(panel)
        _make_instance(session, "R1")
        _make_instance(session, "R2")
        panel._mode_combo.setCurrentIndex(2)  # flat mode
        panel.refresh(session)
        # Flat mode: one top-level item per instance.
        assert panel._tree.topLevelItemCount() == 2

    def test_unplaced_items_are_grey(
        self, qtbot: QtBot, session: EditorSession
    ) -> None:
        from PySide6.QtGui import QColor
        panel = NetlistPanel()
        qtbot.addWidget(panel)
        inst = _make_instance(session, "R1")
        panel._mode_combo.setCurrentIndex(2)  # flat mode
        panel.refresh(session)
        item = panel._find_item(str(inst.id))
        assert item is not None
        # Unplaced items get grey foreground.
        color: QColor = item.foreground(0).color()
        assert color.red() > 150 and color.green() > 150  # grey-ish

    def test_placed_items_are_green(
        self, qtbot: QtBot, session: EditorSession
    ) -> None:
        from PySide6.QtGui import QColor
        panel = NetlistPanel()
        qtbot.addWidget(panel)
        inst = _make_instance(session, "R1")
        # Place the component.
        session.layout.placements[inst.id] = ComponentPlacement(
            component_id=inst.id, position=Point(0, 0), rotation=0, side="top"
        )
        panel._mode_combo.setCurrentIndex(2)  # flat mode
        panel.refresh(session)
        item = panel._find_item(str(inst.id))
        assert item is not None
        color: QColor = item.foreground(0).color()
        assert color.green() > color.red()  # more green than red

    def test_instance_selected_signal_on_click(
        self, qtbot: QtBot, session: EditorSession
    ) -> None:
        panel = NetlistPanel()
        qtbot.addWidget(panel)
        panel.show()
        inst = _make_instance(session, "R1")
        panel._mode_combo.setCurrentIndex(2)
        panel.refresh(session)
        received: list[uuid.UUID] = []
        panel.instance_selected.connect(received.append)
        item = panel._find_item(str(inst.id))
        assert item is not None
        panel._tree.itemClicked.emit(item, 0)
        assert len(received) == 1
        assert received[0] == inst.id

    def test_sync_selection_highlights_component(
        self, qtbot: QtBot, session: EditorSession
    ) -> None:
        panel = NetlistPanel()
        qtbot.addWidget(panel)
        panel.show()
        inst = _make_instance(session, "R1")
        panel._mode_combo.setCurrentIndex(2)
        panel.refresh(session)
        panel.sync_selection(frozenset({inst.id}), frozenset(), None)
        selected = panel._tree.selectedItems()
        assert len(selected) == 1


# ---------------------------------------------------------------------------
# ComponentsPanel — search and basic behaviour
# ---------------------------------------------------------------------------

class TestComponentsPanel:
    def test_shows_all_instances(
        self, qtbot: QtBot, session: EditorSession
    ) -> None:
        panel = ComponentsPanel()
        qtbot.addWidget(panel)
        _make_instance(session, "R1")
        _make_instance(session, "R2")
        panel.refresh(session)
        assert panel._tree.topLevelItemCount() == 2

    def test_filter_by_refdes(
        self, qtbot: QtBot, session: EditorSession
    ) -> None:
        panel = ComponentsPanel()
        qtbot.addWidget(panel)
        _make_instance(session, "R1")
        _make_instance(session, "C1")
        panel.refresh(session)
        panel._search.setText("R1")
        # After filter, only R1 should be visible.
        assert panel._tree.topLevelItemCount() == 1
        assert "R1" in panel._tree.topLevelItem(0).text(0)

    def test_filter_clears_on_empty_string(
        self, qtbot: QtBot, session: EditorSession
    ) -> None:
        panel = ComponentsPanel()
        qtbot.addWidget(panel)
        _make_instance(session, "R1")
        _make_instance(session, "C1")
        panel.refresh(session)
        panel._search.setText("R1")
        assert panel._tree.topLevelItemCount() == 1
        panel._search.setText("")
        assert panel._tree.topLevelItemCount() == 2

    def test_sync_selection_selects_row(
        self, qtbot: QtBot, session: EditorSession
    ) -> None:
        panel = ComponentsPanel()
        qtbot.addWidget(panel)
        panel.show()
        inst = _make_instance(session, "R1")
        panel.refresh(session)
        panel.sync_selection(frozenset({inst.id}), frozenset(), None)
        assert len(panel._tree.selectedItems()) == 1

    def test_place_btn_disabled_when_nothing_selected(
        self, qtbot: QtBot, session: EditorSession
    ) -> None:
        panel = ComponentsPanel()
        qtbot.addWidget(panel)
        panel.refresh(session)
        assert not panel._place_btn.isEnabled()


# ---------------------------------------------------------------------------
# NetInspectorPanel — basic behaviour
# ---------------------------------------------------------------------------

class TestNetInspectorPanel:
    def test_refresh_populates_nets(
        self, qtbot: QtBot, board_view: object, session: EditorSession
    ) -> None:
        from protoboard.gui.board_view import BoardView
        assert isinstance(board_view, BoardView)
        panel = NetInspectorPanel(board_view)
        qtbot.addWidget(panel)
        # The inspector now skips rebuilds while hidden (perf optimisation —
        # populating a hidden tree wastes O(nets) work on every layout
        # change).  Make it visible so refresh() actually populates.
        panel.show()
        qtbot.waitExposed(panel)
        from protoboard.domain.schematic.net import Net
        net = Net(id=uuid.uuid4(), name="VCC", pins=frozenset())
        session.schematic.nets[net.id] = net
        panel.refresh(session)
        assert panel._tree.topLevelItemCount() == 1
        assert panel._tree.topLevelItem(0).text(0) == "VCC"

    def test_sync_selection_by_net_id(
        self, qtbot: QtBot, board_view: object, session: EditorSession
    ) -> None:
        from protoboard.gui.board_view import BoardView
        assert isinstance(board_view, BoardView)
        panel = NetInspectorPanel(board_view)
        qtbot.addWidget(panel)
        panel.show()
        from protoboard.domain.schematic.net import Net
        net = Net(id=uuid.uuid4(), name="GND", pins=frozenset())
        session.schematic.nets[net.id] = net
        panel.refresh(session)
        panel.sync_selection(frozenset(), frozenset(), net.id)
        selected = panel._tree.selectedItems()
        assert len(selected) == 1
