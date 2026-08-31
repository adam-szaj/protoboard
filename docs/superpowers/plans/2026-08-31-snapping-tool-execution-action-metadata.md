# Snapping, Tool Execution, and Action Metadata Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish Workstream 3C by removing duplicated snap selection, direct tool command execution, and parallel action metadata declarations.

**Architecture:** A pure domain snap operation receives a `SnapRequest` containing the cursor and already-authorized nearby pad/pin candidates, applies one tie-breaking rule, and returns the half-grid fallback. Controller and Qt view adapters retain only candidate gathering. An `EditController` command-execution port owns stack invocation, rejection handling, and change notification; tools call that port instead of casting into `CommandStack`. `ActionSpec` becomes the authority for labels, default shortcuts, categories, and enablement; keybinding persistence derives its defaults and display labels from the specs.

**Tech Stack:** Python 3.11+, dataclasses, Protocols, pytest, PySide6 GUI tests, Ruff, Black, mypy, Pyright.

**Spec:** `docs/superpowers/specs/2026-08-30-module-boundaries-and-deduplication-design.md`

## Global Constraints

- Preserve snap tie-breaking, accessibility filtering, command undo/redo, and all existing action identifiers/default shortcuts.
- Keep candidate authorization and GUI access at adapters; the pure snap operation imports no Qt or session state.
- Keep command serialization and YAML shortcut format unchanged.
- Use TDD and commit each completed task separately; do not merge until the workstream gate is complete.
- Defer interactive GUI smoke testing to the parent goal's final gate.

---

### Task 1: Centralize pure snap selection

**Files:**
- Create: `src/protoboard/domain/layout/snapping.py`
- Modify: `src/protoboard/controller/editing/controller.py`
- Modify: `src/protoboard/gui/board_view.py`
- Test: `tests/unit/test_snapping.py`
- Test: `tests/unit/test_edit_controller_armed_transform.py`
- Test: `tests/gui/test_board_view.py`

**Interfaces:**
- Produces frozen `SnapRequest(scene_point, pad_candidates, pin_candidates)` and `snap_point(request) -> Point`.
- `snap_point` chooses the nearest candidate by squared distance, evaluates pads before pins so exact ties prefer pads, and falls back to `Point(floor(x)+0.5, floor(y)+0.5)`.
- Controller/view continue filtering locked, exported, and excluded pins before constructing the request.

- [x] **Step 1: Add failing pure-operation tests**

Cover pad-before-pin tie-breaking, nearest candidate selection, empty candidates, and negative-coordinate half-grid fallback.

- [x] **Step 2: Verify tests fail**

Run:
```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_snapping.py -q
```
Expected: FAIL because the pure snap module does not exist.

- [x] **Step 3: Implement and migrate adapters**

Implement the request and pure operation. Replace only the duplicated nearest-candidate/fallback blocks in `EditController.snap` and `BoardView.snap`; keep preview-state and group accessibility gathering in their current owners.

- [x] **Step 4: Verify snap parity and static checks**

Run:
```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_snapping.py tests/unit/test_edit_controller_armed_transform.py tests/gui/test_board_view.py -q
/home/yvdev/.local/venv/bin/uv run ruff check src/protoboard/domain/layout/snapping.py src/protoboard/controller/editing/controller.py src/protoboard/gui/board_view.py tests/unit/test_snapping.py
/home/yvdev/.local/venv/bin/uv run mypy --strict src/protoboard/domain/layout/snapping.py src/protoboard/controller/editing/controller.py
```

- [x] **Step 5: Commit**

```bash
git add src/protoboard/domain/layout/snapping.py src/protoboard/controller/editing/controller.py src/protoboard/gui/board_view.py tests/unit/test_snapping.py tests/unit/test_edit_controller_armed_transform.py tests/gui/test_board_view.py
git commit -m "refactor(snapping): centralize candidate selection"
```

### Task 2: Route tool command execution through the controller port

**Files:**
- Modify: `src/protoboard/controller/editing/controller.py`
- Modify: `src/protoboard/controller/tools/select_tool.py`
- Modify: `src/protoboard/controller/tools/wire_edit_tool.py`
- Test: `tests/unit/test_edit_service_ports.py`

**Interfaces:**
- Produces `EditController.execute_command(command: object) -> bool` and `execute_command_silent(command: object) -> bool` as the command execution port.
- Both methods catch `CommandRejectedError`; the notifying form emits one model-change notification only after success, while the silent form emits none.
- Select and wire-edit tools use the port for single and multi-command operations; no tool reaches `session.stack.execute` directly.

- [x] **Step 1: Add failing execution-port contract tests**

Assert success/rejection behaviour and notification count through the controller facade, and add an AST assertion that the two migrated tools contain no `stack.execute` call.

- [x] **Step 2: Verify the tests fail**

Run:
```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_edit_service_ports.py tests/gui/test_select_tool.py -q
```
Expected: FAIL because the public execution-port methods and AST contract do not exist.

- [x] **Step 3: Implement and migrate callers**

Promote the existing controller execution substrate to the two public methods, preserve its casts at the controller boundary, and replace `_execute_command`/`_exec_command` stack calls in the selected tools with the port.

- [x] **Step 4: Verify command behaviour and static checks**

Run the focused tests above plus `tests/gui/test_wire_edit_tool.py`, Ruff, Black, strict mypy, and Pyright for changed files.

- [x] **Step 5: Commit**

```bash
git add src/protoboard/controller/editing/controller.py src/protoboard/controller/editing/contexts.py src/protoboard/controller/tools/select_tool.py src/protoboard/controller/tools/wire_edit_tool.py tests/unit/test_edit_service_ports.py tests/gui/test_select_tool.py
git commit -m "refactor(controller): centralize tool command execution"
```

### Task 3: Make action specs authoritative for metadata

**Files:**
- Modify: `src/protoboard/gui/actions.py`
- Modify: `src/protoboard/gui/keybindings.py`
- Test: `tests/gui/test_action_registry.py`

**Interfaces:**
- `ActionSpec` gains `default_shortcut`, `category`, and existing `enabled` remains authoritative; labels continue to derive display names.
- `ALL_ACTIONS` contains every default shortcut currently in `keybindings.DEFAULTS`.
- `KeyBindings` derives defaults/display names from `ALL_ACTIONS`; persistence still writes only non-default overrides and reads the same YAML keys.
- Duplicate `DISPLAY_NAMES` and shortcut-default tables are removed after parity tests pass.

- [x] **Step 1: Add failing metadata-parity tests**

Assert every `ALL_ACTIONS` id has one spec, a default shortcut matching the current binding, and a category/display label; assert keybinding defaults and display labels are derived from the spec registry.

- [x] **Step 2: Verify tests fail**

Run:
```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/gui/test_action_registry.py tests/gui/test_keymap.py -q
```
Expected: FAIL because `ActionSpec` does not yet carry the metadata and keybindings still own parallel tables.

- [x] **Step 3: Migrate metadata without changing identifiers**

Add the fields to the specs, transfer all existing values mechanically, derive keybinding lookup/display data, and delete only the superseded tables and compatibility-only tests.

- [x] **Step 4: Verify registry/YAML parity and static checks**

Run the focused GUI tests, full action/keymap tests, Ruff, Black, strict mypy, and Pyright.

- [x] **Step 5: Commit**

```bash
git add src/protoboard/gui/actions.py src/protoboard/gui/keybindings.py src/protoboard/gui/action_builder.py tests/gui/test_action_registry.py tests/gui/test_keymap.py
git commit -m "refactor(gui): centralize action metadata"
```

### Task 4: Workstream 3C verification and status record

**Files:**
- Modify: `docs/superpowers/plans/2026-08-31-snapping-tool-execution-action-metadata.md`
- Modify: `docs/superpowers/specs/2026-08-30-module-boundaries-and-deduplication-design.md`

- [ ] **Step 1: Run the full relevant automated gate**

Run full unit and offscreen GUI suites, the marked property suite if production routing inputs changed, Ruff, Black, strict mypy, Pyright, and `git diff --check`.

- [ ] **Step 2: Audit superseded paths**

Verify one snap operation, no direct stack execution in migrated tools, one authoritative action metadata registry, and no stale private aliases remain.

- [ ] **Step 3: Update design and plan status**

Mark this plan complete, record test totals/LOC metrics, identify Workstream 4 (GUI/controller decomposition, comment cleanup, final audit) as next, and retain the manual-GUI deferral.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/plans/2026-08-31-snapping-tool-execution-action-metadata.md docs/superpowers/specs/2026-08-30-module-boundaries-and-deduplication-design.md
git commit -m "docs(architecture): complete snapping and action metadata"
```

## Self-Review Checklist

- [ ] Snap tie-breaking and accessibility semantics are unchanged.
- [ ] Tools have no direct command-stack access.
- [ ] Action identifiers and persisted shortcuts remain backward compatible.
- [ ] Every task has its own verified commit and no merge was performed.
