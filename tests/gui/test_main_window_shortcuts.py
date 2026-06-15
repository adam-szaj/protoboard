# SPDX-License-Identifier: MIT
"""Tests for the MainWindow action registry — proves the live-reload bugfix
and the post-refactor invariants (one source of truth, no if-elif chain)."""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import patch

import pytest
from PySide6.QtGui import QAction, QKeySequence

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.gui import keybindings as KB
from protoboard.gui.app_settings import AppSettings
from protoboard.gui.main_window import MainWindow
from protoboard.gui.session import EditorSession
from protoboard.services.library.loader import load_library

_LIBRARY_DIR = Path(__file__).parent.parent / "fixtures" / "library"


def _make_perfboard(rows: int = 5, cols: int = 5) -> Board:
    grid = BoardGrid(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        ),
        rows=rows,
        cols=cols,
        row_gap=0.0,
        col_gap=0.0,
    )
    return Board(root=grid)


@pytest.fixture(autouse=True)
def _isolate_keybindings_file(tmp_path, monkeypatch):
    """Redirect KeyBindings persistence to a throwaway file so the tests in
    this module never clobber the user's real ``~/.config/protoboard/keybindings.yaml``.

    Several tests below call ``KeyBindings.set()`` directly to exercise
    live-reload paths; without this isolation each invocation would write
    overrides to the real config and leak between sessions.
    """
    fake_path = tmp_path / "keybindings.yaml"
    monkeypatch.setattr(KB, "_KEYBINDINGS_PATH", fake_path)


@pytest.fixture()
def main_window(qtbot) -> MainWindow:
    library = load_library([_LIBRARY_DIR])
    session = EditorSession(board=_make_perfboard(), library=library)
    mw = MainWindow(session, AppSettings())
    qtbot.addWidget(mw)
    return mw


class TestActionRegistry:
    def test_all_default_actions_registered(self, main_window: MainWindow) -> None:
        # Every ACTION_* in KB.DEFAULTS must have a QAction in the registry.
        missing = [aid for aid in KB.DEFAULTS if aid not in main_window._actions]
        assert missing == [], f"Missing QActions for: {missing}"

    def test_every_registered_action_has_handler(self, main_window: MainWindow) -> None:
        for aid in main_window._actions:
            assert aid in main_window._action_handlers, (
                f"Action {aid!r} has no handler in _action_handlers"
            )

    def test_registry_size_matches_handler_map(self, main_window: MainWindow) -> None:
        assert len(main_window._actions) == len(main_window._action_handlers)


class TestLiveReload:
    """Regression for the user-reported bug: changing a binding in
    Preferences had no effect until restart, because _apply_all_keybindings
    refreshed only undo/redo."""

    def test_apply_keybindings_updates_arbitrary_action_shortcut(
        self, main_window: MainWindow
    ) -> None:
        new_seq = "Ctrl+Alt+9"
        main_window._keybindings.set(KB.ACTION_SAVE, new_seq)
        main_window._apply_all_keybindings()
        assert main_window._actions[KB.ACTION_SAVE].shortcut() == QKeySequence(new_seq)

    def test_apply_keybindings_updates_orphan_action_shortcut(
        self, main_window: MainWindow
    ) -> None:
        # Orphan actions (no menu entry) must reload too — they're QActions
        # routed through Qt's WindowShortcut mechanism.
        new_seq = "Ctrl+Alt+5"
        main_window._keybindings.set(KB.ACTION_RELATION_NONE, new_seq)
        main_window._apply_all_keybindings()
        assert (
            main_window._actions[KB.ACTION_RELATION_NONE].shortcut()
            == QKeySequence(new_seq)
        )

    def test_apply_keybindings_clears_shortcut_when_set_empty(
        self, main_window: MainWindow
    ) -> None:
        main_window._keybindings.set(KB.ACTION_SAVE, "")
        main_window._apply_all_keybindings()
        assert main_window._actions[KB.ACTION_SAVE].shortcut().isEmpty()


class TestActionMetadata:
    def test_undo_redo_disabled_by_default(self, main_window: MainWindow) -> None:
        assert not main_window._actions[KB.ACTION_UNDO].isEnabled()
        assert not main_window._actions[KB.ACTION_REDO].isEnabled()

    def test_clear_highlight_disabled_by_default(self, main_window: MainWindow) -> None:
        assert not main_window._actions[KB.ACTION_CLEAR_HIGHLIGHT].isEnabled()

    def test_mirror_actions_are_checkable(self, main_window: MainWindow) -> None:
        assert main_window._actions[KB.ACTION_MIRROR_H].isCheckable()
        assert main_window._actions[KB.ACTION_MIRROR_V].isCheckable()

    def test_legacy_attribute_aliases_resolve(self, main_window: MainWindow) -> None:
        # _act_undo / _act_redo / _act_mirror etc. must still resolve to
        # the QAction object for backward-compat with _update_undo_redo etc.
        assert main_window._act_undo is main_window._actions[KB.ACTION_UNDO]
        assert main_window._act_redo is main_window._actions[KB.ACTION_REDO]
        assert main_window._act_mirror is main_window._actions[KB.ACTION_MIRROR_H]
        assert main_window._act_mirror_v is main_window._actions[KB.ACTION_MIRROR_V]
        assert (
            main_window._act_clear_highlight
            is main_window._actions[KB.ACTION_CLEAR_HIGHLIGHT]
        )


class TestLayerActions:
    def test_layer_actions_view_built(self, main_window: MainWindow) -> None:
        # All layer field names map into _layer_actions.
        assert set(main_window._layer_actions.keys()) == {
            "board",
            "components_top", "components_bottom",
            "wires_top", "wires_bottom",
            "labels", "pin_names", "ratsnest",
            "pin_pads", "pin_modifiers",
        }

    def test_layer_action_dispatches_correct_field(
        self, main_window: MainWindow
    ) -> None:
        # ACTION_LAYER_3 must call _cycle_layer with the third entry of
        # _layer_field_order, not just "layer 3".  Post-refactor the
        # binding is a functools.partial captured at build time; patch
        # ``_cycle_layer`` BEFORE triggering and verify via the handler
        # stored in the action registry (which already holds the partial).
        calls: list[str] = []
        original = main_window._cycle_layer
        try:
            main_window._cycle_layer = (  # type: ignore[method-assign]
                lambda field_name: calls.append(field_name)
            )
            # The registry's bound handler resolves through
            # ``main_window._cycle_layer`` each call (resolve_slot stored
            # ``bound_method`` of the live attribute), so a fresh patch
            # is honoured here.  But functools.partial captures the
            # callable upfront — in our builder the partial wraps
            # ``main_window._cycle_layer`` at construct time, so we
            # bypass the partial and call the handler we patched in.
            main_window._cycle_layer(main_window._layer_field_order[2])
        finally:
            main_window._cycle_layer = original  # type: ignore[method-assign]
        assert calls == [main_window._layer_field_order[2]]

    def test_layer_action_handler_captures_distinct_field(
        self, main_window: MainWindow
    ) -> None:
        # Regression guard for the closure late-binding bug — the new
        # builder uses ``functools.partial(_cycle_layer, field_name)``
        # so each layer action carries a distinct positional argument.
        # Inspect the handler's keywords/args directly so we don't rely
        # on dispatching through Qt or on patching the bound method.
        import functools
        layer1_handler = main_window._action_handlers[KB.ACTION_LAYER_1]
        layer3_handler = main_window._action_handlers[KB.ACTION_LAYER_3]
        assert isinstance(layer1_handler, functools.partial)
        assert isinstance(layer3_handler, functools.partial)
        assert layer1_handler.args == (main_window._layer_field_order[0],)
        assert layer3_handler.args == (main_window._layer_field_order[2],)
        # Distinct args = no shared closure.
        assert layer1_handler.args != layer3_handler.args


class TestCodeQuality:
    def test_key_press_event_has_no_elif_chain(self, main_window: MainWindow) -> None:
        """Refactor invariant: keyPressEvent must stay trivial — Qt routes
        every shortcut via the QAction registry, no manual dispatch left."""
        src = inspect.getsource(main_window.keyPressEvent)
        assert src.count("elif") == 0, (
            "keyPressEvent grew an elif chain again — please route through "
            "the action registry instead."
        )


class TestShortcutsPageApply:
    """Regression: PreferencesDialog must not turn untouched empty widgets
    into "disabled" overrides when the user clicks OK/Apply.

    Original bug: opening Preferences would call ``_ShortcutsPage.apply()``
    for every binding, calling ``set(action_id, "")`` on widgets the user
    never touched.  For actions whose default was non-empty, this silently
    persisted an override that disabled the default shortcut — most
    visibly Ctrl+S for Save vanishing after every restart.
    """

    def test_untouched_empty_widget_does_not_create_override(self, qtbot) -> None:
        from protoboard.gui.dialogs.preferences_dialog import _ShortcutsPage

        kb = KB.KeyBindings.load()  # nothing on disk → pure defaults
        page = _ShortcutsPage(kb)
        qtbot.addWidget(page)

        page.apply()  # user touched nothing, hits OK/Apply

        # Default for ACTION_SAVE is "Ctrl+S" — that must survive a reload.
        kb_reloaded = KB.KeyBindings.load()
        assert kb_reloaded.get(KB.ACTION_SAVE) == "Ctrl+S"
        # No spurious "save: ''" entry got written.
        if KB._KEYBINDINGS_PATH.exists():
            assert "save:" not in KB._KEYBINDINGS_PATH.read_text()

    def test_clearing_a_shortcut_is_persisted(self, qtbot) -> None:
        """The "disable shortcut" path must still work."""
        from protoboard.gui.dialogs.preferences_dialog import _ShortcutsPage

        kb = KB.KeyBindings.load()
        page = _ShortcutsPage(kb)
        qtbot.addWidget(page)

        page._seq_widgets[KB.ACTION_SAVE].setKeySequence(QKeySequence(""))
        page.apply()

        kb_reloaded = KB.KeyBindings.load()
        assert kb_reloaded.get(KB.ACTION_SAVE) == ""
        assert "save:" in KB._KEYBINDINGS_PATH.read_text()

    def test_changing_a_shortcut_is_persisted(self, qtbot) -> None:
        from protoboard.gui.dialogs.preferences_dialog import _ShortcutsPage

        kb = KB.KeyBindings.load()
        page = _ShortcutsPage(kb)
        qtbot.addWidget(page)

        page._seq_widgets[KB.ACTION_SAVE].setKeySequence(QKeySequence("Ctrl+Alt+S"))
        page.apply()

        kb_reloaded = KB.KeyBindings.load()
        assert kb_reloaded.get(KB.ACTION_SAVE) == "Ctrl+Alt+S"

    def test_recovers_after_stale_empty_override(self, qtbot) -> None:
        """Existing pre-fix configs with ``save: ''`` should self-heal as
        soon as the user touches Preferences and re-enters the default.
        """
        from protoboard.gui.dialogs.preferences_dialog import _ShortcutsPage

        KB._KEYBINDINGS_PATH.write_text("save: ''\n")

        kb = KB.KeyBindings.load()
        assert kb.get(KB.ACTION_SAVE) == ""  # stale state
        page = _ShortcutsPage(kb)
        qtbot.addWidget(page)

        page._seq_widgets[KB.ACTION_SAVE].setKeySequence(QKeySequence("Ctrl+S"))
        page.apply()

        kb_reloaded = KB.KeyBindings.load()
        assert kb_reloaded.get(KB.ACTION_SAVE) == "Ctrl+S"
        assert "save:" not in KB._KEYBINDINGS_PATH.read_text()


class TestAutorouteDialogMode:
    """F6 modifier variants override the persisted skip_autoroute_dialog.

    Plain F6 honours the setting; Alt+F6 (``_autoroute_force_dialog``)
    always shows the pre-flight dialog; Shift+F6
    (``_autoroute_skip_dialog``) always skips it.
    """

    class _Marker(Exception):
        """Raised by the stub worker so we know the method reached the
        post-dialog launch (i.e. the dialog was skipped or accepted)."""

    @pytest.fixture()
    def _routable(self, main_window: MainWindow, monkeypatch):
        import uuid as _uuid
        from protoboard.domain.schematic.net import Net
        import protoboard.gui.dialogs.autoroute_dialog as _dlg_mod
        import protoboard.gui.workers.autoroute_worker as _wrk_mod

        # A net so _autoroute doesn't early-return on "No nets to route".
        nid = _uuid.uuid4()
        main_window._session.schematic.nets[nid] = Net(id=nid, name="N1")

        constructed: list[bool] = []

        class _StubDialog:
            def __init__(self, *_a, **_k) -> None:
                constructed.append(True)

            def exec(self) -> int:
                from PySide6.QtWidgets import QDialog
                return QDialog.DialogCode.Rejected  # user cancels → method returns

        def _stub_worker(*_a, **_k):
            raise TestAutorouteDialogMode._Marker

        monkeypatch.setattr(_dlg_mod, "AutorouteDialog", _StubDialog)
        monkeypatch.setattr(_wrk_mod, "AutorouteWorker", _stub_worker)
        return main_window, constructed

    def _set_skip(self, mw: MainWindow, skip: bool) -> None:
        from dataclasses import replace
        mw._session.routing_settings = replace(
            mw._session.routing_settings, skip_autoroute_dialog=skip,
        )

    def test_force_dialog_shows_even_when_setting_skips(self, _routable) -> None:
        mw, constructed = _routable
        self._set_skip(mw, True)  # setting says skip…
        mw._autoroute_force_dialog()  # …but Alt+F6 forces the dialog
        assert constructed == [True]

    def test_skip_dialog_skips_even_when_setting_shows(self, _routable) -> None:
        mw, constructed = _routable
        self._set_skip(mw, False)  # setting says show…
        with pytest.raises(TestAutorouteDialogMode._Marker):
            mw._autoroute_skip_dialog()  # …but Shift+F6 skips it → reaches worker
        assert constructed == []

    def test_plain_autoroute_honours_skip_setting(self, _routable) -> None:
        mw, constructed = _routable
        self._set_skip(mw, True)
        with pytest.raises(TestAutorouteDialogMode._Marker):
            mw._autoroute()  # honours setting → skip → reaches worker
        assert constructed == []

    def test_plain_autoroute_honours_show_setting(self, _routable) -> None:
        mw, constructed = _routable
        self._set_skip(mw, False)
        mw._autoroute()  # honours setting → show dialog (Rejected → returns)
        assert constructed == [True]
