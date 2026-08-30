# SPDX-License-Identifier: MIT
"""Tests for DragAskRemoveDialog (Yes / Cancel + opt-out)."""

from __future__ import annotations

from PySide6.QtWidgets import QCheckBox, QDialog, QPushButton

from protoboard.gui.dialogs.drag_ask_remove_dialog import DragAskRemoveDialog


class TestDragAskRemoveDialog:
    def test_default_state(self, qtbot) -> None:
        dlg = DragAskRemoveDialog(
            title="Remove?",
            message="Drop 1 wire?",
        )
        qtbot.addWidget(dlg)
        assert dlg.session_opt_out() is False
        # Window title respected.
        assert dlg.windowTitle() == "Remove?"

    def test_yes_button_accepts(self, qtbot) -> None:
        dlg = DragAskRemoveDialog(title="Q", message="M")
        qtbot.addWidget(dlg)
        yes = next(b for b in dlg.findChildren(QPushButton) if b.text() == "Yes")
        yes.click()
        assert dlg.result() == QDialog.DialogCode.Accepted

    def test_cancel_button_rejects(self, qtbot) -> None:
        dlg = DragAskRemoveDialog(title="Q", message="M")
        qtbot.addWidget(dlg)
        cancel = next(b for b in dlg.findChildren(QPushButton) if b.text() == "Cancel")
        cancel.click()
        assert dlg.result() == QDialog.DialogCode.Rejected

    def test_opt_out_propagates_on_yes(self, qtbot) -> None:
        dlg = DragAskRemoveDialog(title="Q", message="M")
        qtbot.addWidget(dlg)
        cb = next(iter(dlg.findChildren(QCheckBox)))
        cb.setChecked(True)
        yes = next(b for b in dlg.findChildren(QPushButton) if b.text() == "Yes")
        yes.click()
        assert dlg.session_opt_out() is True

    def test_opt_out_not_cemented_on_cancel(self, qtbot) -> None:
        dlg = DragAskRemoveDialog(title="Q", message="M")
        qtbot.addWidget(dlg)
        cb = next(iter(dlg.findChildren(QCheckBox)))
        cb.setChecked(True)
        cancel = next(b for b in dlg.findChildren(QPushButton) if b.text() == "Cancel")
        cancel.click()
        assert dlg.session_opt_out() is False
