# Archived design documents

This folder holds the **plans** and **diagnostic write-ups** that
produced the work now in `main`. They are kept for historical context —
they no longer describe the live behaviour of the application. For that,
see the files one level up.

| File | Subject | Outcome |
|---|---|---|
| `drag-pipeline-refactor-plan.md` | 7-step plan to rebuild the drag pipeline around `DragDetector`, `DragSet`, `MoveDragSetCommand`. | Implemented. See [`docs/drag-pipeline-architecture.md`](../drag-pipeline-architecture.md). |
| `bug-drag-skewed-wire-shorts.md` | Diagnostic of the "shorts after drag" bug and the design for a unified drag model. | Fixed (commits `c67fa11`, `a3c2f25`). |
| `multi-group-mixed-drag-plan.md` | 8-step plan for first-class multi-group and mixed-drag selection. | Implemented; see the selection FSM rev 3 in [`docs/highlight-state-machine.txt`](../highlight-state-machine.txt). |
| `menu-toolbar-redesign.md` | Audit + redesign of menu / toolbars (`File · Edit · Select · View · Analysis · Help`, declarative `ActionSpec` registry). | Implemented. See [`docs/gui.md`](../gui.md). |
| `pad-state-refactor-questions.md` | Design questions about a unified pad-state structure. | Answered by `PadIndex`, `PadStrategy`, `pad_state.Item` union. |
| `wire-pin-implementation.md` | 7-commit plan for the virtual goldpin (`WirePinKnot`). | Implemented. See [`docs/data-model.md`](../data-model.md) §6.1. |
| `group-export-encapsulation.md` | 6-commit plan for the encapsulated `exported_pins` re-export chain. | Implemented. See [`docs/data-model.md`](../data-model.md) §9 and DRC `GROUP_INVALID_EXPORT`. |

Open follow-up plans that are still relevant (e.g. kapton tape modifier,
hierarchical autoroute) live in the parent folder until shipped, then
move here.
