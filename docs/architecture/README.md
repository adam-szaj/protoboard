<!-- SPDX-License-Identifier: MIT -->
# Architecture overview

High-level, diagram-first view of the system. Every diagram is Mermaid (renders
natively on GitHub) and reconstructed from the **actual code**, not the declared
design — deviations from the declared architecture are flagged inline.

| Diagram | File | What it shows |
|---|---|---|
| **Module dependencies & layers** | [modules.md](modules.md) | Real import edges between GUI / Controller / Domain / Services; the `EditController`/`EditState` seam; **known layer violations** (services→gui, transitive Qt leak into domain). |
| **State machines** | [state-machines.md](state-machines.md) | Tool lifecycle (Select↔Armed↔Place↔Route↔Cut), the armed-transform sub-machine (arm→step→accept / conflict-park / cancel), and the selection/highlight stack FSM. |
| **Sequence flows** | [sequences.md](sequences.md) | Call-level sequences for armed rotate (E5 preview-model), drag-commit, and background autoroute — with the Qt-free boundary marked. |
| **Domain data model** | [data-model.md](data-model.md) | Entities and relations: Schematic (logical netlist) → Layout (physical) → Board, plus the connectivity projection. |

## Orientation

- **Layers** (CLAUDE.md §3): GUI (PySide6) → Application/Controller
  (`EditController`, Tools, Selection) → Domain (pure Python) → Services (DRC,
  Router, KiCAD I/O, Persistence, Export).
- **The seam**: `EditController` (typed by the `EditState` protocol) is the one
  place GUI tools and the headless CLI meet — both drive the same verbs.
  `EditorSession(HeadlessSession, QObject)` is the headless session plus Qt.
- **Qt-free rule**: the domain must be testable without a display server. See
  [modules.md](modules.md) for where the runtime import graph currently bends
  that rule.

## Related canonical docs

- [../gui.md](../gui.md) — GUI, tools, multi-selection, wire editing
- [../rendering.md](../rendering.md) — draw chain, Z-layers, LayerState
- [../highlight-state-machine.txt](../highlight-state-machine.txt) — canonical
  selection/highlight FSM spec (source for [state-machines.md](state-machines.md) §3)
- [../data-model.md](../data-model.md) — prose data-model reference
- [../commands.md](../commands.md) — undo/redo, command inventory
- [../drag-pipeline-architecture.md](../drag-pipeline-architecture.md) — drag pipeline

> These diagrams are a snapshot (2026-07). If one disagrees with the code, trust
> the code and update the diagram.
