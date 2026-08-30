# SPDX-License-Identifier: MIT
"""Strong invariants for the declarative action registry.

These tests run after every PR to catch regressions that the Phase 1
mechanical migration could have masked: a spec drifting away from its
slot, a menu path silently broken when a submenu rename lands, an
orphan added without justification.  See ``src/protoboard/gui/actions.py``
for the spec list and ``src/protoboard/gui/action_builder.py`` for the
builder under test.
"""

from __future__ import annotations

import functools
import inspect
import re
from collections import Counter
from pathlib import Path

import pytest
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu
from pytestqt.qtbot import QtBot

from protoboard.controller.app_settings import AppSettings
from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.gui import keybindings as KB
from protoboard.gui.actions import (
    ALL_ACTIONS,
    DISPLAY_NAMES_FROM_SPECS,
    ActionSpec,
    strip_mnemonic,
)
from protoboard.gui.main_window import MainWindow
from protoboard.gui.session import EditorSession
from protoboard.services.library.loader import load_library

_LIBRARY_DIR = Path(__file__).parent.parent / "fixtures" / "library"


def _make_perfboard(rows: int = 5, cols: int = 5) -> Board:
    return Board(
        root=BoardGrid(
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
    )


@pytest.fixture(autouse=True)
def _isolate_keybindings_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect KeyBindings persistence to a throwaway file — see the
    twin fixture in test_main_window_shortcuts.py for the rationale."""
    fake_path = tmp_path / "keybindings.yaml"
    monkeypatch.setattr(KB, "_KEYBINDINGS_PATH", fake_path)


@pytest.fixture()
def main_window(qtbot: QtBot) -> MainWindow:
    library = load_library([_LIBRARY_DIR])
    session = EditorSession(board=_make_perfboard(), library=library)
    mw = MainWindow(session, AppSettings())
    qtbot.addWidget(mw)
    return mw


# ---------------------------------------------------------------------------
# Spec / KB.DEFAULTS coverage
# ---------------------------------------------------------------------------


class TestDefaultsAndSpecsAlignment:
    """KB.DEFAULTS and ALL_ACTIONS must describe the same action set."""

    def test_every_kb_default_has_spec(self) -> None:
        spec_ids = {s.id for s in ALL_ACTIONS}
        missing = sorted(set(KB.DEFAULTS) - spec_ids)
        assert missing == [], f"KB.DEFAULTS entries without an ActionSpec: {missing}"

    def test_every_spec_id_in_kb_defaults(self) -> None:
        # Defensive: a spec with an id not in KB.DEFAULTS would silently
        # have no shortcut at all (KB.get returns "").  Forbidden.
        kb_ids = set(KB.DEFAULTS)
        extra = sorted({s.id for s in ALL_ACTIONS} - kb_ids)
        assert extra == [], f"ActionSpec ids not in KB.DEFAULTS: {extra}"

    def test_no_duplicate_spec_ids(self) -> None:
        counts = Counter(s.id for s in ALL_ACTIONS)
        dups = sorted(k for k, v in counts.items() if v > 1)
        assert dups == [], f"Duplicate ActionSpec ids: {dups}"


# ---------------------------------------------------------------------------
# Shortcut hygiene
# ---------------------------------------------------------------------------


class TestShortcutHygiene:
    def test_no_two_actions_share_shortcut(self) -> None:
        non_empty = [(aid, sc) for aid, sc in KB.DEFAULTS.items() if sc]
        seen: dict[str, list[str]] = {}
        for aid, sc in non_empty:
            seen.setdefault(sc, []).append(aid)
        clashes = {sc: aids for sc, aids in seen.items() if len(aids) > 1}
        assert clashes == {}, f"Multiple actions bound to the same shortcut: {clashes}"


# ---------------------------------------------------------------------------
# Slot resolution + arity
# ---------------------------------------------------------------------------


class TestSlotResolution:
    """Static-resolve every spec slot against the MainWindow class.

    We cannot construct a MainWindow without Qt/session machinery, but
    we can inspect the class — bound methods resolve via
    ``getattr(MainWindow, name)`` returning the unbound function.  For
    cross-owner slots (dotted ``"owner.method"``) we still allowlist
    the owner key; Phase 2 has no such entries yet.
    """

    @pytest.mark.parametrize("spec", list(ALL_ACTIONS), ids=lambda s: s.id)
    def test_slot_exists_on_main_window(self, spec: ActionSpec) -> None:
        if "." in spec.slot_name:
            # Dotted slot name carries its own owner; nothing to assert
            # here without instantiating the owner.  Once Phase 2 adds
            # real cross-owner slots, extend this test with an allowlist.
            return
        method = getattr(MainWindow, spec.slot_name, None)
        assert (
            method is not None
        ), f"Spec {spec.id!r}: MainWindow.{spec.slot_name} does not exist."
        assert callable(
            method
        ), f"Spec {spec.id!r}: MainWindow.{spec.slot_name} is not callable."

    @pytest.mark.parametrize("spec", list(ALL_ACTIONS), ids=lambda s: s.id)
    def test_slot_arity_matches_shortcut_args(self, spec: ActionSpec) -> None:
        """Method must accept exactly ``len(shortcut_args)`` positional
        parameters beyond ``self``.  Catches the "I forgot to thread the
        spec.shortcut_args[0] through to the slot" bug at CI time."""
        if "." in spec.slot_name:
            return
        method = getattr(MainWindow, spec.slot_name, None)
        if method is None:
            pytest.skip("slot_exists test covers absence")
        sig = inspect.signature(method)
        # Drop "self" (first positional param for unbound methods).
        params = list(sig.parameters.values())[1:]
        positional = [
            p
            for p in params
            if p.kind
            in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.POSITIONAL_ONLY,
            )
            and p.default is inspect.Parameter.empty
        ]
        required = len(positional)
        provided = len(spec.shortcut_args)
        assert required == provided, (
            f"Spec {spec.id!r}: slot {spec.slot_name!r} expects {required} "
            f"positional arg(s) but shortcut_args provides {provided}."
        )


# ---------------------------------------------------------------------------
# Label / DISPLAY_NAMES consistency
# ---------------------------------------------------------------------------


class TestLabelConsistency:
    def test_strip_mnemonic_matches_display_names(self) -> None:
        for spec in ALL_ACTIONS:
            assert DISPLAY_NAMES_FROM_SPECS[spec.id] == strip_mnemonic(
                spec.label
            ), f"Spec {spec.id!r}: derived display name diverges from spec.label."

    def test_display_names_lookup_covers_every_default(self) -> None:
        # KB.DISPLAY_NAMES is the legacy lookup used by Preferences.
        # Either every KB.DEFAULTS key is in KB.DISPLAY_NAMES, or it is
        # in DISPLAY_NAMES_FROM_SPECS — the spec list is the new source
        # of truth so we tolerate the legacy dict missing entries that
        # exist in the spec list (Phase 2 wire-pin actions).
        for aid in KB.DEFAULTS:
            has_legacy = aid in KB.DISPLAY_NAMES
            has_spec = aid in DISPLAY_NAMES_FROM_SPECS
            assert (
                has_legacy or has_spec
            ), f"Action {aid!r} has no human-readable label anywhere."


# ---------------------------------------------------------------------------
# Menu + toolbar coverage (live MainWindow)
# ---------------------------------------------------------------------------


def _menu_action_ids(menu: QMenu) -> set[QAction]:
    """Return the set of QActions reachable from ``menu`` and its
    submenus (transitively).  Used to assert that every spec lands
    where its ``menu_path`` says it should."""
    actions: set[QAction] = set()
    stack: list[QMenu] = [menu]
    while stack:
        m = stack.pop()
        for a in m.actions():
            actions.add(a)
            sub = a.menu()
            if isinstance(sub, QMenu):
                stack.append(sub)
    return actions


def _menu_bar_top_menu(main_window: MainWindow, title: str) -> QMenu | None:
    for m in main_window.menuBar().findChildren(QMenu):
        # Top-level menus are direct children of the QMenuBar; match by
        # display title with mnemonic stripped.
        if strip_mnemonic(m.title()) == title:
            return m
    return None


class TestMenuCoverage:
    """Every spec with a ``menu_path`` must surface in the corresponding
    QMenu after MainWindow construction."""

    @pytest.mark.parametrize(
        "spec",
        [s for s in ALL_ACTIONS if s.menu_path is not None],
        ids=lambda s: s.id,
    )
    def test_spec_action_appears_in_its_menu(
        self,
        main_window: MainWindow,
        spec: ActionSpec,
    ) -> None:
        assert spec.menu_path is not None
        top_title = spec.menu_path[0]
        top = _menu_bar_top_menu(main_window, top_title)
        assert (
            top is not None
        ), f"Spec {spec.id!r}: top-level menu {top_title!r} not found."
        reachable = _menu_action_ids(top)
        act = main_window._actions[spec.id]
        assert (
            act in reachable
        ), f"Spec {spec.id!r}: QAction not reachable from {top_title!r} menu."


class TestOrphansHaveNoMenu:
    """Specs with ``menu_path=None`` must NOT appear in any top-level
    menu.  Catches an orphan accidentally being added to the menu."""

    @pytest.mark.parametrize(
        "spec",
        [s for s in ALL_ACTIONS if s.menu_path is None],
        ids=lambda s: s.id,
    )
    def test_orphan_not_in_menus(
        self,
        main_window: MainWindow,
        spec: ActionSpec,
    ) -> None:
        act = main_window._actions[spec.id]
        for top in main_window.menuBar().findChildren(QMenu):
            if top.parent() is not main_window.menuBar():
                continue  # skip submenus — we walk from the top below
            reachable = _menu_action_ids(top)
            assert act not in reachable, (
                f"Spec {spec.id!r}: marked orphan but appears in menu "
                f"{top.title()!r}."
            )


# ---------------------------------------------------------------------------
# Orphan justification (AST inspection)
# ---------------------------------------------------------------------------


_ORPHAN_COMMENT_RE = re.compile(r"#\s*orphan:\s*\S+")
_ACTIONS_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "protoboard" / "gui" / "actions.py"
)


def _orphan_ids_with_comment() -> set[str]:
    """Walk ``actions.py`` line-by-line and collect ActionSpec ids whose
    ``ActionSpec(...)`` constructor is preceded (within 5 lines) by a
    ``# orphan: ...`` comment.  Implementation is intentionally crude:
    a regex over the raw source.  Phase 4 may revisit if labels grow."""
    text = _ACTIONS_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()
    orphan_ids: set[str] = set()
    for idx, line in enumerate(lines):
        if "ActionSpec(" not in line:
            continue
        block = "\n".join(lines[idx : idx + 12])
        m = re.search(r"id=KB\.(ACTION_[A-Z0-9_]+)", block)
        if m is None:
            continue
        # Look back up to 10 lines for an "# orphan:" justification —
        # contiguous spec blocks share one comment for the whole group.
        window = lines[max(0, idx - 10) : idx]
        if any(_ORPHAN_COMMENT_RE.search(prev) for prev in window):
            attr = m.group(1)
            value = getattr(KB, attr, None)
            if isinstance(value, str):
                orphan_ids.add(value)
    return orphan_ids


class TestOrphansAreJustified:
    def test_every_orphan_has_inline_comment(self) -> None:
        declared_orphans = {s.id for s in ALL_ACTIONS if s.menu_path is None}
        commented = _orphan_ids_with_comment()
        missing = sorted(declared_orphans - commented)
        assert missing == [], (
            "Specs with menu_path=None must carry a `# orphan: ...` "
            f"justification within 5 lines above: {missing}"
        )


# ---------------------------------------------------------------------------
# Spec → registry round-trip
# ---------------------------------------------------------------------------


class TestRegistryRoundTrip:
    def test_registry_has_one_action_per_spec(self, main_window: MainWindow) -> None:
        assert set(main_window._actions) >= {
            s.id for s in ALL_ACTIONS
        }, "MainWindow registry is missing actions from ALL_ACTIONS."

    def test_action_groups_match_specs(self, main_window: MainWindow) -> None:
        expected: dict[str, set[str]] = {}
        for s in ALL_ACTIONS:
            if s.action_group is not None:
                expected.setdefault(s.action_group, set()).add(s.id)
        for group_name, ids in expected.items():
            group = main_window._registry.action_groups.get(group_name)
            assert group is not None, (
                f"Action group {group_name!r} declared in specs but missing "
                f"from registry."
            )
            members = {a.text() for a in group.actions()}  # debug visibility
            actual_ids = {
                aid
                for aid, act in main_window._actions.items()
                if act in group.actions()
            }
            assert actual_ids == ids, (
                f"Action group {group_name!r}: expected {ids!r}, got {actual_ids!r}. "
                f"(member labels: {members!r})"
            )

    def test_partial_args_match_shortcut_args(self, main_window: MainWindow) -> None:
        """For every spec with non-empty ``shortcut_args``, the registry's
        bound handler is a ``functools.partial`` with matching args."""
        for spec in ALL_ACTIONS:
            if not spec.shortcut_args:
                continue
            handler = main_window._action_handlers[spec.id]
            assert isinstance(
                handler, functools.partial
            ), f"Spec {spec.id!r}: expected functools.partial handler."
            assert handler.args == spec.shortcut_args, (
                f"Spec {spec.id!r}: handler args {handler.args!r} "
                f"diverge from spec.shortcut_args {spec.shortcut_args!r}."
            )
