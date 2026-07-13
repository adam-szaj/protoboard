# Controller-layer migration — tools & KeyMap seam to a real Controller facade

**Date:** 2026-07-12 (rewritten from the earlier KeyCode-based draft)
**Status:** Design in progress
**Author:** Adam Szaj (with Claude Code)

> This spec was rewritten after re-reading memory + `docs/keymap-per-tool-layer.md`
> + `docs/mvc-verb-layer.md`. The earlier draft proposed a `KeyCode` enum and
> kept `on_key` in the tools — both wrong. The settled model (see memory
> `project_tools_belong_to_controller`, refinements 2026-07-12) is below.

## The settled model (do not re-litigate)

1. **KeyMap is a seam that STAYS IN GUI.** Its only job: translate GUI input
   (key + mouse) → a Controller command (action-id). It does not move.
2. **A tool is a machine of COMMANDS with zero knowledge of keys.** No `on_key`,
   `key_actions`, `key_claims`, `make_key_map`, `Qt.Key`, `Intent` on a tool.
3. **The Controller is a FACADE.** The View talks only to the Controller
   instance; the GUI does not reach around it into `session.layout` or tools.
4. **The Controller exposes tool-commands as data.** A controller function
   returns the active tool's available commands (action-id → command); the GUI
   builds the **Key Bindings panel** (Properties) from that.
5. **Default keybindings live in a YAML file**, not hardcoded in `key_actions()`.
6. **`ToolHost` already holds Controller logic to extract** (audited 2026-07-12):
   active-tool ownership (`_active_tool`/`set_tool`), `get_context_at`
   (tool-commands aggregation), `trigger_tool_action` (a keycode round-trip
   anti-pattern). Input dispatch / pan / `[?]` overlay stay in GUI.

## What already exists (REUSE, do not rebuild)

- **`services/editing/`** — Qt-free `EditController` + `HeadlessSession` + CLI
  (the Controller core). Verbs: delete/rotate/flip/place/add_wire/begin_transform
  /autoroute/DRC/save/export. `EditState` protocol; `EditorSession(HeadlessSession,
  QObject)` adds Qt signals.
- **`gui/keymap.py`** — per-tool KeyMap layer with `lookup(trigger) → action_id`
  (reverse, leaf-first shadowing), shared user-override store, `KEYBINDINGS_V2_FILE`
  persistence. Stages 1/1b/2/3 shipped 2026-07-09 (commits bcd53d9…809e973).
- **`gui/ports/`** — Qt-free DTOs/protocols (the View↔Controller vocabulary).
- **The verb seam** (`default_action`/`secondary_action`/`empty_action`,
  `controller_state_changed`) — shipped 2026-07-11 (docs/mvc-verb-layer.md).
- **`get_context_at`, `trigger_tool_action`, `_active_tool`** in `tool_host.py` —
  the extraction targets, not greenfield.

## Goal

Relocate the interaction tools to the Controller as Qt-free command machines,
behind a Controller facade that exposes tool-commands, driven by keys/mouse
through the KeyMap seam that remains in GUI. Default bindings move to YAML.

## Non-goals

- **KeyMap does not move.** No `KeyCode` enum. `on_key`/`Qt.Key`/`_key_matches`
  stay GUI-side (they are input-translation).
- **Renderers stay in the View** (`*_preview.py`, overlays).
- **No behavioural change** to shortcuts, dispatch order, or rebinding.
- **Do not redesign the dispatch mechanism** — reuse `KeyMap.lookup → dispatch`.

## Architecture — the seam after migration

```
GUI:  Qt event → KeyMap.lookup(trigger) → action_id
                    │
                    ▼
Controller facade:  controller.dispatch_tool_command(action_id)   ← View calls this
                    │  (active tool owned here; commands are data)
                    ▼
Tool (Qt-free):     commands[action_id]()   ← executes; knows nothing of keys
```

- The GUI still decodes Qt input and resolves the action-id (KeyMap seam).
- The GUI then asks the **Controller facade** to run that command — it does not
  reach into the tool. The Controller owns which tool is active and routes the
  command.
- On active-tool change, the Controller emits state; the View swaps the KeyMap
  layer (the `controller_state_changed` consumer already designed as step 6b,
  currently deferred — this migration activates it where dispatch flows through
  the KeyMap).
- The Key Bindings panel reads `controller.tool_commands()` + the YAML defaults.

## Migration stages (each: own commit, green suite, parity-before-swap)

| # | Stage | Verify |
|---|---|---|
**Ordering decision (2026-07-13):** the Controller facade comes AFTER the tool
relocation, not before. The facade owns the active tool as an instance; a tool is
only Qt-free after relocation, so building the facade first would force a bridge
protocol. Relocate the tools to Qt-free command machines first, then the facade
aggregates their commands and takes ownership.

| # | Stage | Verify |
|---|---|---|
| 0 | Scaffold `controller/`; add to pyproject | import works; suite green |
| 1 | `gui/ports/` → `controller/ports/`; repoint importers | green; `armed_transform` no longer imports `gui.ports` |
| 2 | `services/editing/` → `controller/editing/`; repoint | green; headless CLI runs |
| 3 | **Split `_Tool`**: Qt-free command base in `controller/tools/base.py` (commands, verbs, lifecycle on `EditState`); View bindings (KeyMap layer build, theme, preview injection, `on_key`→command translation) stay in `gui/` as a mixin | green; both compose to identical behaviour |
| 4 | **Default bindings → YAML** (extend `KEYBINDINGS_V2_FILE`, no new file): add a top-level `defaults:` section alongside the existing `overrides:`; extract each tool's `key_actions()` hardcoded shortcuts into `defaults:`; `load` reads both (override wins); tools stop declaring shortcuts | green; rebinding + persistence + default-propagation unchanged |
| 5 | Move the tools' command logic → `controller/tools/`; each tool exposes `commands() -> dict[str, Callable]`, no `on_key`/`Qt.Key`; GUI mixin translates key→command via KeyMap | **parity tests repointed BEFORE swap** |
| 6 | `select_tool.py` (180 KB) last, alone: strip `on_key`/`Qt.Key`/`QColor` to GUI mixin + preview; land in `controller/tools/` | FSM/selection/delete/group parity green |
| 7 | **Controller facade** (tools now Qt-free): add `tool_commands()` + `dispatch_tool_command(action_id)` to the Controller; move active-tool ownership (`_active_tool`/`set_tool`) + `get_context_at` aggregation into the Controller; add an `active_tool_changed` notify hook the View consumes to swap the KeyMap layer; delete `trigger_tool_action` round-trip | facade tests; `get_context_at` + RMB-menu parity; CLI drives a tool by action-id |
| 8 | Cleanup: layer guard (`controller/` imports no `gui`), docs, CLAUDE.md §5, modules.md redraw | full suite + smoke |

## Bindings file shape (Stage 5)

`KEYBINDINGS_V2_FILE` today is a flat, **override-only** tree
(`category → action.name → shortcut`); `save_overrides` deliberately skips
entries equal to the declared default so default changes propagate. To hold
defaults without breaking that, the file gains two top-level sections:

```yaml
defaults:                 # full default bindings (was hardcoded in key_actions())
  select-tool:
    action.reroute: "W"
    action.rotate: "R"
  global:
    action.delete: "Del"
overrides:                # existing minimal user diffs (unchanged semantics)
  select-tool:
    action.reroute: "Q"   # user rebind; wins over defaults
```

- `load` reads `defaults:` into the declared bindings and `overrides:` into the
  user store; an override wins. A file with no `defaults:` (old file) still
  loads — defaults then come from the built-in fallback, so this is
  backward-compatible.
- `save_overrides` writes only the `overrides:` section, minimised exactly as
  today (skip == declared default). It never rewrites `defaults:`.
- The migration seeds `defaults:` from each tool's current `key_actions()`
  values, then the tools stop declaring shortcuts (they only name their
  action-ids as commands).

## `select_tool.py` — why it is not a rewrite

Only ~34 lines touch Qt today. It is overwhelmingly Qt-free selection/FSM logic
already delegating to `EditController`/`HeadlessSession`. The migration strips its
`on_key`/`Qt.Key.*`/`QColor` (into the GUI mixin + preview), exposes its actions
as `commands()`, and moves the rest — guarded by behaviour parity tests. Highest
risk, so last and alone.

## The `_Tool` split (Stage 4) — the load-bearing seam

Every tool subclasses `_Tool(BoardViewTool)` in `gui/board_view_tool.py`. Only a
few members are View-bound: `make_key_map`/`_theme`/`_key_matches` (KeyMap/Theme)
and the raw `on_key` bodies. Split:

- **Controller (`controller/tools/base.py`):** the command surface
  (`commands()`), the verb surface, lifecycle typed on `EditState`, and the
  Qt-free interaction state. No keys, no Qt.
- **GUI (`gui/board_view_tool.py` mixin):** `make_key_map`, `_theme`, preview
  injection, and the `on_key(KeyInput) → resolve action-id via KeyMap → call the
  tool's command` translation. This is where key-handling LIVES post-migration.

Concrete GUI tools compose `ViewToolMixin + <ControllerTool>` (in
`gui/tools/gui_tools.py`); `tool_host` constructs those. Headless drivers use the
bare Controller tool + `controller.dispatch_tool_command(action_id)` directly —
no keys involved, proving the "CLI test".

## Testing strategy

- Per-stage green suite is the gate.
- **Parity-before-swap:** behaviour tests (selection highlight, context menu,
  FSM, delete/group, shortcut dispatch) repointed to the new path in the SAME
  commit that moves code.
- **Facade tests (Stage 3):** `controller.tool_commands()` returns the active
  tool's action-ids; `dispatch_tool_command(action_id)` runs the command;
  active-tool change emits state.
- **YAML bindings test (Stage 5):** defaults load from YAML; a rebind persists to
  `KEYBINDINGS_V2_FILE`; round-trip unchanged.
- **CLI test (the north star):** a headless script activates a tool via the
  Controller and drives it purely by `dispatch_tool_command(action_id)` — never
  touching a key. If that works, the boundary is correct.
- **Layer guard (Stage 8):** `grep -rn "import.*protoboard\.gui" src/protoboard/controller/`
  returns nothing.
- **Smoke:** `run-protoboard-designer` headless driver.

## Risks

- **Silent regression on `select_tool.py`** — parity-before-swap + smoke; last
  and alone.
- **`get_context_at` / active-tool extraction (Stage 3)** touches the RMB menu
  and `[?]` overlay wiring — parity tests for both before the move.
- **YAML bindings (Stage 5)** must preserve the shared-override store + final-
  trigger rules from `docs/keymap-per-tool-layer.md` — don't regress rebinding.
- **KeyMap-layer swap on active-tool change** was deferred (step 6b) because armed
  keys didn't flow through the KeyMap; this migration makes dispatch flow through
  it, so the swap becomes real — needs its own parity tests.

## Success criteria

1. `controller/{ports,editing,tools}` exist; tools are Qt-free command machines.
2. `grep -rn "import.*gui" src/protoboard/controller/` → nothing.
3. No `on_key`/`Qt.Key`/`QColor` in `controller/tools/`. (`Intent` was
   reclassified as a Qt-free port in `controller/ports/inputs.py` — it is not a
   forbidden symbol; the rule is that a tool does not *decode keys* into intents.)
4. `controller.tool_commands()` + `dispatch_tool_command(action_id)` drive every
   tool action; `trigger_tool_action` deleted.
5. Default bindings in YAML; rebinding + persistence unchanged.
6. A headless CLI drives a tool by action-id with zero keys.
7. KeyMap + renderers remain in `gui/`.
8. Full suite green; smoke passes; docs + CLAUDE.md §5 updated.
