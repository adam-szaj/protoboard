# Controller-layer migration — `gui/{ports,tools}` + `services/editing` → `controller/`

**Date:** 2026-07-12
**Status:** Design approved, pending spec review
**Author:** Adam Szaj (with Claude Code)

## Problem

Three packages that are conceptually part of the **Controller** layer currently
live in the wrong place:

- `gui/ports/*` — Qt-free Protocols and DTOs (the View↔Controller seam). Already
  Qt-free, simply filed under `gui/`.
- `services/editing/*` — the actual Controller: `EditController`,
  `HeadlessSession`, the headless `cli.py`, `armed_transform.py`, and the
  domain-logic helpers moved out of `gui/` (`group_lock`, `wire_nets`,
  `placement_conflict`, `drag_commit`, `preview_state`, `item_handle`). Already
  Qt-free, but filed under `services/` rather than as its own layer.
- `gui/tools/*` — the interaction tools (Select / Place / Route / Cut / Simplify
  / ArmedTransform). Project memory records these as belonging to the
  Controller, not the View; they are only superficially Qt-bound.

This misplacement produces a concrete layering smell: `services/editing/armed_transform.py`
imports from `gui.ports` (`WheelInput`, `WheelNotchAccumulator`) and also
references `gui.selection` and `gui.tools` under `TYPE_CHECKING` — a
Controller-layer module depending on `gui.*` paths, i.e. the dependency arrow
points the wrong way. Stage 1 (ports) removes the runtime part of this smell;
the `gui.selection` / `gui.tools` references are addressed when those modules
migrate (Stage 4).

CLAUDE.md §3 declares the intended layering:

```
GUI Layer (PySide6)  →  Application/Controller  →  Domain  →  Services
```

There is no first-class `controller/` package to hold that layer. This migration
creates it.

## Goal

A single, coherent, **Qt-free** `controller/` top-level package containing the
ports, the editing controller, and the interaction tools. After the migration:

- `controller/` imports only `domain/`, `services/`, `geometry`, stdlib — never `gui/`.
- `gui/` (Qt adapters + renderers) imports **upward** into `controller/`.
- The `armed_transform` → `gui.ports` smell is gone.
- The tools are genuinely Qt-free (no residual `Qt.Key.*`, `QColor`, or
  `session.signal.emit()` inside tool logic).

## Non-goals

- **Renderers stay in the View.** The `*_preview.py` files
  (`select_preview`, `place_preview`, `route_preview`, `simplify_preview`),
  `drag_conflict_overlay.py`, `drag_corner_text.py`, and `transform_overlays.py`
  are genuinely Qt (QGraphicsItem / QPainter / scene mutation). They are the
  View half of the tool/preview split and are **not** migration targets.
- No behavioural changes. This is a structural move plus a narrow Qt-decoupling
  of the tool classes. User-visible behaviour is invariant.
- No unrelated refactoring of tool internals beyond removing the three Qt
  residues.

## Target structure

```
src/protoboard/
├── controller/                 # NEW — Qt-free layer between GUI and Domain/Services
│   ├── __init__.py             # facade: EditController, HeadlessSession, key ports
│   ├── ports/                  # from gui/ports/ (already Qt-free)
│   │   ├── inputs.py, inputs_details.py
│   │   ├── viewport.py, viewport_details.py
│   │   ├── executor.py, help_overlay.py, hover.py
│   │   ├── interaction_context.py, status_event.py, tool_controller.py
│   │   └── keycodes.py         # NEW — Qt-free key enum, replaces Qt.Key.* in tools
│   ├── editing/                # from services/editing/
│   │   ├── controller.py, headless_session.py, cli.py, armed_transform.py
│   │   ├── group_lock.py, wire_nets.py, placement_conflict.py
│   │   ├── drag_commit.py, preview_state.py, item_handle.py
│   └── tools/                  # from gui/tools/ — LOGIC ONLY
│       ├── select_tool.py, place_tool.py, route_tool.py
│       ├── armed_transform_tool.py, cut_tool.py, simplify_tool.py
│       ├── drag_commit_decision.py, select_prompts.py
└── gui/
    ├── tools/                  # STAYS: renderers (Qt View half)
    │   ├── select_preview.py, place_preview.py, route_preview.py, simplify_preview.py
    │   ├── drag_conflict_overlay.py, drag_corner_text.py, transform_overlays.py
    ├── qt_viewport.py, qt_inputs.py, qt_executor.py   # adapters → import UP into controller/
    └── qt_keymap.py            # NEW/extended — maps Qt.Key.* → controller keycode enum at the boundary
```

**Dependency direction (invariant to enforce):**
`gui/` (adapters + renderers) → `controller/` → `domain/` / `services/`. Never
the reverse. The same mechanical rule that protects `domain/` now protects
`controller/`.

## Approach: staged migration, green suite per stage

Chosen over a big-bang `git mv` + bulk import-rewrite because the migration
touches load-bearing modules and the 180 KB `select_tool.py`. Staging keeps
every commit bisectable and reversible, and honours CLAUDE.md §8 ("no silent
regressions"): parity tests are repointed to the new location **before** the old
code is deleted, never in the same breath.

### Stages

Each stage is one commit with a green test suite.

| # | Stage | Verify |
|---|---|---|
| 0 | Scaffold `controller/` package; add to `pyproject` packages/mypy_path | import works; suite green |
| 1 | `gui/ports/` → `controller/ports/`; repoint ~20 importer files; Qt adapters stay in `gui/` | suite green; `armed_transform` no longer imports `gui.ports` at runtime |
| 2 | Add `controller/ports/keycodes.py` (Qt-free key enum) + `gui/qt_keymap` mapping `Qt.Key.*` → enum | keycode-mapping test |
| 3 | `services/editing/` → `controller/editing/`; repoint importers | suite green; headless CLI runs |
| 4a | `cut_tool`, `drag_commit_decision`, `select_prompts` → `controller/tools/` (already Qt-free) | suite green |
| 4b | `place/route/armed/simplify_tool` → `controller/tools/`; QColor → preview, `emit()` → `notify_changed()`, keycodes → enum | **parity tests repointed BEFORE swap** |
| 4c | `select_tool.py` (180 KB) → `controller/tools/`; remove last Qt residues | behaviour parity tests (FSM, selection, delete/group) green |
| 5 | Cleanup: remove empty `gui/ports/`, `services/editing/`; update `docs/` + CLAUDE.md §5 | full suite + smoke (`run-protoboard-designer`) |

## The three Qt residues in the tools (Stage 2 / 4b / 4c)

Removing these is what makes `controller/` genuinely Qt-free:

1. **`Qt.Key.*` keycode constants** — used in key-dispatch comparisons. The
   `ports` layer already provides Qt-free equivalents for pointer buttons and
   modifiers; keys are the one gap. Fix: a Qt-free `KeyCode` enum in
   `controller/ports/keycodes.py`, with the Qt→enum mapping performed in
   `gui/qt_keymap.py` at the boundary (exactly where `qt_inputs.py` already maps
   pointer/mods).
2. **`QColor` tints** — a handful of overlay colour values. These belong to the
   preview renderer, not the tool. Fix: pass them from the `Qt*Preview`
   implementation, or express them as Qt-free colour DTOs the preview resolves.
3. **`session.signal.emit()`** — several sites in `select_tool`, 1–2 elsewhere
   (the file carries ~34 Qt-token lines total across all three residues).
   `HeadlessSession` already exposes a Qt-free `notify_changed()` hook that
   `EditorSession` (the `QObject` subclass) overrides to emit `layout_changed`.
   Fix: route tool notifications through `notify_changed()` instead of `.emit()`.

## `select_tool.py` (180 KB) — why it is not a rewrite

Despite its size, only ~34 lines touch Qt (grep of `PySide6|QColor|Qt.Key|.emit(|QGraphicsItem`). It is overwhelmingly domain/selection
logic (FSM state, hit-testing, selection algebra, net queries, drag orchestration,
delete/group macros) already delegating to `HeadlessSession` / `EditController`.
It already has a headless seam: `activate(session, view: AbstractBoardView)` takes
the Protocol view. The migration is move + repoint + the three residue fixes,
guarded by behaviour parity tests — not a rewrite.

## Testing strategy

- **Per-stage green suite** is the primary gate; no stage proceeds on red.
- **Parity tests survive the swap.** For the tool moves (4b/4c), behaviour tests
  (what the user sees — selection highlight, context menu, FSM transitions,
  delete/group) are repointed to `controller/tools/` before the old modules are
  deleted. Tests of private internals may be deleted with their code; behaviour
  tests may not.
- **Keycode mapping test** (Stage 2): assert `gui/qt_keymap` maps representative
  `Qt.Key.*` values to the correct `controller.ports.keycodes.KeyCode` members.
- **Smoke test** (Stage 5): `run-protoboard-designer` headless driver — launch,
  select, zoom, camera, rebuild — must pass.
- **Layering guard:** after Stage 5, `grep -rn "import.*\bgui\b" src/protoboard/controller/`
  must return nothing (controller must not import gui).

## Risks

- **Silent regression on `select_tool.py`** — mitigated by parity-before-swap and
  the smoke test. This is the highest-risk stage; do it last and alone (4c).
- **Import churn** — ~20 `gui.ports` importer files plus `services.editing` importers.
  Mechanical, but wide. Repoint per stage, lean on the suite.
- **`inputs_details.py` / `viewport_details.py`** are concrete logic classes
  (not port interfaces) but equally Qt-free; placement in `controller/ports/` is
  a naming choice, not a coupling problem.
- **Scope creep on the QColor/emit cleanup** — bounded to the three named residues;
  no other tool-internal refactoring.

## Success criteria

1. `controller/` exists; `ports/`, `editing/`, `tools/` (logic) live under it.
2. `grep -rn "import.*gui" src/protoboard/controller/` returns nothing.
3. No `Qt.Key.*`, `QColor`, or `.emit()` in `controller/tools/`.
4. Renderers (`*_preview.py`, overlays) remain in `gui/`.
5. Full pytest suite green; `run-protoboard-designer` smoke passes.
6. `docs/` and CLAUDE.md §5 source-layout updated to match.
