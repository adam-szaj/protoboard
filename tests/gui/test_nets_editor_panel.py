# SPDX-License-Identifier: MIT
"""Tests for NetsEditorPanel — net list, pin list, sync, signals."""

from __future__ import annotations

import uuid

import pytest
from pytestqt.qtbot import QtBot

from PySide6.QtCore import Qt

from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.gui.panels.nets_editor_panel import NetsEditorPanel, _NET_COL_NAME, _ROLE
from protoboard.gui.session import EditorSession


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _add_net(session: EditorSession, name: str) -> Net:
    net = Net(id=uuid.uuid4(), name=name, pins=set())
    session.schematic.nets[net.id] = net
    return net


def _add_instance(session: EditorSession, refdes: str = "R1") -> ComponentInstance:
    inst = ComponentInstance(
        id=uuid.uuid4(), definition_ref="R", refdes=refdes, value="10k"
    )
    session.instances[inst.id] = inst
    return inst


def _panel(qtbot: QtBot) -> NetsEditorPanel:
    panel = NetsEditorPanel()
    qtbot.addWidget(panel)
    return panel


# ---------------------------------------------------------------------------
# Net list
# ---------------------------------------------------------------------------

class TestNetsEditorPanelNetList:
    def test_refresh_shows_nets(
        self, qtbot: QtBot, session: EditorSession
    ) -> None:
        panel = _panel(qtbot)
        _add_net(session, "VCC")
        _add_net(session, "GND")
        panel.refresh(session)
        assert panel._net_tree.topLevelItemCount() == 2

    def test_nets_sorted_alphabetically(
        self, qtbot: QtBot, session: EditorSession
    ) -> None:
        panel = _panel(qtbot)
        _add_net(session, "Z_NET")
        _add_net(session, "A_NET")
        panel.refresh(session)
        assert panel._net_tree.topLevelItem(0).text(0) == "A_NET"
        assert panel._net_tree.topLevelItem(1).text(0) == "Z_NET"

    def test_net_pin_count_shown(
        self, qtbot: QtBot, session: EditorSession
    ) -> None:
        panel = _panel(qtbot)
        inst = _add_instance(session, "R1")
        net = Net(
            id=uuid.uuid4(), name="VCC",
            pins={PinRef(component_id=inst.id, pin_number=1)}
        )
        session.schematic.nets[net.id] = net
        panel.refresh(session)
        item = panel._net_tree.topLevelItem(0)
        assert item.text(1) == "1"

    def test_empty_session_shows_no_nets(
        self, qtbot: QtBot, session: EditorSession
    ) -> None:
        panel = _panel(qtbot)
        panel.refresh(session)
        assert panel._net_tree.topLevelItemCount() == 0

    def test_net_selected_signal_on_click(
        self, qtbot: QtBot, session: EditorSession
    ) -> None:
        panel = _panel(qtbot)
        panel.show()
        net = _add_net(session, "VCC")
        panel.refresh(session)
        received: list[uuid.UUID] = []
        panel.net_selected.connect(received.append)
        item = panel._net_tree.topLevelItem(0)
        assert item is not None
        panel._net_tree.itemClicked.emit(item, 0)
        assert len(received) == 1
        assert received[0] == net.id

    def test_global_net_shown_with_checkmark(
        self, qtbot: QtBot, session: EditorSession
    ) -> None:
        panel = _panel(qtbot)
        net = Net(id=uuid.uuid4(), name="GND", pins=set(), global_net=True)
        session.schematic.nets[net.id] = net
        panel.refresh(session)
        item = panel._net_tree.topLevelItem(0)
        assert item is not None
        assert "✓" in item.text(3)


# ---------------------------------------------------------------------------
# Pin list
# ---------------------------------------------------------------------------

class TestNetsEditorPanelPinList:
    def test_pins_shown_per_instance(
        self, qtbot: QtBot, session: EditorSession
    ) -> None:
        panel = _panel(qtbot)
        _add_instance(session, "R1")
        panel.refresh(session)
        # R has 2 pins; should show one parent + 2 children.
        assert panel._pin_tree.topLevelItemCount() == 1
        parent = panel._pin_tree.topLevelItem(0)
        assert parent.childCount() == 2

    def test_pin_shows_net_name_when_assigned(
        self, qtbot: QtBot, session: EditorSession
    ) -> None:
        panel = _panel(qtbot)
        inst = _add_instance(session, "R1")
        net = Net(
            id=uuid.uuid4(), name="VCC",
            pins={PinRef(component_id=inst.id, pin_number=1)}
        )
        session.schematic.nets[net.id] = net
        panel.refresh(session)
        parent = panel._pin_tree.topLevelItem(0)
        assert parent is not None
        # Find pin 1's item.
        pin1_item = parent.child(0)
        assert pin1_item is not None
        assert pin1_item.text(2) == "VCC"

    def test_unassigned_pin_shows_dash(
        self, qtbot: QtBot, session: EditorSession
    ) -> None:
        panel = _panel(qtbot)
        _add_instance(session, "R1")
        panel.refresh(session)
        parent = panel._pin_tree.topLevelItem(0)
        pin_item = parent.child(0)
        assert pin_item is not None
        assert pin_item.text(2) == "—"

    def test_multiple_instances_all_shown(
        self, qtbot: QtBot, session: EditorSession
    ) -> None:
        panel = _panel(qtbot)
        _add_instance(session, "R1")
        _add_instance(session, "R2")
        panel.refresh(session)
        assert panel._pin_tree.topLevelItemCount() == 2


# ---------------------------------------------------------------------------
# sync_selection
# ---------------------------------------------------------------------------

class TestNetsEditorPanelSyncSelection:
    def test_sync_selects_matching_net_row(
        self, qtbot: QtBot, session: EditorSession
    ) -> None:
        panel = _panel(qtbot)
        panel.show()
        net = _add_net(session, "VCC")
        panel.refresh(session)
        panel.sync_selection(frozenset(), frozenset(), net.id)
        selected = panel._net_tree.selectedItems()
        assert len(selected) == 1
        assert selected[0].data(_NET_COL_NAME, _ROLE) == str(net.id)

    def test_sync_with_none_clears_selection(
        self, qtbot: QtBot, session: EditorSession
    ) -> None:
        panel = _panel(qtbot)
        panel.show()
        net = _add_net(session, "VCC")
        panel.refresh(session)
        # First select something.
        panel.sync_selection(frozenset(), frozenset(), net.id)
        # Then clear.
        panel.sync_selection(frozenset(), frozenset(), None)
        assert panel._net_tree.selectedItems() == []

    def test_sync_noop_when_hidden(
        self, qtbot: QtBot, session: EditorSession
    ) -> None:
        panel = _panel(qtbot)
        # Panel is not shown.
        net = _add_net(session, "VCC")
        panel.refresh(session)
        panel.sync_selection(frozenset(), frozenset(), net.id)
        assert panel._net_tree.selectedItems() == []


# ---------------------------------------------------------------------------
# signals — nets_changed
# ---------------------------------------------------------------------------

class TestNetsEditorPanelSignals:
    def test_nets_changed_exists(
        self, qtbot: QtBot, session: EditorSession
    ) -> None:
        panel = _panel(qtbot)
        received: list[None] = []
        panel.nets_changed.connect(lambda: received.append(None))
        panel.nets_changed.emit()
        assert len(received) == 1


class TestAssignFromBoardClick:
    """_show_assign_dialog must not dead-end when no net exists: it offers to
    create one, then assigns the (board-clicked) pin to it."""

    def test_no_nets_creates_then_assigns(
        self, qtbot: QtBot, session: EditorSession, monkeypatch: object
    ) -> None:
        from PySide6.QtWidgets import QInputDialog, QMessageBox

        from protoboard.domain.schematic.net import PinRef

        panel = _panel(qtbot)
        inst = _add_instance(session)
        panel.refresh(session)
        assert session.schematic.nets == {}  # precondition: no nets

        monkeypatch.setattr(
            QMessageBox, "question",
            staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
        )
        monkeypatch.setattr(
            QInputDialog, "getText",
            staticmethod(lambda *a, **k: ("MyNet", True)),
        )
        # Board-click path: assign a specific pin with no panel selection.
        panel._show_assign_dialog(inst.id, 1)

        nets = list(session.schematic.nets.values())
        assert len(nets) == 1
        assert nets[0].name == "MyNet"
        assert PinRef(component_id=inst.id, pin_number=1) in nets[0].pins

    def test_no_nets_cancel_does_nothing(
        self, qtbot: QtBot, session: EditorSession, monkeypatch: object
    ) -> None:
        from PySide6.QtWidgets import QMessageBox

        panel = _panel(qtbot)
        inst = _add_instance(session)
        panel.refresh(session)
        monkeypatch.setattr(
            QMessageBox, "question",
            staticmethod(lambda *a, **k: QMessageBox.StandardButton.No),
        )
        panel._show_assign_dialog(inst.id, 1)
        assert session.schematic.nets == {}  # declined → no net created
