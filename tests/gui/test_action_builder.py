# SPDX-License-Identifier: MIT
"""Unit tests for the Phase-0 ActionSpec / build_actions infrastructure.

These tests exercise the builder against a tiny synthetic spec list so
the suite stays decoupled from MainWindow.  Coverage:

* slot resolution (bare name + dotted owner.method)
* shortcut application from a KeyBindings instance
* menu placement, including submenu reuse and separators
* toolbar placement and customisation diff (added / removed)
* exclusive QActionGroup wiring
* error cases (unknown slot, duplicate id, missing owner)
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMainWindow, QMenu
from pytestqt.qtbot import QtBot

from protoboard.errors import ActionRegistrationError
from protoboard.gui import keybindings as KB
from protoboard.gui.action_builder import (
    ActionRegistry,
    ToolbarCustomization,
    build_actions,
    resolve_slot,
)
from protoboard.gui.actions import ActionSpec, strip_mnemonic


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class _MwStub:
    """Records slot invocations without needing real QObject signals."""
    calls: list[tuple[str, tuple[object, ...]]] = field(default_factory=list)

    def _save(self) -> None:
        self.calls.append(("_save", ()))

    def _move_wire_to_side(self, side: str) -> None:
        self.calls.append(("_move_wire_to_side", (side,)))

    def _toggle_thing(self) -> None:
        self.calls.append(("_toggle_thing", ()))


@dataclass
class _ToolStub:
    calls: list[str] = field(default_factory=list)

    def _convert(self) -> None:
        self.calls.append("_convert")


@pytest.fixture
def main_window(qtbot: QtBot) -> QMainWindow:
    mw = QMainWindow()
    qtbot.addWidget(mw)
    return mw


@pytest.fixture
def kb_empty() -> KB.KeyBindings:
    # No shortcuts so we can vary per-test by mutating ``_bindings``.
    return KB.KeyBindings(_bindings={})


# ---------------------------------------------------------------------------
# strip_mnemonic
# ---------------------------------------------------------------------------


class TestStripMnemonic:
    def test_drops_single_ampersand(self) -> None:
        assert strip_mnemonic("&Save") == "Save"
        assert strip_mnemonic("Sa&ve As") == "Save As"

    def test_preserves_double_ampersand(self) -> None:
        assert strip_mnemonic("Fish && Chips") == "Fish & Chips"

    def test_no_marker_unchanged(self) -> None:
        assert strip_mnemonic("Plain") == "Plain"

    def test_trailing_ampersand_swallowed(self) -> None:
        assert strip_mnemonic("Save&") == "Save"


# ---------------------------------------------------------------------------
# resolve_slot
# ---------------------------------------------------------------------------


class TestResolveSlot:
    def test_bare_method_on_default_owner(self) -> None:
        mw = _MwStub()
        cb = resolve_slot({"mw": mw}, "_save", ())
        cb()
        assert mw.calls == [("_save", ())]

    def test_dotted_owner_method(self) -> None:
        mw = _MwStub()
        tool = _ToolStub()
        cb = resolve_slot({"mw": mw, "select_tool": tool}, "select_tool._convert", ())
        cb()
        assert tool.calls == ["_convert"]
        assert mw.calls == []

    def test_partial_with_bound_args(self) -> None:
        mw = _MwStub()
        cb = resolve_slot({"mw": mw}, "_move_wire_to_side", ("top",))
        cb()
        cb()
        assert mw.calls == [
            ("_move_wire_to_side", ("top",)),
            ("_move_wire_to_side", ("top",)),
        ]

    def test_missing_owner_raises(self) -> None:
        mw = _MwStub()
        with pytest.raises(ActionRegistrationError, match="no registered owner"):
            resolve_slot({"mw": mw}, "ghost._x", ())

    def test_missing_method_raises(self) -> None:
        mw = _MwStub()
        with pytest.raises(ActionRegistrationError, match="no callable attribute"):
            resolve_slot({"mw": mw}, "_does_not_exist", ())

    def test_non_callable_attribute_raises(self) -> None:
        class Owner:
            x = 42
        with pytest.raises(ActionRegistrationError, match="no callable attribute"):
            resolve_slot({"mw": Owner()}, "x", ())


# ---------------------------------------------------------------------------
# build_actions — happy paths
# ---------------------------------------------------------------------------


class TestBuildActions:
    def test_creates_qactions_and_handlers(
        self,
        main_window: QMainWindow,
        kb_empty: KB.KeyBindings,
    ) -> None:
        mw = _MwStub()
        specs = (
            ActionSpec(id="save", label="&Save", slot_name="_save",
                       menu_path=("File",), default_toolbar=None),
        )
        reg = build_actions(
            main_window, specs, kb_empty,
            slot_owners={"mw": mw},
        )
        assert "save" in reg.actions
        assert isinstance(reg.actions["save"], QAction)
        # Handler invocation routes to the stub.
        reg.handlers["save"]()
        assert mw.calls == [("_save", ())]

    def test_applies_shortcut_from_keybindings(
        self,
        main_window: QMainWindow,
    ) -> None:
        mw = _MwStub()
        kb = KB.KeyBindings(_bindings={"save": "Ctrl+S"})
        specs = (
            ActionSpec(id="save", label="&Save", slot_name="_save",
                       menu_path=("File",), default_toolbar=None),
        )
        reg = build_actions(main_window, specs, kb, slot_owners={"mw": mw})
        assert reg.actions["save"].shortcut().toString() == "Ctrl+S"

    def test_empty_shortcut_means_no_shortcut(
        self,
        main_window: QMainWindow,
        kb_empty: KB.KeyBindings,
    ) -> None:
        mw = _MwStub()
        specs = (
            ActionSpec(id="x", label="X", slot_name="_save",
                       menu_path=None, default_toolbar=None),
        )
        reg = build_actions(main_window, specs, kb_empty, slot_owners={"mw": mw})
        assert reg.actions["x"].shortcut().isEmpty()

    def test_menu_path_creates_submenu_and_caches(
        self,
        main_window: QMainWindow,
        kb_empty: KB.KeyBindings,
    ) -> None:
        mw = _MwStub()
        specs = (
            ActionSpec(id="a", label="A", slot_name="_save",
                       menu_path=("Edit", "Design", "Wire"),
                       default_toolbar=None),
            ActionSpec(id="b", label="B", slot_name="_toggle_thing",
                       menu_path=("Edit", "Design", "Wire"),
                       default_toolbar=None),
            ActionSpec(id="c", label="C", slot_name="_save",
                       menu_path=("Edit", "Design"),
                       default_toolbar=None),
        )
        reg = build_actions(main_window, specs, kb_empty, slot_owners={"mw": mw})
        wire_menu = reg.menus[("Edit", "Design", "Wire")]
        # Both A and B share the same Wire submenu.
        labels = [a.text() for a in wire_menu.actions()]
        assert labels == ["A", "B"]
        # Edit ► Design has C as direct child plus the Wire submenu.
        design_menu = reg.menus[("Edit", "Design")]
        design_children = [a.text() for a in design_menu.actions()]
        assert "C" in design_children
        # And the Wire submenu sits inside it as a sub-action.
        sub_menus = [a.menu() for a in design_menu.actions() if a.menu() is not None]
        assert wire_menu in sub_menus

    def test_separator_before_inserts_divider(
        self,
        main_window: QMainWindow,
        kb_empty: KB.KeyBindings,
    ) -> None:
        mw = _MwStub()
        specs = (
            ActionSpec(id="a", label="A", slot_name="_save",
                       menu_path=("File",), default_toolbar=None),
            ActionSpec(id="b", label="B", slot_name="_save",
                       menu_path=("File",), default_toolbar=None,
                       separator_before=True),
        )
        reg = build_actions(main_window, specs, kb_empty, slot_owners={"mw": mw})
        file_menu = reg.menus[("File",)]
        actions = file_menu.actions()
        # Expect: A, separator, B
        assert actions[0].text() == "A"
        assert actions[1].isSeparator()
        assert actions[2].text() == "B"

    def test_menu_path_none_means_orphan(
        self,
        main_window: QMainWindow,
        kb_empty: KB.KeyBindings,
    ) -> None:
        mw = _MwStub()
        specs = (
            ActionSpec(id="orphan", label="Orphan", slot_name="_save",
                       menu_path=None, default_toolbar=None),
        )
        reg = build_actions(main_window, specs, kb_empty, slot_owners={"mw": mw})
        assert reg.menus == {}
        # But the QAction still exists for shortcut routing.
        assert "orphan" in reg.actions

    def test_toolbar_default_placement(
        self,
        main_window: QMainWindow,
        kb_empty: KB.KeyBindings,
    ) -> None:
        mw = _MwStub()
        specs = (
            ActionSpec(id="run_drc", label="Run DRC", slot_name="_save",
                       menu_path=("Analysis",), default_toolbar="analysis"),
        )
        reg = build_actions(main_window, specs, kb_empty, slot_owners={"mw": mw})
        assert "analysis" in reg.toolbars
        assert reg.actions["run_drc"] in reg.toolbars["analysis"].actions()

    def test_action_group_makes_actions_exclusive(
        self,
        main_window: QMainWindow,
        kb_empty: KB.KeyBindings,
    ) -> None:
        mw = _MwStub()
        specs = (
            ActionSpec(id="t1", label="T1", slot_name="_save",
                       menu_path=None, default_toolbar=None,
                       checkable=True, action_group="tool"),
            ActionSpec(id="t2", label="T2", slot_name="_save",
                       menu_path=None, default_toolbar=None,
                       checkable=True, action_group="tool"),
        )
        reg = build_actions(main_window, specs, kb_empty, slot_owners={"mw": mw})
        group = reg.action_groups["tool"]
        assert group.isExclusive()
        assert set(group.actions()) == {reg.actions["t1"], reg.actions["t2"]}

    def test_partial_args_pass_to_slot(
        self,
        main_window: QMainWindow,
        kb_empty: KB.KeyBindings,
    ) -> None:
        mw = _MwStub()
        specs = (
            ActionSpec(id="top", label="Top", slot_name="_move_wire_to_side",
                       menu_path=None, default_toolbar=None,
                       shortcut_args=("top",)),
            ActionSpec(id="bot", label="Bot", slot_name="_move_wire_to_side",
                       menu_path=None, default_toolbar=None,
                       shortcut_args=("bottom",)),
        )
        reg = build_actions(main_window, specs, kb_empty, slot_owners={"mw": mw})
        reg.handlers["top"]()
        reg.handlers["bot"]()
        assert mw.calls == [
            ("_move_wire_to_side", ("top",)),
            ("_move_wire_to_side", ("bottom",)),
        ]


# ---------------------------------------------------------------------------
# Customisation
# ---------------------------------------------------------------------------


class TestToolbarCustomization:
    def test_removed_hides_from_default_toolbar(
        self,
        main_window: QMainWindow,
        kb_empty: KB.KeyBindings,
    ) -> None:
        mw = _MwStub()
        specs = (
            ActionSpec(id="a", label="A", slot_name="_save",
                       menu_path=None, default_toolbar="view"),
        )
        custom = ToolbarCustomization(removed={"view": ["a"]})
        reg = build_actions(
            main_window, specs, kb_empty,
            slot_owners={"mw": mw}, toolbar_customization=custom,
        )
        # Toolbar never created when the only action was removed.
        assert "view" not in reg.toolbars

    def test_added_promotes_orphan_to_toolbar(
        self,
        main_window: QMainWindow,
        kb_empty: KB.KeyBindings,
    ) -> None:
        mw = _MwStub()
        specs = (
            ActionSpec(id="orphan", label="Orphan", slot_name="_save",
                       menu_path=None, default_toolbar=None),
        )
        custom = ToolbarCustomization(added={"view": ["orphan"]})
        reg = build_actions(
            main_window, specs, kb_empty,
            slot_owners={"mw": mw}, toolbar_customization=custom,
        )
        assert reg.actions["orphan"] in reg.toolbars["view"].actions()

    def test_added_unknown_id_silently_ignored(
        self,
        main_window: QMainWindow,
        kb_empty: KB.KeyBindings,
    ) -> None:
        mw = _MwStub()
        custom = ToolbarCustomization(added={"view": ["ghost"]})
        reg = build_actions(
            main_window, (), kb_empty,
            slot_owners={"mw": mw}, toolbar_customization=custom,
        )
        # No toolbar created for an empty addition set.
        assert "view" not in reg.toolbars


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestBuildActionsErrors:
    def test_duplicate_id_raises(
        self,
        main_window: QMainWindow,
        kb_empty: KB.KeyBindings,
    ) -> None:
        mw = _MwStub()
        specs = (
            ActionSpec(id="dup", label="A", slot_name="_save",
                       menu_path=None, default_toolbar=None),
            ActionSpec(id="dup", label="B", slot_name="_save",
                       menu_path=None, default_toolbar=None),
        )
        with pytest.raises(ActionRegistrationError, match="duplicate"):
            build_actions(main_window, specs, kb_empty, slot_owners={"mw": mw})

    def test_unknown_slot_raises(
        self,
        main_window: QMainWindow,
        kb_empty: KB.KeyBindings,
    ) -> None:
        mw = _MwStub()
        specs = (
            ActionSpec(id="x", label="X", slot_name="_nope",
                       menu_path=None, default_toolbar=None),
        )
        with pytest.raises(ActionRegistrationError):
            build_actions(main_window, specs, kb_empty, slot_owners={"mw": mw})

    def test_empty_menu_path_raises(
        self,
        main_window: QMainWindow,
        kb_empty: KB.KeyBindings,
    ) -> None:
        # Tuples cannot be empty in the field type, but a bare-cast bug
        # could slip ``()`` in.  Verify the builder defends against it.
        mw = _MwStub()
        spec = ActionSpec(
            id="x", label="X", slot_name="_save",
            menu_path=(), default_toolbar=None,
        )
        with pytest.raises(ActionRegistrationError, match="non-empty"):
            build_actions(main_window, (spec,), kb_empty, slot_owners={"mw": mw})


# ---------------------------------------------------------------------------
# Smoke: triggering a QAction routes through to the slot
# ---------------------------------------------------------------------------


class TestActionTrigger:
    def test_qaction_trigger_invokes_handler(
        self,
        main_window: QMainWindow,
        kb_empty: KB.KeyBindings,
    ) -> None:
        mw = _MwStub()
        specs = (
            ActionSpec(id="save", label="&Save", slot_name="_save",
                       menu_path=("File",), default_toolbar=None),
        )
        reg = build_actions(main_window, specs, kb_empty, slot_owners={"mw": mw})
        # Trigger via QAction directly — verifies the lambda adapter.
        reg.actions["save"].trigger()
        assert mw.calls == [("_save", ())]


# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------


class TestActionRegistryShape:
    def test_default_registry_is_empty(self) -> None:
        reg = ActionRegistry()
        assert reg.actions == {}
        assert reg.handlers == {}
        assert reg.menus == {}
        assert reg.toolbars == {}
        assert reg.action_groups == {}

    def test_menus_indexed_by_full_path(
        self,
        main_window: QMainWindow,
        kb_empty: KB.KeyBindings,
    ) -> None:
        mw = _MwStub()
        specs = (
            ActionSpec(id="a", label="A", slot_name="_save",
                       menu_path=("Edit", "Design"),
                       default_toolbar=None),
        )
        reg = build_actions(main_window, specs, kb_empty, slot_owners={"mw": mw})
        # Builder records every prefix it created, not just the leaf.
        assert ("Edit",) in reg.menus
        assert ("Edit", "Design") in reg.menus
        assert isinstance(reg.menus[("Edit",)], QMenu)
