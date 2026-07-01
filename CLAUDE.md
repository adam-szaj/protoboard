# CLAUDE.md — Perfboard / Stripboard Layout Designer

## 0. Project goal

A Linux desktop application (PySide6) for designing the physical placement of
components on prototyping boards — perfboards, stripboards and similar matrix
boards.

**What makes it different:** the project is *connectivity-aware*. The data
model is a graph of logical nets (the netlist); the physical view is the
projection of that graph onto a grid of holes. This makes DRC,
KiCAD import/export and intelligent routing tractable.

**Working name:** `protoboard-designer`.

## 1. Technology stack

| Layer | Choice |
|---|---|
| Language | Python 3.11+ |
| GUI | PySide6 (Qt 6), `QGraphicsScene` / `QGraphicsView` |
| Graphs | `networkx` + a custom union-find (`domain/connectivity/incremental.py`) |
| Routing | Custom A* (Python + optional Rust PyO3 implementation) |
| Persistence | YAML (`ruamel.yaml`) |
| KiCAD I/O | Custom S-expression parser (MIT) + optional `kiutils` (GPL) |
| Tests | `pytest` + `pytest-qt` |
| Linters | `ruff` + `black` + `mypy --strict` |
| Packaging | `uv` + `pyproject.toml` (PEP 621) |

## 2. License

- Core code, KiCAD parser, GUI, data models: **MIT**.
- Optional `kicad_integration_gpl` module: **GPLv3**
  (`pip install protoboard-designer[kicad-gpl]`).
- SPDX header on every file: `# SPDX-License-Identifier: MIT`.

## 3. Architecture — layers

```
GUI Layer (PySide6)
  ↑ Qt signals
Application Layer (CommandStack, Tools, Selection)
  ↑ Commands
Domain Model (pure Python, no Qt)
  ↑
Services (DRC, Router, KiCAD I/O, Persistence, Export)
```

**Rule:** the Domain Model must not import Qt. It must be testable without a
display server.

## 4. Detailed documentation

| Topic | File |
|---|---|
| Data model (box model, pads, schematic, layout, groups, wire/knot) | [docs/data-model.md](docs/data-model.md) |
| Defining components & packages (library YAML authoring guide) | [docs/footprints/authoring.md](docs/footprints/authoring.md) |
| Package model (bodies, leads, schema, kind; design rationale) | [docs/footprints/package-model.md](docs/footprints/package-model.md) |
| Geometry (placement, rotation, flip, bounding box) | [docs/geometry.md](docs/geometry.md) |
| Routing (A*, cost model, autorouter) | [docs/routing.md](docs/routing.md) |
| Undo / redo, command inventory | [docs/commands.md](docs/commands.md) |
| Persistence, KiCAD I/O | [docs/persistence.md](docs/persistence.md) |
| DRC | [docs/drc.md](docs/drc.md) |
| GUI, tools, multi-selection, wire editing | [docs/gui.md](docs/gui.md) |
| Selection / highlight FSM (canonical) | [docs/highlight-state-machine.txt](docs/highlight-state-machine.txt) |
| Pad / pin / wire / joint semantics (hard and soft rules) | [docs/pad-pin-wire-junction-semantics.md](docs/pad-pin-wire-junction-semantics.md) |
| Drag pipeline architecture | [docs/drag-pipeline-architecture.md](docs/drag-pipeline-architecture.md) |
| Armed-rotate mode + sub-tool delegation (design) | [docs/armed-rotate-and-subtool-delegation.md](docs/armed-rotate-and-subtool-delegation.md) |
| Bendable leads for N-pin parts + per-pin re-drag (design) | [docs/bendable-leads-redrag.md](docs/bendable-leads-redrag.md) |
| Testing strategy | [docs/testing.md](docs/testing.md) |
| Benchmarks, profiling, GUI performance architecture | [docs/benchmarks.md](docs/benchmarks.md) |
| Historical performance optimisations | [docs/performance.md](docs/performance.md) |
| Incremental connectivity (union-find) | [docs/connectivity.md](docs/connectivity.md) |
| Command recording for headless replay | [docs/command-recorder.md](docs/command-recorder.md) |
| Implementation phases (historical) | [docs/phases.md](docs/phases.md) |
| Open follow-ups not yet scheduled | [docs/todo.md](docs/todo.md) |

Archived design plans whose work has shipped live in [docs/archive/](docs/archive/).

## 5. Source layout

```
src/protoboard/
├── units.py, geometry.py
├── domain/
│   ├── board/          # boxes.py, layout_engine.py, pads.py, board.py, pad_state.py, pad_geometry.py, layer.py, templates.py
│   ├── components/     # footprints.py, definition.py, instance.py
│   ├── schematic/      # net.py, schematic.py
│   ├── layout/         # placement.py, wire.py, wire_utils.py, knot.py, cut.py,
│   │                   # group.py, layout.py, normalize.py, pad_conflict.py,
│   │                   # drag_classification.py, group_ownership.py, pin_pad_classification.py
│   ├── connectivity/   # incremental.py (graph.py is a re-export shim)
│   ├── routing_settings.py
│   └── commands/       # base.py, board_cmds.py, component_cmds.py,
│                       # drag_cmds.py, group_cmds.py, net_cmds.py
├── services/
│   ├── drc/            # engine.py, checks.py
│   ├── routing/        # cost_model.py, astar.py, autorouter.py, hierarchical.py,
│   │                   # cost_analyzer.py, astar_rs/ (Rust PyO3 backend)
│   ├── persistence/    # board_yaml.py, project_yaml.py, wire_migration.py, git_repo.py
│   ├── export/         # snapshot.py, paint.py, svg.py, pdf.py, pdf_template.py, png.py, bom.py
│   ├── library/        # loader.py
│   ├── recording/      # recorder.py, replay.py, serialization.py, headless.py
│   ├── kicad/          # parser.py, importer.py, exporter.py
│   ├── placement/      # spreader, etc.
│   ├── drag_detector.py
│   └── pad_strategy.py
├── gui/
│   ├── main_window.py, session.py, board_view.py, selection_fsm.py,
│   │                   # drag_set.py, keybindings.py, …
│   ├── selection/      # SelectTool-as-a-service: selectable.py (protocol+adapters),
│   │                   # service.py (SelectionService + pick-API), hit_test.py,
│   │                   # queries.py (net relation), wire_edit.py, wire_ops.py,
│   │                   # mouse_input.py, point_pick_controller.py,
│   │                   # drag_controller.py, rubber_band_controller.py, adapters.py
│   ├── highlight/      # controller.py (HighlightController service)
│   ├── renderers/      # board_renderer.py, component_renderer.py, wire_renderer.py
│   ├── dialogs/        # preferences_dialog.py, group_target_dialog.py,
│   │                   # pad_conflict_dialog.py, drag_disconnect_dialog.py,
│   │                   # drag_ask_remove_dialog.py, wire_conflict_dialog.py,
│   │                   # net_merge_dialog.py, autoroute_dialog.py,
│   │                   # cost_presets_dialog.py, resize_board_dialog.py,
│   │                   # toolbar_customize_dialog.py, welcome_dialog.py,
│   │                   # new_project_dialog.py
│   ├── panels/         # library_panel.py, properties_panel.py, groups_panel.py,
│   │                   # netlist_panel.py, nets_editor_panel.py,
│   │                   # net_inspector_panel.py, components_panel.py,
│   │                   # layers_panel.py, performance_panel.py,
│   │                   # selection_filter_widget.py, highlight_mode_widget.py,
│   │                   # tree_panel_mixin.py
│   ├── tools/          # select_tool.py, place_tool.py, route_tool.py,
│   │                   # cut_tool.py, simplify_tool.py,
│   │                   # drag_conflict_overlay.py, drag_corner_text.py,
│   │                   # drag_commit_decision.py
│   └── workers/        # background QThread workers for autoroute, DRC, simplify
└── app.py
```

## 6. Coding conventions

- `mypy --strict` must pass. No `Any` outside documented escape hatches.
- `frozen=True` dataclasses wherever practical. `@property` for derived values.
- Pure functions in `domain/`; side effects live in `services/persistence`,
  `services/recording`, `services/export`, and `gui/`.
- Naming: `snake_case` functions, `PascalCase` classes, `SCREAMING_SNAKE` constants.
- Google-style docstrings on public modules and classes.
- Custom exceptions inherit from `ProtoboardError` (`protoboard.errors`).
- Logging: `structlog`; default level INFO, `--verbose` enables DEBUG.
- Commits: Conventional Commits (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`).

## 7. Open design questions

1. **Pin numbering in rotated components**: `FootprintTemplate.pin_position()`
   returns the local-frame position; the absolute world position is computed
   by `pin_world_position()` in `domain/layout/placement.py`.
2. **Components overhanging the board**: warn or hard-block? (Currently
   tolerated — see DRC `OFF_BOARD_PIN`.)
3. **Wire insulation rendering**: insulated = solid line; bare = grey line with
   black end caps.
4. **Stripboard cuts**: rendered as a gap between pads (kept; clearer than an
   "X" overlay).
5. **KiCAD `.net` format**: the importer currently targets the new XML
   `<export version="D">` format; S-expression variants depend on the KiCAD
   release.

## 8. Recommendations for Claude Code

- Work in phases. Don't start phase N+1 until phase N has a green CI and a
  smoke test.
- Each phase: update `CHANGELOG.md` at the start, commit `chore: complete phase N`
  at the end.
- TDD for the layout engine and routing: write the failing test first.
- If you hit a design decision not covered by this document — **stop and ask**.
- Update this file and the files under `docs/` whenever a decision is taken.
- Every new public API gets a test and a docstring before the commit.

### Rewrites and swaps (no silent regressions)

Hard-won lesson from the SelectTool v1→v2 swap, where dozens of polished,
user-facing behaviours (bright selection highlight, the unified context menu,
the reroute preview anchor) silently vanished or degraded because they lived
*inside* the monolith rather than behind a stable interface, and the swap
deleted their tests along with the old code. The principle you should have been
able to rely on — "a new piece of work must not change finished functionality
except by fixing how it's *called*" — only holds if finished functionality has
a seam and a guard. Enforce both:

- **Parity tests survive the swap; they do not die with the old code.** Before
  deleting an old module, repoint its *behaviour* tests (what the user sees —
  not tests of private internals) at the replacement. If the replacement does
  not pass them, it is **not** ready to swap in. Delete old tests only after
  their behaviour is re-asserted against the new code, never in the same breath
  as the implementation they guarded.
- **"Deferred to Phase 2" is tracked debt, not a code comment.** Any behaviour
  deliberately dropped during a swap goes into `docs/todo.md` with a date and
  the commit that dropped it — never as an inline comment that no one reads. A
  swap that defers user-visible parity is incomplete by definition.
- **Pull polished functionality out of the monolith *before* swapping it.** If a
  behaviour is worth perfecting, it is worth extracting into a service with a
  named interface first, so the swap only re-wires call sites and cannot drag
  the behaviour down with it. Prefer incremental refactor over from-scratch
  rewrite precisely because a rewrite re-implements (and therefore can lose)
  every undocumented refinement.
