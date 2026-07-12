# Controller-layer migration Implementation Plan

> **⚠️ SUPERSEDED (2026-07-12).** This plan was built on a wrong model (a
> `KeyCode` enum, tools keeping `on_key`). The settled model — KeyMap stays in
> GUI, tools are Qt-free command machines, the Controller is a facade exposing
> `tool_commands()`, default bindings in YAML — is in the rewritten spec
> `docs/superpowers/specs/2026-07-12-controller-layer-migration-design.md`. A new
> plan will be written from that spec. Do NOT execute the tasks below; they are
> kept only for the mechanical file-move details (importer lists) that remain
> valid for Stages 1–2.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Promote the scattered Controller layer (`gui/ports/*`, `services/editing/*`, `gui/tools/*` logic) into a single Qt-free `controller/` top-level package.

**Architecture:** Staged `git mv` + import repoint, one stage per commit, green suite after each. Ports move first (leaf, already Qt-free, resolves an existing layering smell), then a Qt-free `KeyCode` enum removes `Qt.Key.*` from tools, then `services/editing/` moves, then the tool logic moves stage-by-stage with the 180 KB `select_tool.py` last and alone. Renderers (`*_preview.py`, overlays) stay in `gui/`.

**Tech Stack:** Python 3.11+, PySide6, pytest + pytest-qt, `uv`, ruff/black/mypy.

## Global Constraints

- **SPDX header on every new file:** `# SPDX-License-Identifier: MIT` (first line).
- **`controller/` MUST NOT import `gui`.** Enforced at the end by grep; the same rule that protects `domain/`.
- **Dependency direction:** `gui/` (Qt adapters + renderers) → `controller/` → `domain/`/`services/`. Never reverse.
- **Renderers stay in the View:** `*_preview.py`, `drag_conflict_overlay.py`, `drag_corner_text.py`, `transform_overlays.py` are NOT migration targets.
- **Parity-before-swap (CLAUDE.md §8):** behaviour tests are repointed to the new import path in the SAME commit that moves the code; never deleted with the old code.
- **Test command:** `uv run pytest` (config: `testpaths=["tests"]`, `addopts="-v -m 'not property and not slow'"`, `pythonpath=["src","tests"]`). Run offscreen: prefix `QT_QPA_PLATFORM=offscreen` for GUI tests.
- **Git staging:** never `git add -A`/`.`. Use `git add -u` + explicit `git add <new-file>`; sanity-check `git diff --cached --name-only`.
- **Conventional Commits.** Every stage is its own commit.
- **Full green suite is the gate for every stage.** No stage proceeds on red.

**Spec:** `docs/superpowers/specs/2026-07-12-controller-layer-migration-design.md`

---

## File structure (what moves where)

```
NEW  src/protoboard/controller/__init__.py          # facade
MOVE src/protoboard/gui/ports/*        → controller/ports/*
NEW  src/protoboard/controller/ports/keycodes.py    # Qt-free KeyCode(IntEnum), values = Qt.Key ABI
MOVE src/protoboard/services/editing/* → controller/editing/*
NEW  src/protoboard/controller/tools/base.py        # ControllerTool (Qt-free) + KeyResolver protocol
MOVE src/protoboard/gui/tools/{select,place,route,armed_transform,cut,simplify}_tool.py
                                       → controller/tools/*
MOVE src/protoboard/gui/tools/{drag_commit_decision,select_prompts}.py
                                       → controller/tools/*
NEW  src/protoboard/gui/tools/gui_tools.py          # ViewToolMixin + controller tool = GUI-composed tools
STAY src/protoboard/gui/board_view_tool.py          # AbstractBoardView seam + ViewToolMixin (make_key_map/_theme)
STAY src/protoboard/gui/tools/*_preview.py, drag_conflict_overlay.py,
     drag_corner_text.py, transform_overlays.py        (renderers — View)
STAY src/protoboard/gui/{qt_inputs,qt_viewport,qt_executor}.py  (adapters, import UP)

NOTE No qt_keymap.py needed: KeyCode is an IntEnum whose members equal the
     Qt.Key ABI values, so KeyInput.key (raw int from qt_inputs) compares
     directly against KeyCode.* — no translation table, no new adapter.
```

---

## Task 0: Scaffold `controller/` package

**Files:**
- Create: `src/protoboard/controller/__init__.py`
- Modify: `pyproject.toml` (packages already `["src/protoboard"]` — nested packages are auto-discovered; verify no explicit package list needs the new dir)
- Test: `tests/unit/test_controller_package.py`

**Interfaces:**
- Produces: importable package `protoboard.controller`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_controller_package.py
# SPDX-License-Identifier: MIT
def test_controller_package_importable():
    import protoboard.controller  # noqa: F401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_controller_package.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'protoboard.controller'`

- [ ] **Step 3: Create the package**

```python
# src/protoboard/controller/__init__.py
# SPDX-License-Identifier: MIT
"""Controller layer — Qt-free edit verbs, ports, and interaction tools.

Sits between the GUI (PySide6 adapters + renderers) and the Domain/Services.
Nothing here may import ``protoboard.gui``; the same mechanical rule that
protects ``domain/`` protects this package.
"""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_controller_package.py -v`
Expected: PASS

- [ ] **Step 5: Verify setuptools discovers the package**

Run: `uv run python -c "import protoboard.controller; print(protoboard.controller.__file__)"`
Expected: prints `.../src/protoboard/controller/__init__.py`. If it fails, add `"src/protoboard/controller"` is NOT needed (find-packages is recursive); but confirm `[tool.setuptools] packages` — if it lists packages explicitly rather than using find, add the new subpackages there.

- [ ] **Step 6: Commit**

```bash
git add src/protoboard/controller/__init__.py tests/unit/test_controller_package.py
git commit -m "feat(controller): scaffold Qt-free controller/ package"
```

---

## Task 1: Move `gui/ports/` → `controller/ports/`

**Files:**
- Move (git mv): all of `src/protoboard/gui/ports/*.py` → `src/protoboard/controller/ports/`
- Modify (repoint importers — exact list below)
- Test: existing suite (no new test; `tests/gui/test_viewport_port.py` guards `CameraMath`)

**Interfaces:**
- Consumes: `protoboard.controller` package (Task 0).
- Produces: `protoboard.controller.ports` with the same public names (`Mods`, `Intent`, `PointerInput`, `WheelInput`, `KeyInput`, `LEFT_BUTTON`, `RIGHT_BUTTON`, `MIDDLE_BUTTON`, `HoverEvent`, `ActionOption`, `InteractionContext`, `StatusEvent`, `ExecutorPort`, `BlockingJob`, `HelpOverlayPort`, `ViewportPort`, `CursorKind`, `ScreenMetrics`, `ToolController`, and submodules `.inputs`, `.inputs_details`, `.viewport`, `.viewport_details`, `.executor`).

- [ ] **Step 1: Move the files**

```bash
git mv src/protoboard/gui/ports src/protoboard/controller/ports
```

- [ ] **Step 2: Repoint importers (top-level + TYPE_CHECKING + deferred)**

Rewrite `protoboard.gui.ports` → `protoboard.controller.ports` in every file/line below. These are ALL sites (verified by grep). Use exact edits:

Production code:
- `src/protoboard/services/editing/armed_transform.py`: L52 `from protoboard.controller.ports import WheelInput`; L53 `from protoboard.controller.ports.inputs_details import WheelNotchAccumulator`; L61 (TYPE_CHECKING) `from protoboard.controller.ports import KeyInput`
- `src/protoboard/gui/board_view_tool.py`: L27–39 block; L435 deferred; L453 deferred
- `src/protoboard/gui/qt_executor.py`: L17 (TYPE_CHECKING) `from protoboard.controller.ports.executor import BlockingJob`
- `src/protoboard/gui/qt_viewport.py`: L18 `from protoboard.controller.ports.viewport import CursorKind, ScreenMetrics` (and docstring L8)
- `src/protoboard/gui/qt_inputs.py`: L16 `from protoboard.controller.ports import KeyInput, Mods, PointerInput, WheelInput` (and docstring L5)
- `src/protoboard/gui/view_camera.py`: L22 (TYPE_CHECKING)
- `src/protoboard/gui/board_view.py`: L16–24 block (TYPE_CHECKING)
- `src/protoboard/gui/tool_host.py`: L21, L22, L42–48 block (TYPE_CHECKING), L195 deferred
- `src/protoboard/gui/main_window.py`: L16 (TYPE_CHECKING), L1725 deferred, L3084 deferred
- `src/protoboard/gui/tools/place_tool.py`: L45, L50 (TYPE_CHECKING)
- `src/protoboard/gui/tools/simplify_tool.py`: L31
- `src/protoboard/gui/tools/cut_tool.py`: L12, L16 (TYPE_CHECKING)
- `src/protoboard/gui/tools/armed_transform_tool.py`: L31, L36 (TYPE_CHECKING), L277 deferred, L339 deferred
- `src/protoboard/gui/selection/mouse_input.py`: L40 `from protoboard.controller.ports.inputs import Intent, Mods`
- `src/protoboard/gui/tools/select_tool.py`: L39–48 block, L3887 deferred, L3972 deferred
- `src/protoboard/gui/tools/route_tool.py`: L16, L22 (TYPE_CHECKING)

Tests:
- `tests/fakes/board_view.py`: L18 block, L27, L28, L321 deferred
- `tests/gui/input_helpers.py`: L14
- `tests/gui/test_viewport_port.py`: L14
- `tests/gui/test_headless_tool_flows.py`: L38, L270 deferred
- `tests/gui/test_view_notifications.py`: L14, L75/L83/L89 deferred, L116 deferred

Sanity grep after edits: `grep -rn "gui\.ports" src/ tests/` → **must return nothing**.

- [ ] **Step 3: Run the full suite**

Run: `QT_QPA_PLATFORM=offscreen uv run pytest`
Expected: PASS (same count as before the move). If a collection error names `gui.ports`, a repoint site was missed — grep again.

- [ ] **Step 4: Verify the smell is gone (armed_transform no longer imports gui.ports)**

Run: `grep -rn "gui" src/protoboard/services/editing/armed_transform.py`
Expected: only the `gui.selection`/`gui.tools` TYPE_CHECKING refs remain (addressed in Task 5); no `gui.ports`.

- [ ] **Step 5: Commit**

```bash
git add -u && git add src/protoboard/controller/ports
git diff --cached --name-only   # sanity-check
git commit -m "refactor(controller): move gui/ports → controller/ports"
```

---

## Task 2: Add Qt-free `KeyCode` enum, remove `Qt.Key.*` from tools

**Rationale:** `KeyInput.key` is a raw `int(event.key())` (the `Qt.Key.*` ABI value). A `KeyCode(IntEnum)` whose members equal those ABI values lets tools compare `key == KeyCode.ESCAPE` with **no Qt import** and **no DTO change** — exactly how `LEFT_BUTTON = 0x1` already mirrors `Qt.MouseButton`.

**Files:**
- Create: `src/protoboard/controller/ports/keycodes.py`
- Modify: the 6 tool logic files (remove `from PySide6.QtCore import Qt`, replace `Qt.Key.*` with `KeyCode.*`)
- Test: `tests/unit/test_keycodes.py`

**Interfaces:**
- Produces: `protoboard.controller.ports.keycodes.KeyCode` — `IntEnum` with members `RETURN=16777220`, `ENTER=16777221`, `ESCAPE=16777216`, `BACKSPACE=16777219`, `I=73`, `T=84`, `B=66`, `R=82`, `QUOTE_LEFT=96`. Because it's an `IntEnum`, `KeyInput.key (int) == KeyCode.ESCAPE` is True when the raw key matches.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_keycodes.py
# SPDX-License-Identifier: MIT
from protoboard.controller.ports.keycodes import KeyCode


def test_keycode_values_match_qt_abi():
    # ABI values captured from Qt.Key.* (stable Qt 6 ABI).
    assert int(KeyCode.ESCAPE) == 16777216
    assert int(KeyCode.RETURN) == 16777220
    assert int(KeyCode.ENTER) == 16777221
    assert int(KeyCode.BACKSPACE) == 16777219
    assert int(KeyCode.R) == 82


def test_keycode_compares_equal_to_raw_int():
    # A tool compares KeyInput.key (a raw int) against a KeyCode member.
    raw_escape = 16777216
    assert raw_escape == KeyCode.ESCAPE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_keycodes.py -v`
Expected: FAIL with `ModuleNotFoundError: ...keycodes`

- [ ] **Step 3: Create the enum**

```python
# src/protoboard/controller/ports/keycodes.py
# SPDX-License-Identifier: MIT
"""Qt-free key codes for tool key-dispatch (qt-ports §2.2).

Members equal the ``Qt.Key.*`` ABI values (stable Qt 6 ABI), mirroring how
``LEFT_BUTTON = 0x1`` mirrors ``Qt.MouseButton`` in ``inputs.py``.  Because
this is an ``IntEnum``, a tool compares ``KeyInput.key`` (a raw int carried
across the seam) directly: ``key == KeyCode.ESCAPE``.  The Qt → int mapping
already happens in ``gui.qt_inputs.key_input`` (``int(event.key())``); no
translation table is needed.
"""

from __future__ import annotations

from enum import IntEnum


class KeyCode(IntEnum):
    """The subset of keys tool logic dispatches on."""

    RETURN = 16777220
    ENTER = 16777221
    ESCAPE = 16777216
    BACKSPACE = 16777219
    I = 73
    T = 84
    B = 66
    R = 82
    QUOTE_LEFT = 96
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_keycodes.py -v`
Expected: PASS

- [ ] **Step 5: Replace `Qt.Key.*` in the tools**

For each file, remove `from PySide6.QtCore import Qt` (only if no other `Qt.*` use remains — grep each file first: `grep -n "Qt\." <file>`), add `from protoboard.controller.ports.keycodes import KeyCode`, and rewrite each comparison. Exact sites:

`route_tool.py`: L128 `if key in (KeyCode.RETURN, KeyCode.ENTER):`; L130 `elif key == KeyCode.ESCAPE:`; L132 `elif key == KeyCode.I:`; L134 `elif key == KeyCode.T:`; L136 `elif key == KeyCode.B:`

`place_tool.py`: L316 `if key in (KeyCode.RETURN, KeyCode.ENTER):`; L319 `if key == KeyCode.ESCAPE:`; L328 `if key == KeyCode.ESCAPE and self._is_phase(PhaseEnum.DRAG_PIN):`

`armed_transform_tool.py`: L348 `if key in (KeyCode.RETURN, KeyCode.ENTER):`; L350 `if key == KeyCode.ESCAPE:`

`simplify_tool.py`: L173 `if inp.key == KeyCode.ESCAPE:`

`select_tool.py`: L1591 `if key in (KeyCode.RETURN, KeyCode.ENTER):`; L1593 `if key == KeyCode.ESCAPE:`; L1595 `if key == KeyCode.BACKSPACE:`; L2230 `... or key == KeyCode.BACKSPACE:`; L2232 `... or key == KeyCode.ESCAPE:`; L2498 `if key == KeyCode.ESCAPE:`; L2524 `key == KeyCode.QUOTE_LEFT and shift`; L2529 `key == KeyCode.QUOTE_LEFT and not shift`; L2536 `or (key == KeyCode.BACKSPACE and not ctrl)`; L2544 `if key == KeyCode.R and ctrl:`; L2554 `... or (key == KeyCode.R and alt):`; L2562 `if key == KeyCode.ESCAPE:`

Note: `select_tool.py` L65 (TYPE_CHECKING) `from PySide6.QtGui import QKeyEvent` and L33 `from PySide6.QtGui import QColor` are handled in Task 6 — leave them now. `select_tool.py` still imports `QColor` (Qt), so the file is NOT Qt-free yet after this task; that's expected.

- [ ] **Step 6: Run the full suite**

Run: `QT_QPA_PLATFORM=offscreen uv run pytest`
Expected: PASS. Key-dispatch behaviour is unchanged (same int values).

- [ ] **Step 7: Verify no Qt.Key remains in tools**

Run: `grep -rn "Qt\.Key" src/protoboard/gui/tools/*.py`
Expected: nothing (excluding `*_preview.py`/overlays if any — they had none).

- [ ] **Step 8: Commit**

```bash
git add src/protoboard/controller/ports/keycodes.py tests/unit/test_keycodes.py
git add -u
git commit -m "refactor(controller): Qt-free KeyCode enum replaces Qt.Key.* in tools"
```

---

## Task 3: Route tool `.emit()` through `notify_changed()`

**Rationale:** `session.<signal>.emit()` is the last domain-notification coupling in the tools. `HeadlessSession.notify_changed()` (no-op base) is overridden by `EditorSession` to `self.layout_changed.emit()`. Routing `layout_changed` emits through `notify_changed()` keeps tools Qt-free for that signal. Two signals need dedicated Qt-free hooks that don't exist yet: `drag_moved` (1 site) and `highlight_mode_changed` (1 site — already mirrored by `notify_highlight_mode`).

**Files:**
- Modify: `src/protoboard/services/editing/headless_session.py` (add `notify_drag_moved` no-op hook)
- Modify: `src/protoboard/gui/session.py` (override `notify_drag_moved` → `drag_moved.emit()`)
- Modify: tool logic files (replace `.emit()` with the hook call)
- Test: `tests/unit/test_notify_hooks.py`

**Interfaces:**
- Consumes: `HeadlessSession.notify_changed()` (exists), `notify_highlight_mode(label)` (exists on EditorSession; add no-op base if missing).
- Produces: `HeadlessSession.notify_drag_moved()` (new no-op base hook); `EditorSession.notify_drag_moved()` override emitting `drag_moved`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_notify_hooks.py
# SPDX-License-Identifier: MIT
from protoboard.services.editing.headless_session import HeadlessSession


def test_headless_notify_drag_moved_is_noop():
    # A bare headless session has the hook and it does nothing (no panels).
    session = HeadlessSession.__new__(HeadlessSession)  # no full init needed
    # Should not raise.
    HeadlessSession.notify_drag_moved(session)
```

(If `HeadlessSession` cannot be instantiated bare, adapt to the existing headless-session fixture used in `tests/unit/test_edit_controller_scaffold.py`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_notify_hooks.py -v`
Expected: FAIL with `AttributeError: ... 'notify_drag_moved'`

- [ ] **Step 3: Add the base hook**

In `src/protoboard/services/editing/headless_session.py`, next to `notify_changed` (~L185):

```python
def notify_drag_moved(self) -> None:
    """Change hook fired on every drag step — a lightweight sibling of
    :meth:`notify_changed` that must NOT trigger panels/DRC.

    No-op in the headless base; ``EditorSession`` overrides it to emit
    ``drag_moved``.
    """
```

- [ ] **Step 4: Override in EditorSession**

In `src/protoboard/gui/session.py`, next to the `notify_changed` override (~L169):

```python
def notify_drag_moved(self) -> None:
    """Emit ``drag_moved`` (does NOT trigger panels/DRC)."""
    self.drag_moved.emit()
```

- [ ] **Step 5: Replace `.emit()` in the tools**

Replace each site with the Qt-free hook. `self._session` is typed as the `EditState` protocol; call the hook method instead of the signal.

`select_tool.py`:
- L1288 `self._session.notify_drag_moved()` (was `self._session.drag_moved.emit()`)
- L854 `self._session.notify_highlight_mode(RELATION_LABELS[relation])` (was `highlight_mode_changed.emit(...)`)
- L1520, L3545, L3565, L3615, L3633, L3669, L3796, L4233, L4255: each `self._session.notify_changed()` (was `layout_changed.emit()`)

`place_tool.py`: L602, L643 → `self._session.notify_changed()`
`cut_tool.py`: L50 → `self._session.notify_changed()`
`simplify_tool.py`: L221 → `self._session.notify_changed()`

If `notify_highlight_mode` is not on `EditState`/`HeadlessSession` base, add a no-op base method mirroring Step 3 (the EditorSession override at session.py:288 already exists).

- [ ] **Step 6: Add the drag_moved semantics to the parity test AND run the suite**

The existing GUI suite already asserts drag/layout refresh behaviour through `EditorSession` (which now routes via the hooks). Run:

Run: `QT_QPA_PLATFORM=offscreen uv run pytest tests/unit/test_notify_hooks.py tests/gui/test_view_notifications.py tests/gui/test_select_tool.py -v`
Expected: PASS. Then full suite: `QT_QPA_PLATFORM=offscreen uv run pytest` → PASS.

- [ ] **Step 7: Verify no `.emit()` remains in tool logic**

Run: `grep -rn "\.emit(" src/protoboard/gui/tools/select_tool.py src/protoboard/gui/tools/place_tool.py src/protoboard/gui/tools/cut_tool.py src/protoboard/gui/tools/simplify_tool.py`
Expected: nothing.

- [ ] **Step 8: Commit**

```bash
git add -u && git add tests/unit/test_notify_hooks.py
git commit -m "refactor(controller): route tool signals through notify_changed/notify_drag_moved hooks"
```

---

## Task 4: Move `services/editing/` → `controller/editing/`

**Files:**
- Move (git mv): `src/protoboard/services/editing/*` → `src/protoboard/controller/editing/`
- Modify (repoint importers — exact list below, from grep)

**Interfaces:**
- Consumes: `protoboard.controller` (Task 0).
- Produces: `protoboard.controller.editing` with public names `EditController`, `EditState`, `HeadlessSession` (facade), and submodules `.controller`, `.headless_session`, `.armed_transform`, `.cli`, `.drag_commit`, `.group_lock`, `.item_handle`, `.placement_conflict`, `.preview_state`, `.wire_nets`.

- [ ] **Step 1: Move the files**

```bash
git mv src/protoboard/services/editing src/protoboard/controller/editing
```

- [ ] **Step 2: Repoint INTERNAL package imports**

Inside the moved package, rewrite `protoboard.services.editing` → `protoboard.controller.editing` at:
- `__init__.py`: L17, L18
- `armed_transform.py`: L64 (TYPE_CHECKING), L445, L448 (deferred)
- `cli.py`: L41, L42, L43
- `controller.py`: L38, L57, L58, L66, L67 (TYPE_CHECKING) + all deferred sites L140/L380/L381/L404/L415/L431/L432/L467/L748/L804/L1221/L1313/L1404
- `group_lock.py`: L23; `headless_session.py`: L36, L37, L181; `item_handle.py`: L39; `preview_state.py`: L46, L47, L76; `drag_commit.py`: L20; `placement_conflict.py`: L41; `wire_nets.py`: L35

Fast path: `grep -rl "protoboard.services.editing" src/protoboard/controller/editing` then sed-replace within those files, then re-grep to confirm zero.

- [ ] **Step 3: Repoint EXTERNAL importers (production)**

Rewrite `protoboard.services.editing` → `protoboard.controller.editing`:
- `gui/placement_conflict.py`: L21, L31
- `gui/session.py`: L24, L25
- `gui/wire_net_resolver.py`: L21, L28
- `gui/selection_fsm.py`: L51 (TYPE_CHECKING), L104 (deferred)
- `gui/selection/drag_controller.py`: L370 (deferred)
- `gui/highlight/controller.py`: L100 (deferred)
- `gui/tools/armed_transform_tool.py`: L40, L41 (TYPE_CHECKING)
- `gui/tools/place_tool.py`: L52 (TYPE_CHECKING)
- `gui/tools/select_tool.py`: L56, L775 (deferred), L1478 (deferred)

- [ ] **Step 4: Repoint EXTERNAL importers (tests)**

Rewrite in: `tests/gui/test_headless_tool_flows.py:37`, `tests/gui/test_editor_session_selection.py:23`, `tests/gui/test_hit_item_id.py:15`, `tests/gui/test_edit_cli_acceptance.py:21,58`, `tests/unit/test_edit_controller_scaffold.py:24`, `tests/unit/test_edit_controller_add_wire.py:22`, `tests/unit/test_item_handle_readonly.py:24,25`, `tests/unit/test_edit_controller_group.py:22,23`, `tests/unit/test_edit_controller_armed_transform.py:22,23`, `tests/unit/test_edit_controller_rotate_flip.py:23,24`, `tests/unit/test_edit_controller_autoroute_drc.py:23`, `tests/unit/test_edit_controller_delete.py:23,24`, `tests/unit/test_item_handle_dispatch.py:27,28`, `tests/unit/test_classify_drag_commit.py:19`, `tests/unit/test_item_handle_rotate_flip.py:25,26`, `tests/unit/test_item_handle_delete_group.py:25,26`, `tests/unit/test_edit_controller_place.py:18,19`.

Sanity grep: `grep -rn "services\.editing" src/ tests/` → **must return nothing**.

- [ ] **Step 5: Run the full suite**

Run: `QT_QPA_PLATFORM=offscreen uv run pytest`
Expected: PASS. Also verify the headless CLI still runs: `uv run pytest tests/gui/test_edit_cli_acceptance.py -v` → PASS.

- [ ] **Step 6: Commit**

```bash
git add -u && git add src/protoboard/controller/editing
git diff --cached --name-only
git commit -m "refactor(controller): move services/editing → controller/editing"
```

---

## Task 4.5: Split `_Tool` — Qt-free contract base in `controller/`, View bindings stay in `gui/`

**Rationale (blocker found in planning):** every tool subclasses `_Tool(BoardViewTool)` in `gui/board_view_tool.py`. Reading the base, only THREE members are View-bound: `_theme() -> Theme` (imports `gui.theme`), `make_key_map(parent) -> KeyMap` (imports `gui.keymap`), and `_key_matches()` (calls `view.keymap.matches(...)`). Everything else — the `on_*` DTO hooks, the verb surface (`default_action`/`secondary_action`/`empty_action`), `_synth_pointer`, `key_claims`/`wants_key`/`key_dispatch`/`_dispatch_key`, `mouse_*`, `on_scene_cleared`, `perform_context_action`, `state` — is Qt-free. `AbstractBoardView.keymap` is a Protocol property, and `_key_matches` uses only `keymap.matches(key: int, mods: int, action_id: str) -> bool`. So the seam is a narrow `KeyResolver` protocol, not the whole Qt `KeyMap`.

**Files:**
- Create: `src/protoboard/controller/tools/base.py` — `ControllerTool` (Qt-free contract base), `KeyResolver` protocol
- Modify: `src/protoboard/gui/board_view_tool.py` — `BoardViewTool`/`_Tool` become a thin View mixin over `ControllerTool` that supplies `make_key_map`/`_theme` and binds a concrete keymap
- Test: `tests/unit/test_controller_tool_base.py`

**Interfaces:**
- Consumes: `protoboard.controller.editing` (`EditState`), `protoboard.controller.ports` (`PointerInput`, `KeyInput`, `WheelInput`, `Mods`, `HoverEvent`, `LEFT_BUTTON`), `protoboard.geometry.Point`.
- Produces:
  - `protoboard.controller.tools.base.KeyResolver` — `Protocol` with `matches(self, key: int, mods: int, action_id: str) -> bool`.
  - `protoboard.controller.tools.base.ControllerTool` — Qt-free tool base. Holds `self._session: EditState`, `self._view: AbstractBoardView`. Implements all Qt-free members above. `_key_matches(inp, action_id)` resolves through `self._view.keymap` (typed as `KeyResolver`; `AbstractBoardView.keymap` satisfies it). Declares `make_key_map` and `_theme` as `NotImplementedError`-raising / abstract hooks the View mixin fills.
  - `gui/board_view_tool.py`'s `_Tool` now = `class _Tool(ViewToolMixin, ControllerTool)` where `ViewToolMixin` supplies `make_key_map` (the `gui.keymap` layer builder) and `_theme` (the `gui.theme` reader).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_controller_tool_base.py
# SPDX-License-Identifier: MIT
from protoboard.controller.tools.base import ControllerTool, KeyResolver
from protoboard.controller.ports import KeyInput, Mods


class _FakeKeymap:
    def matches(self, key: int, mods: int, action_id: str) -> bool:
        return action_id == "test.hit" and key == 42


class _FakeView:
    keymap = _FakeKeymap()


def test_controller_tool_is_qt_free_source():
    import inspect
    import protoboard.controller.tools.base as base
    src = inspect.getsource(base)
    assert "PySide6" not in src
    assert "gui.keymap" not in src
    assert "gui.theme" not in src


def test_key_matches_resolves_through_view_keymap():
    tool = ControllerTool()
    tool._view = _FakeView()  # type: ignore[assignment]
    inp = KeyInput(key=42, native_mods=0, is_auto_repeat=False, mods=Mods())
    assert tool._key_matches(inp, "test.hit") is True
    assert tool._key_matches(inp, "test.miss") is False


def test_key_matches_false_without_view():
    tool = ControllerTool()
    tool._view = None  # type: ignore[assignment]
    inp = KeyInput(key=42, native_mods=0, is_auto_repeat=False, mods=Mods())
    assert tool._key_matches(inp, "test.hit") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_controller_tool_base.py -v`
Expected: FAIL with `ModuleNotFoundError: ...controller.tools.base`

- [ ] **Step 3: Create the Qt-free base**

Extract from `gui/board_view_tool.py` the `BoardViewTool` ABC contract + all Qt-free `_Tool` method bodies into `controller/tools/base.py`. Type against `EditState` (not `EditorSession`) and `AbstractBoardView`. Key points:

```python
# src/protoboard/controller/tools/base.py
# SPDX-License-Identifier: MIT
"""Qt-free interaction-tool base (the View mixin adds KeyMap/Theme bindings)."""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Mapping, Protocol

from protoboard.controller.ports import (
    HoverEvent, KeyInput, LEFT_BUTTON, Mods, PointerInput, WheelInput,
)

if TYPE_CHECKING:
    from protoboard.geometry import Point
    from protoboard.controller.editing import EditState
    # AbstractBoardView stays the View↔Controller seam; import under TYPE_CHECKING
    # ONLY (its module lives in gui/ but declares a Qt-free Protocol). If the
    # layer-guard test (Task 7) rejects even a TYPE_CHECKING gui import, move
    # AbstractBoardView's Protocol into controller/ too — see Step 3b.


class KeyResolver(Protocol):
    """The one method `_key_matches` needs from the view's KeyMap."""
    def matches(self, key: int, mods: int, action_id: str) -> bool: ...


class ControllerTool:
    """Do-nothing Qt-free tool base. View mixin supplies make_key_map/_theme."""

    def __init__(self) -> None:
        self._session: EditState = None  # type: ignore[assignment]
        self._view = None  # AbstractBoardView; None pre-activate

    def _key_matches(self, inp: KeyInput, action_id: str) -> bool:
        view = self._view
        if view is None:
            return False
        return view.keymap.matches(inp.key, inp.native_mods, action_id)

    # ... all Qt-free hooks/verbs copied verbatim from _Tool:
    #     activate/deactivate (typed EditState), on_press/on_move/on_release,
    #     on_key/on_key_release, key_claims/wants_key/key_dispatch/_dispatch_key,
    #     default_action/secondary_action/empty_action, _synth_pointer,
    #     mouse_actions/mouse_dispatch/key_actions, displayable_action_ids,
    #     on_scene_cleared, perform_context_action, state.

    def make_key_map(self, parent):  # View mixin overrides with a real KeyMap
        # Qt-free default: headless drivers have no KeyMap layer to build, so
        # the base returns the parent unchanged. ViewToolMixin overrides this
        # to build the gui.keymap layer. Returning parent keeps headless tool
        # activation working without a Qt KeyMap.
        return parent

    def _theme(self):  # View mixin overrides with the gui Theme
        # Qt-free default: headless has no theme. Returns None; the only callers
        # of _theme() are Qt-side rendering paths that never run headless.
        # (Verify with grep during Step 4 that no Qt-free code path reads it.)
        return None
```

**Verify the default is safe (Step 3 sub-check):** `grep -rn "_theme()\|self\._theme\b" src/protoboard/gui/tools/*.py` — every `_theme()` caller must be on a Qt-rendering path (preview/overlay construction), NOT in the Qt-free logic that runs headless. If any Qt-free path reads `_theme()`, that color/theme concern belongs in the preview (like `DragSeverity`, Task 6) — flag it, don't ship a `None` that a logic path dereferences.

**Step 3b (contingency):** if the Task 7 layer-guard forbids the TYPE_CHECKING `AbstractBoardView` import, move the `AbstractBoardView` Protocol definition from `gui/board_view_tool.py` into `controller/tools/base.py` (it is Qt-free except the `scene() -> QGraphicsScene` touchpoint — type that as `object` in the controller copy, or keep `scene()` in a `gui`-side sub-protocol). Decide during Step 6 based on the guard result.

- [ ] **Step 4: Make `gui/board_view_tool.py` a thin View mixin**

Replace the Qt-free `_Tool` bodies with inheritance. Keep `make_key_map` (the `gui.keymap` layer builder, verbatim from current L551–570) and `_theme` (current L394–397) in a `ViewToolMixin`:

```python
# gui/board_view_tool.py
from protoboard.controller.tools.base import ControllerTool

class ViewToolMixin:
    """Qt/View bindings a concrete GUI tool composes over ControllerTool."""
    def make_key_map(self, parent: AbstractKeyMap) -> KeyMap:
        from protoboard.gui.keymap import FinalShortcutError, KeyMap
        km = KeyMap.layer_over(parent)
        for action_id, shortcut in {**self.key_actions(), **self.mouse_actions()}.items():
            try:
                km.register_tool_actions({action_id: shortcut})
            except FinalShortcutError as exc:
                import structlog
                structlog.get_logger(__name__).warning("keymap.tool_action_rejected", error=str(exc))
        return km

    def _theme(self) -> Theme:
        from protoboard.gui.theme import DEFAULT_THEME, Theme
        t = getattr(self._view, "_theme_override", None)
        return t if isinstance(t, Theme) else DEFAULT_THEME

class _Tool(ViewToolMixin, ControllerTool):
    """Concrete GUI tool base — Qt-free contract + View bindings."""
```

Keep `BoardViewTool` (ABC) where it is IF other code references it as the abstract type; otherwise let `ControllerTool` be the contract. Verify: `grep -rn "BoardViewTool" src/ tests/` and preserve any external `isinstance`/type refs.

- [ ] **Step 5: Run the base test + full suite**

Run: `uv run pytest tests/unit/test_controller_tool_base.py -v`
Expected: PASS.
Run: `QT_QPA_PLATFORM=offscreen uv run pytest`
Expected: PASS — tool behaviour is identical (same MRO resolution of make_key_map/_theme; the split is pure relocation).

- [ ] **Step 6: Verify the base is Qt-free**

Run: `grep -nE "PySide6|gui\.keymap|gui\.theme" src/protoboard/controller/tools/base.py`
Expected: nothing. If the TYPE_CHECKING `AbstractBoardView` import trips the Task 7 guard later, apply Step 3b.

- [ ] **Step 7: Commit**

```bash
git add src/protoboard/controller/tools/base.py tests/unit/test_controller_tool_base.py
git add -u
git diff --cached --name-only
git commit -m "refactor(controller): split _Tool — Qt-free ControllerTool base + gui ViewToolMixin"
```

---

## Task 5: Move the Qt-free tools (`cut`, `simplify`, `route`, `place`, `armed`, `drag_commit_decision`, `select_prompts`)

These are the tools that (after Tasks 2–3) no longer touch `Qt.Key.*` or `.emit()` and have no `QColor`. `select_tool.py` is deferred to Task 6 (it still imports `QColor`).

**Files:**
- Move (git mv): `cut_tool.py`, `simplify_tool.py`, `route_tool.py`, `place_tool.py`, `armed_transform_tool.py`, `drag_commit_decision.py`, `select_prompts.py` → `src/protoboard/controller/tools/`
- Modify: importers of these modules (production + tests), plus `services/drag_detector.py` → `drag_commit_decision`
- **Parity tests repointed in THIS commit** (CLAUDE.md §8)

**Interfaces:**
- Consumes: `protoboard.controller.ports`, `protoboard.controller.editing`.
- Produces: `protoboard.controller.tools.{cut_tool,simplify_tool,route_tool,place_tool,armed_transform_tool,drag_commit_decision,select_prompts}` exporting the same classes (`CutTool`, `SimplifyTool`, `RouteTool`, `PlaceTool`, `ArmedTransformTool`, `DragCommitDecision`, and the prompt helpers).

- [ ] **Step 1: Confirm each mover is Qt-free FIRST (incl. base + preview coupling)**

Run: `for f in cut simplify route place armed_transform; do echo "== $f =="; grep -n "from PySide6\|QColor\|Qt\.\|\.emit(\|board_view_tool\|import.*Qt.*Preview\|from protoboard.gui" src/protoboard/gui/tools/${f}_tool.py; done`
Expected after Tasks 2–4.5: no `Qt.Key`/`.emit()`. TWO known `controller → gui` couplings remain in EACH tool and MUST be resolved as part of this move:
  1. **Base class:** each file has `from protoboard.gui.board_view_tool import _Tool` + `class XTool(_Tool)`. Repoint to `from protoboard.controller.tools.base import ControllerTool` and `class XTool(ControllerTool)`. (The GUI needs the View bindings too — see Step 3: `tool_host` composes them, OR each concrete tool keeps subclassing `_Tool` which now = `ViewToolMixin+ControllerTool`. DECISION: tools stay subclassing `_Tool` only if `_Tool` moves too. Since `_Tool` is the View mixin composition and stays in `gui/`, the moved tools subclass `ControllerTool` and the View bindings are injected — see Step 3.)
  2. **Concrete preview:** `place_tool.py:43` has TOP-LEVEL `from protoboard.gui.tools.place_preview import QtPlacePreview`; `select_tool.py:238-240` (Task 6) and others have DEFERRED `QtXPreview` imports inside `activate()`. These construct the concrete Qt preview — a `controller → gui` edge. Resolve by moving the default-preview construction OUT of the tool into `gui/tool_host.py` (which already constructs tools); the tool receives its preview only through the `preview=` constructor arg / the injected Protocol.

If a tool's ONLY remaining `from PySide6` was `Qt` for keys (now gone), remove that import.

- [ ] **Step 2: Move the files**

```bash
mkdir -p src/protoboard/controller/tools
git mv src/protoboard/gui/tools/cut_tool.py src/protoboard/controller/tools/cut_tool.py
git mv src/protoboard/gui/tools/simplify_tool.py src/protoboard/controller/tools/simplify_tool.py
git mv src/protoboard/gui/tools/route_tool.py src/protoboard/controller/tools/route_tool.py
git mv src/protoboard/gui/tools/place_tool.py src/protoboard/controller/tools/place_tool.py
git mv src/protoboard/gui/tools/armed_transform_tool.py src/protoboard/controller/tools/armed_transform_tool.py
git mv src/protoboard/gui/tools/drag_commit_decision.py src/protoboard/controller/tools/drag_commit_decision.py
git mv src/protoboard/gui/tools/select_prompts.py src/protoboard/controller/tools/select_prompts.py
```

Create `src/protoboard/controller/tools/__init__.py`:
```python
# SPDX-License-Identifier: MIT
"""Interaction tools — Qt-free controller logic (renderers stay in gui/)."""
```

- [ ] **Step 3a: Repoint each tool's base class to `ControllerTool`**

In every moved file, change `from protoboard.gui.board_view_tool import _Tool` → `from protoboard.controller.tools.base import ControllerTool`, and `class XTool(_Tool):` → `class XTool(ControllerTool):`. Also change each `def activate(self, session: EditorSession, view: AbstractBoardView)` signature's `EditorSession` type to the Qt-free `EditState` (import from `controller.editing` under TYPE_CHECKING). Behaviour is unchanged — `ControllerTool` supplies the same hooks; `make_key_map`/`_theme` fall back to the Qt-free defaults from Task 4.5.

- [ ] **Step 3b: Wire the View bindings for the GUI at construction**

The moved tools now subclass `ControllerTool` (Qt-free `make_key_map`/`_theme` defaults). In the GUI they still need the real `KeyMap`/`Theme`. `gui/tool_host.py` constructs the tools — have it construct GUI-composed subclasses that mix `ViewToolMixin` in. Add, in `gui/tool_host.py` (or a small `gui/tools/gui_tools.py`):

```python
# gui/tools/gui_tools.py  — thin GUI compositions (View bindings + controller logic)
# SPDX-License-Identifier: MIT
from protoboard.gui.board_view_tool import ViewToolMixin
from protoboard.controller.tools.place_tool import PlaceTool as _PlaceTool
from protoboard.controller.tools.route_tool import RouteTool as _RouteTool
from protoboard.controller.tools.cut_tool import CutTool as _CutTool
from protoboard.controller.tools.simplify_tool import SimplifyTool as _SimplifyTool
from protoboard.controller.tools.armed_transform_tool import ArmedTransformTool as _ArmedTransformTool


class PlaceTool(ViewToolMixin, _PlaceTool): ...
class RouteTool(ViewToolMixin, _RouteTool): ...
class CutTool(ViewToolMixin, _CutTool): ...
class SimplifyTool(ViewToolMixin, _SimplifyTool): ...
class ArmedTransformTool(ViewToolMixin, _ArmedTransformTool): ...
```

`gui/tool_host.py` imports the tools from `gui/tools/gui_tools.py`; the controller-layer classes stay pure. MRO puts `ViewToolMixin.make_key_map`/`_theme` ahead of the `ControllerTool` defaults, so GUI keeps its real KeyMap/Theme and headless keeps the Qt-free defaults.

**Alternative if the parity tests instantiate the tool classes DIRECTLY (they do — Task D):** the tests import e.g. `PlaceTool` and drive it. Point those test imports at `gui/tools/gui_tools.py` (GUI-composed) when the test exercises KeyMap/Theme behaviour, or at `controller.tools.*` (pure) when it drives headless via `HeadlessBoardView`. Decide per test by whether it touches `make_key_map`/`_theme`; default to the GUI-composed class to preserve existing behaviour.

- [ ] **Step 3c: Break the concrete-preview `controller → gui` edge**

`place_tool.py:43` has TOP-LEVEL `from protoboard.gui.tools.place_preview import QtPlacePreview`, and tools construct `QtXPreview(view.scene)` inside `activate()` when no preview was injected (e.g. `select_tool.py:238-240`). That is a forbidden `controller → gui` import. Move the default-preview construction into the GUI-composed class's `activate()` (in `gui/tools/gui_tools.py`) or into `gui/tool_host.py`: the pure controller tool keeps only the `preview: XPreview | None = None` constructor arg and the `self._preview: XPreview | None` Protocol field (import the `XPreview` Protocol under TYPE_CHECKING is still a `gui` ref — move the Protocol into `controller/tools/` alongside `base.py`, OR type the field as `object`/a local `controller` Preview protocol). Verify:

Run: `grep -rn "from protoboard.gui\|import.*Qt.*Preview\|QtPlacePreview\|QtRoutePreview\|QtSimplifyPreview" src/protoboard/controller/tools/`
Expected: nothing. Every concrete `Qt*Preview` reference lives in `gui/`.

- [ ] **Step 4: Repoint EXTERNAL importers of the moved tools**

Production: `gui/tool_host.py` constructs the tools — repoint `from protoboard.gui.tools.place_tool import PlaceTool` → `from protoboard.controller.tools.place_tool import PlaceTool` (and route/cut/simplify/armed). `services/drag_detector.py` → `from protoboard.controller.tools.drag_commit_decision import DragCommitDecision`.

Grep for all: `grep -rn "gui\.tools\.\(cut\|simplify\|route\|place\|armed_transform\|drag_commit_decision\|select_prompts\)" src/ tests/`

- [ ] **Step 5: Repoint the PARITY tests (same commit)**

Repoint tool-class imports in: `tests/gui/test_place_tool.py:16`, `tests/gui/test_route_tool.py:15`, `tests/gui/test_cut_tool.py:19`, `tests/gui/test_simplify_tool.py:18`, `tests/gui/test_armed_transform_tool.py:27,28` (ArmedTransformTool + PlaceTool; SelectTool stays gui until Task 6 — so this file imports BOTH; repoint only the moved ones), `tests/gui/test_headless_tool_flows.py:48,49` (ArmedTransformTool, PlaceTool; SelectTool at :50 stays until Task 6). The `Qt*Preview` imports in these tests STAY pointed at `gui/tools/`.

- [ ] **Step 6: Run the moved tools' parity suites, then full suite**

Run: `QT_QPA_PLATFORM=offscreen uv run pytest tests/gui/test_place_tool.py tests/gui/test_route_tool.py tests/gui/test_cut_tool.py tests/gui/test_simplify_tool.py tests/gui/test_armed_transform_tool.py -v`
Expected: PASS. Then: `QT_QPA_PLATFORM=offscreen uv run pytest` → PASS.

- [ ] **Step 7: Commit**

```bash
git add -u && git add src/protoboard/controller/tools
git diff --cached --name-only
git commit -m "refactor(controller): move Qt-free tools (place/route/cut/simplify/armed) to controller/tools"
```

---

## Task 6: Move `select_tool.py` (last, alone) — remove `QColor`, land in `controller/tools/`

**Rationale:** The 180 KB file is highest-risk. Its only remaining Qt coupling after Tasks 2–3 is `QColor` (L33 import; L1336–1337 construct amber/red severity colors; L1348 `.name(HexArgb)`; L1360 param annotation) and a TYPE_CHECKING `QKeyEvent` (L65). The severity colors belong to the drag-corner preview. Move the color choice to the preview; the tool passes a Qt-free severity value.

**Files:**
- Modify: `src/protoboard/gui/tools/select_tool.py` (remove QColor — see below), then git mv → `controller/tools/`
- Modify: `src/protoboard/gui/tools/select_preview.py` (or the drag-corner renderer) to own the QColor mapping
- Modify: importers + parity tests
- Test: `tests/gui/test_select_tool.py`, `tests/gui/test_select_tool_parity.py` (behaviour parity — MUST pass on the moved code before old path is gone)

**Interfaces:**
- Consumes: `controller.ports`, `controller.editing`, `controller.ports.keycodes.KeyCode`.
- Produces: `protoboard.controller.tools.select_tool.SelectTool`. The drag-outline severity crosses to the preview as a Qt-free value (e.g. `DragSeverity` enum or the existing hex-ARGB string chosen by a Qt-free helper) — NOT a `QColor`.

- [ ] **Step 1: Locate the QColor consumers**

Read `select_tool.py` L1330–1365. The flow: severity → `amber`/`red` `QColor` → `_set_drag_outline(color.name(HexArgb))` (already a string!) and `_show_drag_corner(text, color)`. Since `_set_drag_outline` already takes a string, the QColor is an intermediate. Decide the Qt-free carrier:
- Option (chosen): a Qt-free `DragSeverity` enum (`WARN`/`BLOCK`) the tool passes to the preview; the preview maps severity → QColor. This removes QColor from the tool entirely.

- [ ] **Step 2: Write/repoint the failing parity test against the NEW path**

Add a top-of-file marker test asserting SelectTool is importable from the new location and Qt-free-constructs no color:

```python
# tests/gui/test_select_tool_parity.py  (add)
def test_select_tool_imports_from_controller():
    from protoboard.controller.tools.select_tool import SelectTool  # noqa: F401


def test_select_tool_module_is_qt_free():
    import protoboard.controller.tools.select_tool as st
    import inspect
    src = inspect.getsource(st)
    assert "QColor" not in src
    assert "from PySide6" not in src
```

- [ ] **Step 3: Run to verify it fails**

Run: `QT_QPA_PLATFORM=offscreen uv run pytest tests/gui/test_select_tool_parity.py::test_select_tool_imports_from_controller -v`
Expected: FAIL (module not at new path yet).

- [ ] **Step 4: Add `DragSeverity` + move color mapping to the preview**

In `controller/tools/select_prompts.py` (or a small `controller/tools/drag_severity.py`):
```python
# SPDX-License-Identifier: MIT
from enum import Enum


class DragSeverity(Enum):
    """Qt-free severity of a drag conflict; the preview maps it to a color."""
    WARN = "warn"    # amber
    BLOCK = "block"  # red
```

In the drag-corner preview renderer (`gui/tools/drag_corner_text.py` or `select_preview.py`), add the mapping:
```python
from PySide6.QtGui import QColor
from protoboard.controller.tools.drag_severity import DragSeverity

_SEVERITY_COLORS = {
    DragSeverity.WARN: QColor(180, 130, 0, 220),   # was select_tool L1336
    DragSeverity.BLOCK: QColor(180, 40, 25, 230),  # was select_tool L1337
}
```
Change `_set_drag_outline` / `_show_drag_corner` call sites so the tool passes `DragSeverity`, and the preview resolves the QColor + `.name(HexArgb)`.

- [ ] **Step 5: Remove QColor + repoint base in select_tool.py**

Delete L33 `from PySide6.QtGui import QColor`, L65 `from PySide6.QtGui import QKeyEvent` (replace the `QKeyEvent` annotation with the Qt-free `KeyInput` if it's a param type — verify usage; if it's only a stale TYPE_CHECKING import, remove it). Replace L1336–1360 QColor construction with `DragSeverity.WARN`/`BLOCK` passed to the preview. Also repoint the base (same as Task 5 Step 3a): L37 `from protoboard.gui.board_view_tool import _Tool` → `from protoboard.controller.tools.base import ControllerTool`; L146 `class SelectTool(_Tool):` → `class SelectTool(ControllerTool):`; L234 `activate(self, session: EditorSession, ...)` → type `session` as `EditState`. Move the L238–240 deferred `from protoboard.gui.tools.select_preview import QtSelectPreview` default-construction OUT to the GUI-composed class (Step 7). Grep confirm: `grep -n "PySide6\|QColor\|Qt\.\|from protoboard.gui" src/protoboard/gui/tools/select_tool.py` → nothing (before the move).

- [ ] **Step 6: Move the file**

```bash
git mv src/protoboard/gui/tools/select_tool.py src/protoboard/controller/tools/select_tool.py
```

- [ ] **Step 7: Add GUI-composed SelectTool + repoint importers + parity tests**

Add the GUI composition to `gui/tools/gui_tools.py` (created in Task 5 Step 3b), moving the default `QtSelectPreview` construction here:

```python
from protoboard.controller.tools.select_tool import SelectTool as _SelectTool

class SelectTool(ViewToolMixin, _SelectTool):
    def activate(self, session, view):
        if self._preview is None:
            from protoboard.gui.tools.select_preview import QtSelectPreview
            self._preview = QtSelectPreview(view.scene)
        super().activate(session, view)
```

**Note `SimplifyTool` subclasses `SelectTool`** (`simplify_tool.py:131`): after Task 5 moved `SimplifyTool` to `controller/`, it subclasses the PURE `controller.tools.select_tool.SelectTool`. That is correct (controller→controller). The GUI-composed `SimplifyTool` in `gui_tools.py` must subclass the GUI-composed `SelectTool` so it also gets the preview default — update `gui_tools.py`'s `SimplifyTool` to `class SimplifyTool(ViewToolMixin, _SimplifyTool)` where `_SimplifyTool` already subclasses the pure `SelectTool`; verify its `activate` preview default resolves (SimplifyTool uses `QtSimplifyPreview`).

Production: `gui/tool_host.py` imports `SelectTool` from `gui/tools/gui_tools.py`. Tests: repoint `from protoboard.gui.tools.select_tool import SelectTool`. For tests that drive KeyMap/Theme behaviour point them at `gui/tools/gui_tools.py`; for headless-via-`HeadlessBoardView` tests point at `controller.tools.select_tool`. Files: `tests/gui/test_select_tool.py:22`, `test_armed_transform_tool.py:30`, `test_simplify_tool.py:131`, `test_select_tool_parity.py:41`, `test_headless_tool_flows.py:50`, `test_editor_session_selection.py:22`, `test_selection_sync_bidirectional.py:35`, `test_board_view.py:14`, `test_headless_board_view.py:18`, `test_view_notifications.py:15`, `test_optimize_selected_wires.py:21`, `tests/unit/test_selection_filter.py:10`. `QtSelectPreview` imports STAY at `gui/tools/select_preview.py`.

Sanity grep: `grep -rn "gui\.tools\.select_tool" src/ tests/` → nothing.

- [ ] **Step 8: Run SelectTool parity suites, then full suite**

Run: `QT_QPA_PLATFORM=offscreen uv run pytest tests/gui/test_select_tool.py tests/gui/test_select_tool_parity.py tests/gui/test_headless_tool_flows.py -v`
Expected: PASS (FSM, selection, delete/group behaviour intact). Then: `QT_QPA_PLATFORM=offscreen uv run pytest` → PASS.

- [ ] **Step 9: Commit**

```bash
git add -u && git add src/protoboard/controller/tools/select_tool.py
git diff --cached --name-only
git commit -m "refactor(controller): move select_tool to controller/tools, QColor → preview (DragSeverity)"
```

---

## Task 7: Cleanup + layer guard + docs

**Files:**
- Verify empty: `src/protoboard/gui/ports/` (gone), `src/protoboard/services/editing/` (gone)
- Modify: `CLAUDE.md` §5 source-layout tree; `docs/architecture/modules.md` (redraw diagram with `controller/` subgraph, remove resolved ⚠ edges)
- Test: `tests/unit/test_controller_no_gui_import.py` (layer guard)

- [ ] **Step 1: Write the layer-guard test**

```python
# tests/unit/test_controller_no_gui_import.py
# SPDX-License-Identifier: MIT
import pathlib
import re

CONTROLLER = pathlib.Path(__file__).parents[2] / "src" / "protoboard" / "controller"


def test_controller_does_not_import_gui():
    offenders = []
    for py in CONTROLLER.rglob("*.py"):
        text = py.read_text()
        for m in re.finditer(r"^\s*(from|import)\s+protoboard\.gui", text, re.MULTILINE):
            # allow TYPE_CHECKING-only refs? No — controller must be fully gui-free.
            offenders.append(f"{py}: {m.group(0)}")
    assert not offenders, "controller/ imports gui:\n" + "\n".join(offenders)
```

- [ ] **Step 2: Run it — fix any remaining offenders**

Run: `uv run pytest tests/unit/test_controller_no_gui_import.py -v`
Expected: If FAIL, it lists the exact `controller → gui` imports left (likely stale `gui.tools.*_preview` or `gui.selection` refs). Convert each to dependency injection or a Protocol under `controller/`. Iterate until PASS.

- [ ] **Step 3: Update CLAUDE.md §5 source layout**

Edit the `src/protoboard/` tree in `CLAUDE.md` §5: add the `controller/` block (`ports/`, `editing/`, `tools/`), remove `services/editing/`, and note that `gui/tools/` now holds only renderers (`*_preview.py`, overlays). Keep it factual to the post-migration tree.

- [ ] **Step 4: Redraw modules.md diagram**

In `docs/architecture/modules.md`: add a `subgraph CONTROLLER` with `ports`/`editing`/`tools`, move `EC`/`HS`/`AT`/etc. into it, delete the two resolved ⚠ edges (`AT --> PORTS`, `DD --> DCD`), and update the "Planned" section to past tense (it landed). Verify Mermaid fences stay balanced.

- [ ] **Step 5: Run full suite + smoke test**

Run: `QT_QPA_PLATFORM=offscreen uv run pytest`
Expected: PASS (full suite).
Run the app smoke test (skill `run-protoboard-designer`): launch, select, zoom, camera, rebuild.
Expected: no crash, screenshots produced.

- [ ] **Step 6: Final layer-direction grep**

Run: `grep -rn "import.*protoboard\.gui" src/protoboard/controller/`
Expected: nothing.

- [ ] **Step 7: Commit**

```bash
git add -u && git add tests/unit/test_controller_no_gui_import.py
git commit -m "chore(controller): layer guard + docs; controller/ migration complete"
```

---

## Self-review notes

- **Spec coverage:** Task 0 (scaffold), Task 1 (ports), Task 2 (KeyCode / Qt.Key), Task 3 (emit→notify), Task 4 (editing), **Task 4.5 (split `_Tool` — the blocker)**, Task 5+6 (tools; select last), Task 7 (guard + docs). Every spec stage — including the "Blocker discovered during planning: `_Tool` base is View-bound" section — maps to a task. ✓
- **Renderers stay in gui/** — enforced by never moving `*_preview.py`/overlays and the layer-guard test. ✓
- **`_Tool` split** — Qt-free contract (`ControllerTool` + `KeyResolver` protocol) in `controller/tools/base.py`; View bindings (`make_key_map`/`_theme`) in `gui` `ViewToolMixin`; GUI tools composed in `gui/tools/gui_tools.py`. Headless uses the Qt-free defaults; GUI uses the mixin via MRO. ✓
- **Parity-before-swap** — Task 5 Step 5 and Task 6 Steps 2/7 repoint behaviour tests in the same commit as the move. ✓
- **Qt-free carriers** — `KeyCode` (int-ABI enum), `DragSeverity` (enum), `notify_drag_moved` hook, `KeyResolver` protocol. No `Any`, no Qt in `controller/`. ✓
- **`controller → gui` edges resolved, not deferred:** (a) base class → `ControllerTool` (Task 5 Step 3a); (b) View bindings → `gui_tools.py` composition (Step 3b); (c) concrete `Qt*Preview` construction → moved into `gui_tools.py`/`tool_host` (Step 3c). The Task 7 layer-guard test is the backstop. Spec forbids silent deferral — every edge has a step.
- **Contingency documented:** if `AbstractBoardView`'s TYPE_CHECKING import trips the guard, Task 4.5 Step 3b moves its Protocol into `controller/`.
