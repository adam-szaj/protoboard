# SPDX-License-Identifier: MIT
"""Tests for ``ToolbarCustomizeDialog`` — UI editing + YAML round-trip.

The dialog modifies ``toolbars.yaml`` on accept; the host MainWindow
then calls ``_reload_toolbar_layout`` to realise the changes.  Here we
exercise the dialog in isolation against a temporary YAML and assert
that the post-accept file contents match what the user edited."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMainWindow
from pytestqt.qtbot import QtBot

from protoboard.gui.action_builder import ActionRegistry
from protoboard.gui.dialogs.toolbar_customize_dialog import (
    ToolbarCustomizeDialog,
)
from protoboard.gui.toolbar_config import (
    KNOWN_GROUPS,
    load_toolbar_config,
)


@pytest.fixture(autouse=True)
def _isolate_toolbar_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``TOOLBARS_FILE`` to a throwaway path so the dialog never
    writes into the user's real config dir.  Seed the file with a known
    layout so tests start from a deterministic state."""
    fake_user = tmp_path / "toolbars.yaml"
    fake_user.write_text(
        "view:\n  - camera_toggle\n  - mirror_h\n"
        "wire: []\n"
        "tools:\n  - tool_select\n",
        encoding="utf-8",
    )
    import protoboard.gui.toolbar_config as cfg
    monkeypatch.setattr(cfg, "TOOLBARS_FILE", fake_user)
    return fake_user


@pytest.fixture
def main_window(qtbot: QtBot) -> QMainWindow:
    mw = QMainWindow()
    qtbot.addWidget(mw)
    return mw


@pytest.fixture
def dialog(main_window: QMainWindow, qtbot: QtBot) -> ToolbarCustomizeDialog:
    dlg = ToolbarCustomizeDialog(
        main_window=main_window,
        registry=ActionRegistry(),
        parent=main_window,
    )
    qtbot.addWidget(dlg)
    return dlg


class TestDialogConstruction:
    def test_one_tab_per_known_group(self, dialog: ToolbarCustomizeDialog) -> None:
        # One QTabWidget tab per ToolbarGroup literal.
        assert dialog._tabs.count() == len(KNOWN_GROUPS)
        tab_titles = {
            dialog._tabs.tabText(i).lower() for i in range(dialog._tabs.count())
        }
        assert tab_titles == {g.lower() for g in KNOWN_GROUPS}

    def test_seeded_current_list_loads_from_yaml(
        self,
        dialog: ToolbarCustomizeDialog,
    ) -> None:
        current_view, _avail_view = dialog._tab_widgets["view"]
        labels = [
            current_view.item(i).data(Qt.ItemDataRole.UserRole)  # type: ignore[union-attr]
            for i in range(current_view.count())
        ]
        assert labels == ["camera_toggle", "mirror_h"]

    def test_changed_flag_false_before_accept(
        self,
        dialog: ToolbarCustomizeDialog,
    ) -> None:
        assert dialog.changed is False


class TestEditing:
    def test_move_between_lists(
        self,
        dialog: ToolbarCustomizeDialog,
    ) -> None:
        current_view, avail_view = dialog._tab_widgets["view"]
        # Select the second available item and add it to the toolbar.
        avail_view.setCurrentRow(0)
        first_label = avail_view.item(0).text()  # type: ignore[union-attr]
        before_count = current_view.count()
        dialog._move_item(avail_view, current_view)
        assert current_view.count() == before_count + 1
        assert current_view.item(current_view.count() - 1).text() == first_label  # type: ignore[union-attr]

    def test_reorder_within_current(
        self,
        dialog: ToolbarCustomizeDialog,
    ) -> None:
        current_view, _ = dialog._tab_widgets["view"]
        # Move row 1 up.
        current_view.setCurrentRow(1)
        item_text_before = current_view.item(1).text()  # type: ignore[union-attr]
        dialog._reorder(current_view, -1)
        assert current_view.item(0).text() == item_text_before  # type: ignore[union-attr]
        assert current_view.currentRow() == 0


class TestPersistence:
    def test_accept_writes_yaml(
        self,
        dialog: ToolbarCustomizeDialog,
        _isolate_toolbar_yaml: Path,
    ) -> None:
        # Remove the first view item, then accept.
        current_view, _ = dialog._tab_widgets["view"]
        current_view.setCurrentRow(0)
        # Move into Available instead of deleting outright.
        _, avail_view = dialog._tab_widgets["view"]
        dialog._move_item(current_view, avail_view)

        dialog._on_accept()
        assert dialog.changed is True

        out = load_toolbar_config(
            user_path=_isolate_toolbar_yaml,
            default_path=Path("/dev/null"),
        )
        # 'camera_toggle' was at row 0 — should be gone now.
        assert "camera_toggle" not in out.get("view", [])
        assert "mirror_h" in out.get("view", [])

    def test_reject_does_not_write(
        self,
        dialog: ToolbarCustomizeDialog,
        _isolate_toolbar_yaml: Path,
    ) -> None:
        # Without _on_accept, the YAML stays as seeded.
        original_text = _isolate_toolbar_yaml.read_text()
        dialog.reject()
        assert _isolate_toolbar_yaml.read_text() == original_text
        assert dialog.changed is False
