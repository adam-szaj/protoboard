# Controller-layer migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Relocate the interaction tools to a Qt-free Controller layer as command machines, behind a Controller facade that exposes `tool_commands()`, driven by keys/mouse through the KeyMap seam that STAYS in GUI. Default bindings move into `KEYBINDINGS_V2_FILE`.

**Architecture:** Staged `git mv` + import repoint, one stage per commit, green suite after each. `gui/ports/` and `services/editing/` move under `controller/` first; then `_Tool` is split (Qt-free command base in controller, View bindings in a gui mixin); default bindings move to YAML; the tools relocate as command machines (`select_tool` last, alone); finally the Controller facade takes active-tool ownership + `tool_commands()`. KeyMap and renderers (`*_preview.py`) never leave `gui/`.

**Tech Stack:** Python 3.11+, PySide6, pytest + pytest-qt, `uv`, ruff/black/mypy, ruamel.yaml.

## Global Constraints

- **SPDX header on every new file:** `# SPDX-License-Identifier: MIT` (first line).
- **`controller/` MUST NOT import `gui`.** Enforced at the end by grep + a guard test (the same rule that protects `domain/`).
- **KeyMap stays in GUI.** No `KeyCode` enum. `on_key`/`Qt.Key`/`_key_matches` are input-translation and stay GUI-side. Tools lose `on_key`; they expose `commands()`.
- **Dependency direction:** `gui/` (Qt adapters + renderers + KeyMap) → `controller/` → `domain/`/`services/`. Never reverse.
- **Renderers stay in the View:** `*_preview.py`, `drag_conflict_overlay.py`, `drag_corner_text.py`, `transform_overlays.py` are NOT migration targets.
- **Parity-before-swap (CLAUDE.md §8):** behaviour tests are repointed to the new import path in the SAME commit that moves the code; never deleted with the old code.
- **Test command:** `QT_QPA_PLATFORM=offscreen uv run pytest` (config: `testpaths=["tests"]`, `addopts="-v -m 'not property and not slow'"`, `pythonpath=["src","tests"]`).
- **Git staging:** never `git add -A`/`.`. Use `git add -u` + explicit `git add <new-file>`; sanity-check `git diff --cached --name-only`.
- **Conventional Commits.** Every stage is its own commit.
- **Full green suite is the gate for every stage.** No stage proceeds on red.

**Spec:** `docs/superpowers/specs/2026-07-12-controller-layer-migration-design.md`

---

## File structure (what moves where)

```
NEW  src/protoboard/controller/__init__.py                 # facade package
MOVE src/protoboard/gui/ports/*        → controller/ports/*
MOVE src/protoboard/services/editing/* → controller/editing/*
NEW  src/protoboard/controller/tools/base.py               # ControllerTool (Qt-free: commands/verbs/lifecycle)
NEW  src/protoboard/controller/tools/__init__.py
MOVE src/protoboard/gui/tools/{select,place,route,armed_transform,cut,simplify}_tool.py → controller/tools/*
MOVE src/protoboard/gui/tools/{drag_commit_decision,select_prompts}.py → controller/tools/*
NEW  src/protoboard/gui/tools/gui_tools.py                 # ViewToolMixin + controller tool = GUI-composed
STAY src/protoboard/gui/board_view_tool.py                 # AbstractBoardView seam + ViewToolMixin (make_key_map/_theme/on_key→command)
STAY src/protoboard/gui/keymap.py                          # KeyMap seam — key/mouse → action-id (NEVER moves)
STAY src/protoboard/gui/tool_host.py                       # Qt input dispatch, pan, [?] overlay (Stage 7 sheds controller-logic)
STAY src/protoboard/gui/tools/*_preview.py, *overlay*.py   # renderers (View)
MODIFY src/protoboard/gui/keymap.py                        # Stage 4: defaults:/overrides: sections in KEYBINDINGS_V2_FILE
MODIFY src/protoboard/controller/editing/controller.py     # Stage 7: tool_commands() + dispatch_tool_command() + active-tool ownership
```

---

## Task 0: Scaffold `controller/` package

**Files:**
- Create: `src/protoboard/controller/__init__.py`
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

Sits between the GUI (PySide6 adapters + renderers + KeyMap) and the
Domain/Services. Nothing here may import ``protoboard.gui``; the same mechanical
rule that protects ``domain/`` protects this package.
"""
```

- [ ] **Step 4: Verify import + setuptools discovery**

Run: `uv run python -c "import protoboard.controller; print(protoboard.controller.__file__)"`
Expected: prints `.../src/protoboard/controller/__init__.py`. If it fails, check `[tool.setuptools]` in pyproject — if `packages` is an explicit list (not find), add the new subpackages; if it uses find/recursive, no change needed.

- [ ] **Step 5: Run full suite**

Run: `QT_QPA_PLATFORM=offscreen uv run pytest tests/unit/test_controller_package.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/protoboard/controller/__init__.py tests/unit/test_controller_package.py
git commit -m "feat(controller): scaffold Qt-free controller/ package"
```

---

## Task 1: Move `gui/ports/` → `controller/ports/`

**Files:**
- Move (git mv): all of `src/protoboard/gui/ports/*.py` → `src/protoboard/controller/ports/`
- Modify: importers (exact list below, verified by grep 2026-07-12)

**Interfaces:**
- Consumes: `protoboard.controller` (Task 0).
- Produces: `protoboard.controller.ports` with the same public names (`Mods`, `Intent`, `PointerInput`, `WheelInput`, `KeyInput`, `LEFT_BUTTON`, `RIGHT_BUTTON`, `MIDDLE_BUTTON`, `HoverEvent`, `ActionOption`, `InteractionContext`, `StatusEvent`, `ExecutorPort`, `BlockingJob`, `HelpOverlayPort`, `ViewportPort`, `CursorKind`, `ScreenMetrics`, `ToolController`) and submodules `.inputs`, `.inputs_details`, `.viewport`, `.viewport_details`, `.executor`.

- [ ] **Step 1: Move the files**

```bash
git mv src/protoboard/gui/ports src/protoboard/controller/ports
```

- [ ] **Step 2: Repoint importers**

Rewrite `protoboard.gui.ports` → `protoboard.controller.ports` in every file below (top-level, TYPE_CHECKING, and deferred/local imports). Production:
- `services/editing/armed_transform.py`: L52, L53, L61
- `gui/board_view_tool.py`: L27–39 block, L435, L453
- `gui/qt_executor.py`: L17
- `gui/qt_viewport.py`: L8 (doc), L18
- `gui/qt_inputs.py`: L5 (doc), L16
- `gui/view_camera.py`: L22
- `gui/board_view.py`: L16–24 block
- `gui/tool_host.py`: L21, L22, L42–48 block, L195
- `gui/main_window.py`: L16, L1725, L3084
- `gui/tools/place_tool.py`: L45, L50
- `gui/tools/simplify_tool.py`: L31
- `gui/tools/cut_tool.py`: L12, L16
- `gui/tools/armed_transform_tool.py`: L31, L36, L277, L339
- `gui/selection/mouse_input.py`: L40
- `gui/tools/select_tool.py`: L39–48 block, L3887, L3972
- `gui/tools/route_tool.py`: L16, L22

Tests:
- `tests/fakes/board_view.py`: L18 block, L27, L28, L321
- `tests/gui/input_helpers.py`: L14
- `tests/gui/test_viewport_port.py`: L14
- `tests/gui/test_headless_tool_flows.py`: L38, L270
- `tests/gui/test_view_notifications.py`: L14, L75, L83, L89, L116

Sanity: `grep -rn "gui\.ports" src/ tests/` → **must return nothing**.

- [ ] **Step 3: Run full suite**

Run: `QT_QPA_PLATFORM=offscreen uv run pytest`
Expected: PASS (same count as before). A collection error naming `gui.ports` ⇒ a repoint was missed; grep again.

- [ ] **Step 4: Verify the smell shrank**

Run: `grep -rn "gui\.ports" src/protoboard/services/editing/armed_transform.py`
Expected: nothing (the runtime `gui.ports` import is gone). The `gui.selection`/`gui.tools` TYPE_CHECKING refs remain (resolved in Task 5).

- [ ] **Step 5: Commit**

```bash
git add -u && git add src/protoboard/controller/ports
git diff --cached --name-only
git commit -m "refactor(controller): move gui/ports → controller/ports"
```

---

## Task 2: Move `services/editing/` → `controller/editing/`

**Files:**
- Move (git mv): `src/protoboard/services/editing/*` → `src/protoboard/controller/editing/`
- Modify: importers (exact list below, verified by grep 2026-07-12)

**Interfaces:**
- Produces: `protoboard.controller.editing` with public names `EditController`, `EditState`, `HeadlessSession`, `ResolvedPlacement` (facade) and submodules `.controller`, `.headless_session`, `.armed_transform`, `.cli`, `.drag_commit`, `.group_lock`, `.item_handle`, `.placement_conflict`, `.preview_state`, `.wire_nets`.

- [ ] **Step 1: Move the files**

```bash
git mv src/protoboard/services/editing src/protoboard/controller/editing
```

- [ ] **Step 2: Repoint INTERNAL imports**

Rewrite `protoboard.services.editing` → `protoboard.controller.editing` inside the moved package. Fast path: `grep -rl "protoboard.services.editing" src/protoboard/controller/editing` then edit each, then re-grep to confirm zero. (Sites include `__init__.py:17,18`; `armed_transform.py:64,445,448`; `cli.py:41,42,43`; `controller.py:38,57,58,66,67` + deferred L140/380/381/404/415/431/432/467/748/804/1221/1313/1404; `group_lock.py:23`; `headless_session.py:36,37,181`; `item_handle.py:39`; `preview_state.py:46,47,76`; `drag_commit.py:20`; `placement_conflict.py:41`; `wire_nets.py:35`.)

- [ ] **Step 3: Repoint EXTERNAL production importers**

Rewrite `protoboard.services.editing` → `protoboard.controller.editing`:
- `gui/placement_conflict.py`: L21, L31
- `gui/session.py`: L24, L25
- `gui/wire_net_resolver.py`: L21, L28
- `gui/selection_fsm.py`: L51, L104
- `gui/selection/drag_controller.py`: L370
- `gui/highlight/controller.py`: L100
- `gui/tools/armed_transform_tool.py`: L40, L41
- `gui/tools/place_tool.py`: L52
- `gui/tools/select_tool.py`: L56, L775, L1478

- [ ] **Step 4: Repoint EXTERNAL test importers**

Rewrite in: `tests/gui/test_headless_tool_flows.py:37`, `tests/gui/test_editor_session_selection.py:23`, `tests/gui/test_hit_item_id.py:15`, `tests/gui/test_edit_cli_acceptance.py:21,58`, `tests/unit/test_edit_controller_scaffold.py:24`, `tests/unit/test_edit_controller_add_wire.py:22`, `tests/unit/test_item_handle_readonly.py:24,25`, `tests/unit/test_edit_controller_group.py:22,23`, `tests/unit/test_edit_controller_armed_transform.py:22,23`, `tests/unit/test_edit_controller_rotate_flip.py:23,24`, `tests/unit/test_edit_controller_autoroute_drc.py:23`, `tests/unit/test_edit_controller_delete.py:23,24`, `tests/unit/test_item_handle_dispatch.py:27,28`, `tests/unit/test_classify_drag_commit.py:19`, `tests/unit/test_item_handle_rotate_flip.py:25,26`, `tests/unit/test_item_handle_delete_group.py:25,26`, `tests/unit/test_edit_controller_place.py:18,19`.

Sanity: `grep -rn "services\.editing" src/ tests/` → **must return nothing**.

- [ ] **Step 5: Run full suite + headless CLI**

Run: `QT_QPA_PLATFORM=offscreen uv run pytest`
Expected: PASS.
Run: `QT_QPA_PLATFORM=offscreen uv run pytest tests/gui/test_edit_cli_acceptance.py -v`
Expected: PASS (headless CLI intact).

- [ ] **Step 6: Commit**

```bash
git add -u && git add src/protoboard/controller/editing
git diff --cached --name-only
git commit -m "refactor(controller): move services/editing → controller/editing"
```

---

## Task 3: Split `_Tool` — Qt-free `ControllerTool` base + gui `ViewToolMixin`

**Rationale:** every tool subclasses `_Tool(BoardViewTool)` in `gui/board_view_tool.py`. The Qt-free parts (on_* DTO hooks, verb surface, lifecycle, `key_claims`/`key_dispatch` tables) move to `controller/tools/base.py`; the View bindings (`make_key_map` → `gui.keymap`, `_theme` → `gui.theme`, and — critically — the `_key_matches` that resolves through `view.keymap`) stay in `gui` as `ViewToolMixin`. `on_key` bodies STAY in GUI (they are input-translation); the controller base does not define `on_key`.

**Files:**
- Create: `src/protoboard/controller/tools/base.py`, `src/protoboard/controller/tools/__init__.py`
- Modify: `src/protoboard/gui/board_view_tool.py` (`_Tool` → `ViewToolMixin` + inheritance from `ControllerTool`)
- Test: `tests/unit/test_controller_tool_base.py`

**Interfaces:**
- Consumes: `protoboard.controller.editing` (`EditState`), `protoboard.controller.ports` (`PointerInput`, `KeyInput`, `WheelInput`, `Mods`, `HoverEvent`, `LEFT_BUTTON`), `protoboard.geometry.Point`.
- Produces:
  - `protoboard.controller.tools.base.KeyResolver` — `Protocol` with `matches(self, key: int, mods: int, action_id: str) -> bool`.
  - `protoboard.controller.tools.base.ControllerTool` — Qt-free tool base. Holds `self._session: EditState`, `self._view` (AbstractBoardView; None pre-activate). Implements the Qt-free members. `_key_matches` resolves through `self._view.keymap` typed as `KeyResolver`. `make_key_map`/`_theme` are Qt-free defaults the mixin overrides.
  - `gui/board_view_tool.py`'s `_Tool` becomes `class _Tool(ViewToolMixin, ControllerTool)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_controller_tool_base.py
# SPDX-License-Identifier: MIT
import inspect
from protoboard.controller.tools.base import ControllerTool
from protoboard.controller.ports import KeyInput, Mods


class _FakeKeymap:
    def matches(self, key: int, mods: int, action_id: str) -> bool:
        return action_id == "test.hit" and key == 42


class _FakeView:
    keymap = _FakeKeymap()


def test_controller_tool_is_qt_free_source():
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

Extract from `gui/board_view_tool.py` the `BoardViewTool` contract + the Qt-free `_Tool` bodies (the `@override` defaults at ~L464-593: `on_press`/`on_move`/`on_release`, `on_key`/`on_key_release` — NOTE these MOVE to the mixin, see Step 4; the base does NOT keep `on_key` — `key_claims`/`wants_key`/`key_dispatch`/`_dispatch_key`, `default_action`/`secondary_action`/`empty_action`, `_synth_pointer`, `mouse_actions`/`mouse_dispatch`/`key_actions`, `displayable_action_ids`, `on_scene_cleared`, `perform_context_action`, `state`). Create:

```python
# src/protoboard/controller/tools/base.py
# SPDX-License-Identifier: MIT
"""Qt-free interaction-tool base (the gui ViewToolMixin adds KeyMap/Theme/on_key)."""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Mapping, Protocol

from protoboard.controller.ports import (
    HoverEvent, KeyInput, LEFT_BUTTON, Mods, PointerInput, WheelInput,
)

if TYPE_CHECKING:
    from protoboard.geometry import Point
    from protoboard.controller.editing import EditState


class KeyResolver(Protocol):
    """The one method the tool base needs from the view's KeyMap."""
    def matches(self, key: int, mods: int, action_id: str) -> bool: ...


class ControllerTool:
    """Do-nothing Qt-free tool base. The gui mixin adds make_key_map/_theme/on_key."""

    def __init__(self) -> None:
        self._session: EditState = None  # type: ignore[assignment]
        self._view = None  # AbstractBoardView; None pre-activate

    def _key_matches(self, inp: KeyInput, action_id: str) -> bool:
        view = self._view
        if view is None:
            return False
        return view.keymap.matches(inp.key, inp.native_mods, action_id)

    # Copy verbatim the Qt-free bodies from _Tool:
    #   activate/deactivate (typed EditState), on_move/on_release,
    #   key_claims/wants_key/key_dispatch/_dispatch_key,
    #   default_action/secondary_action/empty_action, _synth_pointer,
    #   mouse_actions/mouse_dispatch/key_actions, displayable_action_ids,
    #   on_scene_cleared, perform_context_action, state.

    def make_key_map(self, parent):
        # Qt-free default: headless has no KeyMap layer to build → return parent.
        return parent

    def _theme(self):
        # Qt-free default: headless has no theme. Only Qt-rendering paths read it.
        return None
```

**Note on `AbstractBoardView`:** it is declared in `gui/board_view_tool.py`. `ControllerTool` references `self._view` untyped (or `object`) to avoid importing gui. If mypy needs the type, add a minimal local `_ViewProto(Protocol)` in base.py declaring only `keymap: KeyResolver` — do NOT import from gui.

- [ ] **Step 4: Make `gui/board_view_tool.py` a thin View mixin**

Keep `make_key_map` (the `gui.keymap` layer builder), `_theme` (the `gui.theme` reader), and the `on_key`/`on_key_release`/`on_press` bodies (input-translation) in a `ViewToolMixin`. `_Tool` becomes the composition:

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

    def on_press(self, inp): return False   # verbatim from _Tool
    def on_key(self, inp): return False     # base no-op; concrete tools override in gui_tools (Stage 5/6)
    def on_key_release(self, inp): pass

class _Tool(ViewToolMixin, ControllerTool):
    """Concrete GUI tool base — Qt-free contract + View bindings."""
```

- [ ] **Step 5: Run the base test + full suite**

Run: `uv run pytest tests/unit/test_controller_tool_base.py -v`
Expected: PASS.
Run: `QT_QPA_PLATFORM=offscreen uv run pytest`
Expected: PASS — MRO resolves `make_key_map`/`_theme`/`on_key` from the mixin ahead of the base defaults; behaviour unchanged.

- [ ] **Step 6: Verify the base is Qt-free**

Run: `grep -nE "PySide6|gui\.keymap|gui\.theme|protoboard\.gui" src/protoboard/controller/tools/base.py`
Expected: nothing.

- [ ] **Step 7: Commit**

```bash
git add src/protoboard/controller/tools/base.py src/protoboard/controller/tools/__init__.py tests/unit/test_controller_tool_base.py
git add -u
git commit -m "refactor(controller): split _Tool — Qt-free ControllerTool base + gui ViewToolMixin"
```

---

## Task 4: Default bindings → `KEYBINDINGS_V2_FILE` (`defaults:` + `overrides:`)

**Rationale:** `KEYBINDINGS_V2_FILE` today is a flat override-only tree; `save_overrides` skips entries equal to the declared default so default changes propagate (`keymap.py:665-729`). Add a top-level `defaults:` section holding each tool's `key_actions()` shortcuts (today hardcoded, e.g. `select-tool.action.reroute: "W"`), keep `overrides:` minimal.

**Files:**
- Modify: `src/protoboard/gui/keymap.py` (`load_overrides`, `save_overrides`, add `load_defaults`)
- Modify: the tools' `key_actions()` to read from the loaded defaults (or drop the literals once seeded)
- Test: `tests/gui/test_keymap_defaults_yaml.py`

**Interfaces:**
- Consumes: `KEYBINDINGS_V2_FILE` (`protoboard.paths`).
- Produces: KeyMap loads a `defaults:` section into declared bindings; `overrides:` still holds user diffs; override wins.

- [ ] **Step 1: Write the failing test**

```python
# tests/gui/test_keymap_defaults_yaml.py
# SPDX-License-Identifier: MIT
import textwrap
from protoboard.gui.keymap import GlobalKeyMap, KeyMap


def test_defaults_section_loads(tmp_path, monkeypatch):
    f = tmp_path / "keybindings.yaml"
    f.write_text(textwrap.dedent("""
        defaults:
          select-tool:
            action.reroute: "W"
        overrides:
          select-tool:
            action.reroute: "Q"
    """))
    import protoboard.paths as paths
    monkeypatch.setattr(paths, "KEYBINDINGS_V2_FILE", f)
    km = KeyMap(GlobalKeyMap.with_globals())
    km.load_defaults()
    km.load_overrides()
    # override wins over the default
    assert km.resolve("select-tool.action.reroute") == "Q"


def test_missing_defaults_section_is_backward_compatible(tmp_path, monkeypatch):
    f = tmp_path / "keybindings.yaml"
    # old-style file: flat, no defaults:/overrides: wrappers
    f.write_text("select-tool:\n  action.reroute: \"Q\"\n")
    import protoboard.paths as paths
    monkeypatch.setattr(paths, "KEYBINDINGS_V2_FILE", f)
    km = KeyMap(GlobalKeyMap.with_globals())
    km.load_defaults()   # no defaults: section → no-op, no crash
    km.load_overrides()  # old flat file still read as overrides
    assert km.resolve("select-tool.action.reroute") == "Q"
```

- [ ] **Step 2: Run to verify it fails**

Run: `QT_QPA_PLATFORM=offscreen uv run pytest tests/gui/test_keymap_defaults_yaml.py -v`
Expected: FAIL (`load_defaults` missing, or override not resolving with the new shape).

- [ ] **Step 3: Update `load_overrides` for the two-section shape**

In `keymap.py:665-701`, make `load_overrides` read the `overrides:` sub-tree when present, else fall back to treating the whole file as the flat override tree (backward compat):

```python
raw = yaml.load(KEYBINDINGS_V2_FILE)
if not isinstance(raw, dict):
    return
tree = raw.get("overrides", raw)   # new shape → overrides:; old file → whole dict
for category, entries in tree.items():
    ...  # unchanged inner loop, writing self._overrides[f"{category}.{name}"]
```

Add `load_defaults`:

```python
def load_defaults(self) -> None:
    """Read the `defaults:` section (full default bindings) if present."""
    from protoboard.paths import KEYBINDINGS_V2_FILE
    if not KEYBINDINGS_V2_FILE.exists():
        return
    try:
        from ruamel.yaml import YAML
        raw = YAML().load(KEYBINDINGS_V2_FILE)
        if not isinstance(raw, dict):
            return
        defaults = raw.get("defaults")
        if not isinstance(defaults, dict):
            return
        for category, entries in defaults.items():
            if not isinstance(category, str) or not isinstance(entries, dict):
                continue
            for name, value in entries.items():
                if isinstance(name, str) and isinstance(value, str):
                    self.declare({f"{category}.{name}": value})  # declared default
    except Exception:  # noqa: BLE001
        return
```

(Adapt `self.declare(...)` to the real KeyMap API for declaring a default binding — check the signature; if `declare` takes an action-spec dict, build it accordingly.)

- [ ] **Step 4: Update `save_overrides` to write under `overrides:`**

In `keymap.py:704-729`, wrap the minimised tree under an `overrides:` key and preserve any existing `defaults:` section (read it first, re-emit unchanged):

```python
existing_defaults = ... # read raw.get("defaults") if the file exists
out = {}
if existing_defaults:
    out["defaults"] = existing_defaults
out["overrides"] = tree   # the minimised user diffs, as today
# then yaml.dump(out, fh)
```

- [ ] **Step 5: Seed `defaults:` from the tools' key_actions() + wire load_defaults into the boot path**

Seed the shipped defaults file (or the in-repo default resource) with each tool's current `key_actions()` values:
- `select-tool`: `action.reroute: "W"`, `action.autoroute: "A"`, `action.rotate: "R"`, `action.move: "G"`, `action.flip_fragment: "F"` (the non-None entries from `select_tool.py:2585-2589`).
- `place-tool`: from `place_tool.py:267` (rotate_cw/ccw/flip/redrag — all inherit global, so may be `null`/absent).
- `armed-transform`: `action.rotate: "R"`, `action.move: "G"`, steps `"Left|A"/"Right|D"/"Up|W"/"Down|S"` (from `armed_transform_tool.py:235-241`).

Call `km.load_defaults()` wherever `km.load_overrides()` is called today (MainWindow boot / KeyMap construction) — `load_defaults` BEFORE `load_overrides` so overrides win. Grep: `grep -rn "load_overrides" src/`.

- [ ] **Step 6: Run the YAML test + full suite**

Run: `QT_QPA_PLATFORM=offscreen uv run pytest tests/gui/test_keymap_defaults_yaml.py tests/gui/test_keymap_layers.py -v`
Expected: PASS.
Run: `QT_QPA_PLATFORM=offscreen uv run pytest`
Expected: PASS — shortcuts + rebinding unchanged.

- [ ] **Step 7: Commit**

```bash
git add -u && git add tests/gui/test_keymap_defaults_yaml.py
git commit -m "feat(keymap): defaults:/overrides: sections in KEYBINDINGS_V2_FILE"
```

---

## Task 5: Move the tools' command logic → `controller/tools/`

Move `cut`, `simplify`, `route`, `place`, `armed_transform`, `drag_commit_decision`, `select_prompts`. Each tool subclasses `ControllerTool`; the GUI-composed subclass (`gui/tools/gui_tools.py`) mixes in `ViewToolMixin` and keeps `on_key`→command translation. `select_tool` is Task 6.

**Files:**
- Move (git mv): the 7 files → `src/protoboard/controller/tools/`
- Create: `src/protoboard/gui/tools/gui_tools.py`
- Modify: `gui/tool_host.py` (import GUI-composed tools), `services/drag_detector.py` (→ controller drag_commit_decision), parity tests
- **Parity tests repointed in THIS commit**

**Interfaces:**
- Consumes: `controller.tools.base.ControllerTool`, `controller.ports`, `controller.editing`.
- Produces: `controller.tools.{cut_tool,simplify_tool,route_tool,place_tool,armed_transform_tool,drag_commit_decision,select_prompts}` exporting the same classes; `gui/tools/gui_tools.py` with `PlaceTool`, `RouteTool`, `CutTool`, `SimplifyTool`, `ArmedTransformTool` = `ViewToolMixin + <controller tool>`.

- [ ] **Step 1: Move the files**

```bash
git mv src/protoboard/gui/tools/cut_tool.py src/protoboard/controller/tools/cut_tool.py
git mv src/protoboard/gui/tools/simplify_tool.py src/protoboard/controller/tools/simplify_tool.py
git mv src/protoboard/gui/tools/route_tool.py src/protoboard/controller/tools/route_tool.py
git mv src/protoboard/gui/tools/place_tool.py src/protoboard/controller/tools/place_tool.py
git mv src/protoboard/gui/tools/armed_transform_tool.py src/protoboard/controller/tools/armed_transform_tool.py
git mv src/protoboard/gui/tools/drag_commit_decision.py src/protoboard/controller/tools/drag_commit_decision.py
git mv src/protoboard/gui/tools/select_prompts.py src/protoboard/controller/tools/select_prompts.py
```

- [ ] **Step 2: Repoint each tool's base + strip Qt from the controller copy**

In each moved file: `from protoboard.gui.board_view_tool import _Tool` → `from protoboard.controller.tools.base import ControllerTool`; `class XTool(_Tool)` → `class XTool(ControllerTool)`. Move the `on_key` body (raw `Qt.Key.*` dispatch) OUT to the GUI-composed subclass (Step 3): the controller tool keeps only the command handlers (the methods `on_key` used to call — `_commit`, `_cancel`, `_set_side`, `_key_rotate`, etc.) and exposes them; the key→command mapping lives in gui. Remove `from PySide6...` from the controller copy. Type `activate(session: EditState, ...)`.

For `route_tool.py` (100% raw keys today): its `on_key` (L126-140) maps Return/Esc/I/T/B to `_commit`/`_cancel`/`_set_insulated`/`_set_side`. In the controller copy, keep those methods; the GUI subclass's `on_key` calls them. Define action-ids for route (it has none today) so the KeyMap can dispatch — add `route-tool.action.{commit,cancel,insulate,side_top,side_bottom}` and their defaults to the YAML `defaults:` (Task 4 seeding, do it here for route since route had no key_actions).

- [ ] **Step 3: Create `gui/tools/gui_tools.py` (View bindings + on_key)**

```python
# gui/tools/gui_tools.py
# SPDX-License-Identifier: MIT
from protoboard.gui.board_view_tool import ViewToolMixin
from protoboard.controller.tools.place_tool import PlaceTool as _PlaceTool
from protoboard.controller.tools.route_tool import RouteTool as _RouteTool
from protoboard.controller.tools.cut_tool import CutTool as _CutTool
from protoboard.controller.tools.simplify_tool import SimplifyTool as _SimplifyTool
from protoboard.controller.tools.armed_transform_tool import ArmedTransformTool as _ArmedTransformTool


class PlaceTool(ViewToolMixin, _PlaceTool):
    # on_key stays here: resolve action-id via KeyMap, call the controller command.
    ...  # move place_tool's on_key body here, calling self._<command>()

class RouteTool(ViewToolMixin, _RouteTool):
    ...  # route's on_key body → calls self._commit/_cancel/_set_side/etc.

class CutTool(ViewToolMixin, _CutTool): ...        # cut has no on_key
class SimplifyTool(ViewToolMixin, _SimplifyTool): ...  # Esc→_clear_draft then super
class ArmedTransformTool(ViewToolMixin, _ArmedTransformTool):
    ...  # armed's on_key body here (default preview injection too)
```

Also move each tool's default `Qt*Preview` construction (e.g. `place_tool` top-level `from ...place_preview import QtPlacePreview`) into the GUI subclass's `activate`. The controller tool keeps only the `preview: XPreview | None = None` field.

- [ ] **Step 4: Repoint importers**

`gui/tool_host.py`: import the tools from `gui/tools/gui_tools.py`. `services/drag_detector.py`: `from protoboard.controller.tools.drag_commit_decision import DragCommitDecision`. Grep: `grep -rn "gui\.tools\.\(cut\|simplify\|route\|place\|armed_transform\|drag_commit_decision\|select_prompts\)" src/ tests/`.

- [ ] **Step 5: Repoint PARITY tests (same commit)**

`tests/gui/test_place_tool.py:16`, `test_route_tool.py:15`, `test_cut_tool.py:19`, `test_simplify_tool.py:18`, `test_armed_transform_tool.py:27,28` (ArmedTransformTool + PlaceTool; SelectTool at :30 stays gui until Task 6), `test_headless_tool_flows.py:48,49` (SelectTool at :50 stays). Point tests that drive KeyMap/on_key behaviour at `gui/tools/gui_tools.py`; tests that drive headless-via-controller at `controller.tools.*`. `Qt*Preview` imports stay at `gui/tools/`.

- [ ] **Step 6: Run parity suites, then full suite**

Run: `QT_QPA_PLATFORM=offscreen uv run pytest tests/gui/test_place_tool.py tests/gui/test_route_tool.py tests/gui/test_cut_tool.py tests/gui/test_simplify_tool.py tests/gui/test_armed_transform_tool.py -v`
Expected: PASS.
Run: `QT_QPA_PLATFORM=offscreen uv run pytest`
Expected: PASS.

- [ ] **Step 7: Verify moved controller tools are Qt-free**

Run: `grep -rnE "PySide6|Qt\.Key|from protoboard\.gui" src/protoboard/controller/tools/*.py` (excluding select_tool, still gui)
Expected: nothing.

- [ ] **Step 8: Commit**

```bash
git add -u && git add src/protoboard/controller/tools src/protoboard/gui/tools/gui_tools.py
git diff --cached --name-only
git commit -m "refactor(controller): relocate tools as Qt-free command machines (select_tool deferred)"
```

---

## Task 6: Move `select_tool.py` (last, alone)

**Files:**
- Modify: `gui/tools/select_tool.py` (strip `on_key`/`Qt.Key`/`QColor` → gui mixin/preview), then git mv → `controller/tools/`
- Modify: `gui/tools/select_preview.py` or drag-corner renderer (own the QColor mapping via a Qt-free severity)
- Modify: `gui/tools/gui_tools.py` (add GUI-composed SelectTool with on_key), importers + parity tests
- Test: `tests/gui/test_select_tool.py`, `tests/gui/test_select_tool_parity.py`

**Interfaces:**
- Produces: `controller.tools.select_tool.SelectTool` (Qt-free command machine); `gui/tools/gui_tools.py::SelectTool` (ViewToolMixin + on_key).

- [ ] **Step 1: Write/repoint parity markers**

```python
# tests/gui/test_select_tool_parity.py (add)
def test_select_tool_imports_from_controller():
    from protoboard.controller.tools.select_tool import SelectTool  # noqa: F401


def test_select_tool_module_is_qt_free():
    import inspect, protoboard.controller.tools.select_tool as st
    src = inspect.getsource(st)
    assert "QColor" not in src and "from PySide6" not in src and "Qt.Key" not in src
```

- [ ] **Step 2: Run to verify it fails**

Run: `QT_QPA_PLATFORM=offscreen uv run pytest tests/gui/test_select_tool_parity.py::test_select_tool_imports_from_controller -v`
Expected: FAIL (not at new path).

- [ ] **Step 3: Move QColor → preview via Qt-free severity**

`select_tool.py:1336-1337` builds `amber`/`red` QColor by drag severity; `_set_drag_outline` already takes a string. Add a Qt-free `DragSeverity` enum in `controller/tools/select_prompts.py`:

```python
from enum import Enum
class DragSeverity(Enum):
    WARN = "warn"   # amber
    BLOCK = "block" # red
```

In the drag-corner preview (`gui/tools/drag_corner_text.py` or `select_preview.py`), map severity → QColor:
```python
_SEVERITY_COLORS = {DragSeverity.WARN: QColor(180,130,0,220), DragSeverity.BLOCK: QColor(180,40,25,230)}
```
The tool passes `DragSeverity`; the preview resolves the QColor + `.name(HexArgb)`.

- [ ] **Step 4: Strip Qt from select_tool + move on_key to gui**

Delete `select_tool.py:33` `from PySide6.QtGui import QColor`, `:65` `QKeyEvent` (replace annotation with Qt-free `KeyInput` if a param; else drop). Move the `on_key` body (L2490-2571, all raw `Qt.Key.*` branches) + `_key_to_intent` (L1583-1597) OUT to the GUI-composed SelectTool. The controller SelectTool keeps the command methods those branches called (`_dispatch_intent`, `_clear_pivot`, `_delete_selection`, `_flip_selected`, `arm_move`, `arm_rotate`, `_cycle_relation`, `begin_redrag`, `exit_group`, `_wire_edit_*`) and exposes them; the key→command mapping lives in gui. Repoint base: `from protoboard.controller.tools.base import ControllerTool`; `class SelectTool(ControllerTool)`; `activate(session: EditState, ...)`. Move the L238-240 deferred `QtSelectPreview` default to the GUI subclass. Grep: `grep -nE "PySide6|QColor|Qt\.|from protoboard.gui" src/protoboard/gui/tools/select_tool.py` → nothing.

- [ ] **Step 5: Move the file**

```bash
git mv src/protoboard/gui/tools/select_tool.py src/protoboard/controller/tools/select_tool.py
```

- [ ] **Step 6: Add GUI-composed SelectTool + repoint importers/tests**

In `gui/tools/gui_tools.py`:
```python
from protoboard.controller.tools.select_tool import SelectTool as _SelectTool
class SelectTool(ViewToolMixin, _SelectTool):
    def activate(self, session, view):
        if self._preview is None:
            from protoboard.gui.tools.select_preview import QtSelectPreview
            self._preview = QtSelectPreview(view.scene)
        super().activate(session, view)
    # on_key body (raw-key → command) lives here
```
`SimplifyTool` (Task 5) subclasses the pure controller `SelectTool`; its GUI-composed form subclasses the GUI-composed `SelectTool`. Repoint `from protoboard.gui.tools.select_tool import SelectTool` in: `test_select_tool.py:22`, `test_armed_transform_tool.py:30`, `test_simplify_tool.py:131`, `test_select_tool_parity.py:41`, `test_headless_tool_flows.py:50`, `test_editor_session_selection.py:22`, `test_selection_sync_bidirectional.py:35`, `test_board_view.py:14`, `test_headless_board_view.py:18`, `test_view_notifications.py:15`, `test_optimize_selected_wires.py:21`, `tests/unit/test_selection_filter.py:10`. KeyMap/on_key-driving tests → `gui_tools.py`; headless → `controller.tools.select_tool`. `tool_host.py` constructs from `gui_tools.py`.

- [ ] **Step 7: Run SelectTool parity suites, then full suite**

Run: `QT_QPA_PLATFORM=offscreen uv run pytest tests/gui/test_select_tool.py tests/gui/test_select_tool_parity.py tests/gui/test_headless_tool_flows.py -v`
Expected: PASS (FSM, selection, delete/group intact).
Run: `QT_QPA_PLATFORM=offscreen uv run pytest`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add -u && git add src/protoboard/controller/tools/select_tool.py
git diff --cached --name-only
git commit -m "refactor(controller): move select_tool to controller/tools, QColor→preview, on_key→gui"
```

---

## Task 7: Controller facade — `tool_commands()`, `dispatch_tool_command()`, active-tool ownership

**Rationale:** now that tools are Qt-free command machines, the Controller owns which tool is active and exposes its commands. `EditController` (`controller/editing/controller.py`) holds `self._state: EditState`; add active-tool state + command aggregation. Move `_active_tool`/`set_tool` state and `get_context_at` aggregation out of `ToolHost`; delete `trigger_tool_action`. Add an `active_tool_changed` notify hook the View consumes to swap the KeyMap layer.

**Files:**
- Modify: `src/protoboard/controller/editing/controller.py` (facade methods + active-tool state)
- Modify: `src/protoboard/controller/editing/headless_session.py` + `gui/session.py` (add `notify_active_tool_changed` hook + `active_tool_changed` signal)
- Modify: `gui/tool_host.py` (delegate active-tool to controller; consume the notify to swap KeyMap; delete `trigger_tool_action`, route menu through `dispatch_tool_command`)
- Test: `tests/unit/test_controller_facade.py`, `tests/gui/test_view_notifications.py` (parity), `tests/gui/test_board_view.py` (RMB menu parity)

**Interfaces:**
- Produces on `EditController`:
  - `active_tool -> ControllerTool | None`
  - `set_active_tool(tool: ControllerTool | None) -> None` — sets state, fires `state.notify_active_tool_changed()`.
  - `tool_commands() -> dict[str, Callable[[], object]]` — the active tool's action-id → command (from the tool's exposed commands / `displayable_action_ids` + dispatch table).
  - `dispatch_tool_command(action_id: str) -> bool` — run the active tool's command for `action_id`; True if handled.
- Produces on session: `notify_active_tool_changed()` (HeadlessSession no-op base; `EditorSession` emits `active_tool_changed = Signal()`).

- [ ] **Step 1: Write the failing facade test**

```python
# tests/unit/test_controller_facade.py
# SPDX-License-Identifier: MIT
from protoboard.controller.editing import HeadlessSession
from protoboard.controller.tools.select_tool import SelectTool


def test_set_and_get_active_tool():
    session = HeadlessSession(...)  # use the same fixture as test_edit_controller_scaffold
    ctl = session.edit_controller
    tool = SelectTool()
    ctl.set_active_tool(tool)
    assert ctl.active_tool is tool


def test_tool_commands_lists_action_ids():
    session = HeadlessSession(...)
    ctl = session.edit_controller
    ctl.set_active_tool(SelectTool())
    cmds = ctl.tool_commands()
    assert "select-tool.action.delete" in cmds


def test_dispatch_tool_command_runs_the_command():
    # arrange a selection, then dispatch delete by action-id (no keys involved)
    session = HeadlessSession(...)
    ctl = session.edit_controller
    ctl.set_active_tool(SelectTool())
    # ... set up a deletable selection via controller verbs ...
    handled = ctl.dispatch_tool_command("select-tool.action.delete")
    assert handled is True
```

(Fill the `HeadlessSession(...)` construction + selection setup from the existing pattern in `tests/unit/test_edit_controller_delete.py`.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_controller_facade.py -v`
Expected: FAIL (`set_active_tool`/`tool_commands`/`dispatch_tool_command` missing).

- [ ] **Step 3: Add the facade methods to EditController**

In `controller/editing/controller.py`, add to `__init__`: `self._active_tool = None`. Add:

```python
@property
def active_tool(self):
    return self._active_tool

def set_active_tool(self, tool) -> None:
    self._active_tool = tool
    self._state.notify_active_tool_changed()

def tool_commands(self) -> dict[str, object]:
    tool = self._active_tool
    if tool is None:
        return {}
    # action-id → handler, from the tool's dispatch table + displayable actions
    return dict(tool.command_table())   # see Step 4: tools expose command_table()

def dispatch_tool_command(self, action_id: str) -> bool:
    tool = self._active_tool
    if tool is None:
        return False
    handler = self.tool_commands().get(action_id)
    if handler is None:
        return False
    handler()
    return True
```

- [ ] **Step 4: Give tools a `command_table()` (action-id → callable)**

On `ControllerTool` (base), add a default `command_table() -> dict[str, Callable[[], None]]` returning `{}`. Each tool overrides it to expose its commands keyed by action-id — reuse the existing `key_dispatch()` table (place/armed already have it) and SelectTool's dispatch branches. This is the data the KeyMap resolves an action-id INTO. Example (place_tool): `return {_ACT_ROTATE_CW: lambda: self._key_rotate(90), ...}`.

- [ ] **Step 5: Add the notify hook + signal**

`controller/editing/headless_session.py`: add no-op `notify_active_tool_changed(self) -> None`. `gui/session.py`: add `active_tool_changed = Signal()` and override `notify_active_tool_changed` → `self.active_tool_changed.emit()`.

- [ ] **Step 6: Rewire ToolHost — delegate active-tool + swap KeyMap on the signal**

`gui/tool_host.py`: `set_tool` now calls `view.session.edit_controller.set_active_tool(tool)` for ownership; ToolHost subscribes to `active_tool_changed` and, on fire, rebuilds the KeyMap layer (`self._tool_keymap = tool.make_key_map(self._base_keymap)` — via the GUI-composed tool's mixin). `active_tool` property reads `edit_controller.active_tool`. Move `get_context_at`'s command aggregation to use `edit_controller.tool_commands()`. Delete `trigger_tool_action`; route `perform_context_action`'s plain-option branch and the menu (`main_window.py:3132`, `board_view.py:761`, `test_board_view.py:274`) through `edit_controller.dispatch_tool_command(action_id)`.

- [ ] **Step 7: Run facade + parity suites, then full suite**

Run: `uv run pytest tests/unit/test_controller_facade.py -v`
Expected: PASS.
Run: `QT_QPA_PLATFORM=offscreen uv run pytest tests/gui/test_view_notifications.py tests/gui/test_board_view.py tests/gui/test_headless_tool_flows.py -v`
Expected: PASS (RMB menu, active-tool, KeyMap-swap parity).
Run: `QT_QPA_PLATFORM=offscreen uv run pytest`
Expected: PASS.

- [ ] **Step 8: CLI test — drive a tool by action-id, zero keys**

Add to `tests/gui/test_edit_cli_acceptance.py` (or a new headless test): activate a tool via `controller.set_active_tool(...)`, then `controller.dispatch_tool_command("select-tool.action.delete")` — assert the effect, never touching a key. This is the north-star proof.

Run: `uv run pytest tests/gui/test_edit_cli_acceptance.py -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add -u && git add tests/unit/test_controller_facade.py
git commit -m "feat(controller): facade — tool_commands/dispatch_tool_command + active-tool ownership; drop trigger_tool_action"
```

---

## Task 8: Cleanup + layer guard + docs

**Files:**
- Test: `tests/unit/test_controller_no_gui_import.py`
- Modify: `CLAUDE.md` §5, `docs/architecture/modules.md`

- [ ] **Step 1: Write the layer-guard test**

```python
# tests/unit/test_controller_no_gui_import.py
# SPDX-License-Identifier: MIT
import pathlib, re
CONTROLLER = pathlib.Path(__file__).parents[2] / "src" / "protoboard" / "controller"

def test_controller_does_not_import_gui():
    offenders = []
    for py in CONTROLLER.rglob("*.py"):
        for m in re.finditer(r"^\s*(from|import)\s+protoboard\.gui", py.read_text(), re.MULTILINE):
            offenders.append(f"{py}: {m.group(0)}")
    assert not offenders, "controller/ imports gui:\n" + "\n".join(offenders)
```

- [ ] **Step 2: Run + fix offenders**

Run: `uv run pytest tests/unit/test_controller_no_gui_import.py -v`
Expected: If FAIL, it lists exact `controller → gui` imports. Convert each to DI / a controller-side protocol. Iterate to PASS.

- [ ] **Step 3: Update CLAUDE.md §5**

Add the `controller/` block (`ports/`, `editing/`, `tools/`); remove `services/editing/`; note `gui/tools/` holds only renderers + `gui_tools.py`; note KeyMap stays in `gui/`.

- [ ] **Step 4: Redraw modules.md**

Add a `subgraph CONTROLLER` (ports/editing/tools); move EC/HS into it; delete the resolved ⚠ edges (`AT --> PORTS`, `DD --> DCD`); show KeyMap staying in GUI as the seam; update the "Planned" section to past tense. Verify Mermaid fences balanced.

- [ ] **Step 5: Full suite + smoke**

Run: `QT_QPA_PLATFORM=offscreen uv run pytest`
Expected: PASS.
Run the smoke driver (skill `run-protoboard-designer`): launch, select, zoom, camera, rebuild.
Expected: no crash.

- [ ] **Step 6: Final grep**

Run: `grep -rn "import.*protoboard\.gui" src/protoboard/controller/`
Expected: nothing.

- [ ] **Step 7: Commit**

```bash
git add -u && git add tests/unit/test_controller_no_gui_import.py
git commit -m "chore(controller): layer guard + docs; controller/ migration complete"
```

---

## Self-review notes

- **Spec coverage:** Task 0 (scaffold), 1 (ports), 2 (editing), 3 (split `_Tool`), 4 (YAML defaults), 5 (tools→command machines), 6 (select_tool), 7 (facade: tool_commands/dispatch/active-tool ownership), 8 (guard+docs). Every spec stage maps. ✓
- **KeyMap stays in GUI** — no KeyCode; `on_key`/`Qt.Key` moved to `gui_tools.py`/`ViewToolMixin`, never into `controller/`. ✓
- **Facade after relocation** — Task 7 depends on Qt-free tools from Tasks 5-6 (no bridge protocol). ✓
- **Parity-before-swap** — Tasks 5/6 repoint behaviour tests in the move commit; Task 7 has RMB-menu + notification parity. ✓
- **Qt-free carriers** — `KeyResolver`/`DragSeverity`/`command_table()`/`notify_active_tool_changed`. No `Any`, no Qt in `controller/`. ✓
- **North-star CLI test** (Task 7 Step 8): a script drives a tool by action-id with zero keys — the boundary proof.
- **Known contingency:** if `command_table()` for SelectTool proves large (many branches), it's still Task 5/6 work (expose the existing dispatch branches as callables), not deferred.
