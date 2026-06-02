# SPDX-License-Identifier: MIT
"""Tests for DragDisconnectDialog (Reroute / Drop / Cancel + opt-out)."""

from __future__ import annotations

from protoboard.gui.dialogs.drag_disconnect_dialog import (
    DragDisconnectAction,
    DragDisconnectDialog,
)


class TestDragDisconnectDialog:
    def test_default_action_is_cancel(self, qtbot) -> None:
        dlg = DragDisconnectDialog(n_wires=1, affected_nets=["VCC"])
        qtbot.addWidget(dlg)
        # Until any button is pressed, chosen_action() returns CANCEL.
        assert dlg.chosen_action() == DragDisconnectAction.CANCEL
        assert dlg.session_opt_out() is False

    def test_reroute_button_sets_reroute_action(self, qtbot) -> None:
        dlg = DragDisconnectDialog(n_wires=1, affected_nets=["VCC"])
        qtbot.addWidget(dlg)
        # Find the Auto-reroute button (default).
        from PySide6.QtWidgets import QPushButton
        reroute = next(
            b for b in dlg.findChildren(QPushButton)
            if b.text() == "Auto-reroute"
        )
        reroute.click()
        assert dlg.chosen_action() == DragDisconnectAction.REROUTE
        assert dlg.session_opt_out() is False

    def test_drop_button_sets_drop_action(self, qtbot) -> None:
        dlg = DragDisconnectDialog(n_wires=2, affected_nets=["VCC", "GND"])
        qtbot.addWidget(dlg)
        from PySide6.QtWidgets import QPushButton
        drop = next(
            b for b in dlg.findChildren(QPushButton) if b.text() == "Drop"
        )
        drop.click()
        assert dlg.chosen_action() == DragDisconnectAction.DROP

    def test_cancel_button_keeps_cancel(self, qtbot) -> None:
        dlg = DragDisconnectDialog(n_wires=1, affected_nets=[])
        qtbot.addWidget(dlg)
        from PySide6.QtWidgets import QPushButton
        cancel = next(
            b for b in dlg.findChildren(QPushButton) if b.text() == "Cancel"
        )
        cancel.click()
        assert dlg.chosen_action() == DragDisconnectAction.CANCEL

    def test_opt_out_checkbox_propagates_on_accept(self, qtbot) -> None:
        dlg = DragDisconnectDialog(n_wires=1, affected_nets=["SIG"])
        qtbot.addWidget(dlg)
        from PySide6.QtWidgets import QCheckBox, QPushButton
        cb = next(iter(dlg.findChildren(QCheckBox)))
        cb.setChecked(True)
        drop = next(
            b for b in dlg.findChildren(QPushButton) if b.text() == "Drop"
        )
        drop.click()
        assert dlg.chosen_action() == DragDisconnectAction.DROP
        assert dlg.session_opt_out() is True

    def test_opt_out_not_set_when_user_cancels(self, qtbot) -> None:
        # Checking opt-out then cancelling must NOT propagate the opt-out —
        # only successful Reroute / Drop should cement the choice.
        dlg = DragDisconnectDialog(n_wires=1, affected_nets=["SIG"])
        qtbot.addWidget(dlg)
        from PySide6.QtWidgets import QCheckBox, QPushButton
        cb = next(iter(dlg.findChildren(QCheckBox)))
        cb.setChecked(True)
        cancel = next(
            b for b in dlg.findChildren(QPushButton) if b.text() == "Cancel"
        )
        cancel.click()
        assert dlg.chosen_action() == DragDisconnectAction.CANCEL
        # Initial _opt_out was False and stays False — cancel didn't run
        # the _accept() path that copies the checkbox state.
        assert dlg.session_opt_out() is False

    def test_long_net_list_truncated(self, qtbot) -> None:
        # 7 nets — should render first 5 + "(+2)" suffix in the label.
        nets = [f"N{i}" for i in range(7)]
        dlg = DragDisconnectDialog(n_wires=7, affected_nets=nets)
        qtbot.addWidget(dlg)
        from PySide6.QtWidgets import QLabel
        labels = [lbl.text() for lbl in dlg.findChildren(QLabel)]
        joined = " ".join(labels)
        assert "N0" in joined and "N4" in joined
        assert "N5" not in joined  # truncated
        assert "+2" in joined
