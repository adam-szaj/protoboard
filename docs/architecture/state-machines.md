<!-- SPDX-License-Identifier: MIT -->
# State machines (tools / FSM)

Three state machines reconstructed from the code, with guards quoted from the
source. Renders on GitHub (Mermaid `stateDiagram-v2`).

- **[1] Tool lifecycle** — which tool is active, how the app switches between
  them (`gui/tool_host.py`, `gui/tools/*`).
- **[2] Armed-transform** — the armed rotate/move/flip sub-machine
  (`services/editing/armed_transform.py` + `gui/tools/armed_transform_tool.py`).
- **[3] Selection / highlight** — the 4-layer selection stack + highlight
  (`gui/selection_fsm.py`, canonical spec `docs/highlight-state-machine.txt`).

---

## [1] Tool lifecycle / tool-switching

`ToolHost.set_tool()` deactivates the old tool, pops its KeyMap layer, activates
the new one, pushes its KeyMap layer, and fires `view.refresh()`. Escape is
two-stage: the active tool gets `on_key(Esc)` first (internal cancel), then
`escape_to_select()` always runs → `_tool_select` in `MainWindow`.

`ArmedTransformTool` is the first **tool-self-transition**: SelectTool on
`[R]`/`[G]` does `set_tool(ArmedTransformTool(..., return_to=...))`, and the
armed tool calls the injected `return_to` on accept/cancel to hand control back.

Guard: while `session.autoroute_active`, `set_tool` forces any non-SelectTool to
`None` (`tool_host.py`).

```mermaid
stateDiagram-v2
    [*] --> Select

    state "Select (select.idle)" as Select
    state "Route (idle)" as Route
    state "Cut (idle)" as Cut
    state "Simplify (select.idle)" as Simplify
    state "ArmedTransform (move.float.rotate|move)" as Armed

    Select --> Route: W
    Select --> Cut: C
    Select --> Simplify: Shift+S
    Select --> Place: pick from library

    Route --> Select: S / Esc
    Cut --> Select: S / Esc
    Simplify --> Select: S / Esc
    Place --> Select: S / Esc

    Select --> Armed: R arm rotate (selection non-empty, rotatable)
    Select --> Armed: G arm move
    Armed --> Armed: accept HARD conflict (parked, finished=False)
    Armed --> Select: accept committable / cancel / Esc (return_to)

    state Place {
        [*] --> ANCHOR
        ANCHOR --> DRAG_PIN: press pin
        DRAG_PIN --> FLOATING: reach committed
        DRAG_PIN --> ANCHOR: Esc
        FLOATING --> ANCHOR: Enter commit / Esc cancel
    }

    note right of Select
        Any tool: Esc runs escape_to_select
        after the tool's own on_key(Esc).
        Guard: autoroute_active forces
        Place/Route/Cut to None.
    end note
```

---

## [2] Armed-transform (E5 preview-model)

`state` = `move.float.{mode}` while `armed and not armed.finished`, else
`select.idle`.

The whole gesture mutates a **preview** layout (`live.preview_copy()`); the live
layout stays pristine until accept commits one `TransformDragSetCommand`.

Guards, quoted from `services/editing/armed_transform.py`:

- **switch-mode keeps the pose** (`_enter_mode`): changing `rotate`↔`move` only
  re-aims the arrow keys — no transform step, no commit.
- **accept → conflict-park**: `if decision is not None and not
  decision.can_commit:` → status message, `_emit_preview()`, `return` — leaves
  `finished == False`, so the tool stays armed (conditional disarm). Conflict is
  classified against the **preview** pose via `PreviewState`.
- **cancel**: `finished = True`, nothing to undo (live never mutated), preview
  dropped.

```mermaid
stateDiagram-v2
    [*] --> idle

    state "armed(rotate) — move.float.rotate" as armed_rotate
    state "armed(move) — move.float.move" as armed_move
    state "parked (armed, finished=False)" as parked

    idle --> armed_rotate: begin_transform(mode=rotate)
    idle --> armed_move: begin_transform(mode=move)

    armed_rotate --> armed_rotate: wheel / Up / R = +90, Down = -90 (mutate preview)
    armed_rotate --> armed_rotate: click pad = set pivot (toggle)
    armed_move --> armed_move: arrows / WASD = move_armed(dx,dy)
    armed_move --> armed_move: click pad = set pivot

    armed_rotate --> armed_move: G switch_transform_mode (keeps pose, no commit)
    armed_move --> armed_rotate: R switch_transform_mode (keeps pose)

    armed_rotate --> armed_rotate: F / Alt+R flip_armed (2-pin gate)
    armed_move --> armed_move: F / Alt+R flip_armed (2-pin gate)

    armed_rotate --> committed: Enter accept [can_commit AND is_transformed]
    armed_move --> committed: Enter accept [can_commit AND is_transformed]

    armed_rotate --> idle: accept but not is_transformed (no change)
    armed_move --> idle: accept but not is_transformed

    armed_rotate --> parked: accept [decision not None AND not can_commit = HARD conflict]
    armed_move --> parked: accept [HARD conflict]

    parked --> committed: step to a clear spot + Enter
    parked --> cancelled: Esc

    armed_rotate --> cancelled: Esc / cancel (drop preview)
    armed_move --> cancelled: Esc / cancel
    armed_rotate --> cancelled: deactivate() while not finished
    armed_move --> cancelled: deactivate() while not finished

    committed --> [*]: return_to → SelectTool
    cancelled --> [*]: return_to → SelectTool
```

---

## [3] Selection / highlight

Canonical source: `docs/highlight-state-machine.txt` (spec rev 3). Four stack
layers (`gui/selection_fsm.py::StackLayer`): `NORMAL` / `HIGHLIGHT` /
`SELECTION` / `MULTI_SELECTION`, plus a modal `wire-edit` state entered with
`[E]` on a single-wire selection.

Set-op axis (`SelectTool._decode_modifiers`, rev 3): **SET** (plain LMB),
**ADD** (Ctrl), **REMOVE** (Ctrl+Shift), **RELATION** (Alt). Highlight is a
separate visual tier (auto-dimming) from the bright selection rectangles: SET
replaces it, ADD unions into it, REMOVE leaves it unchanged (asymmetric — the
only "clear all" is Esc).

```mermaid
stateDiagram-v2
    [*] --> NORMAL

    state "NORMAL (nothing selected)" as NORMAL
    state "HIGHLIGHT (relation overlay, no primary)" as HIGHLIGHT
    state "SELECTION (primary + relation)" as SELECTION
    state "MULTI_SELECTION (primary + multi set)" as MULTI
    state "wire-edit (modal)" as WIREEDIT

    NORMAL --> SELECTION: LMB hit (SET)
    HIGHLIGHT --> SELECTION: LMB hit (SET)

    SELECTION --> SELECTION: LMB hit != primary (switch_primary)
    SELECTION --> HIGHLIGHT: LMB hit == primary (pop_to_highlight)
    SELECTION --> MULTI: Ctrl+LMB new hit (ADD, basis not subset)

    MULTI --> MULTI: Ctrl+LMB new hit (ADD)
    MULTI --> SELECTION: LMB hit != primary (collapse_to_selection)
    MULTI --> HIGHLIGHT: LMB hit == primary (collapse_to_highlight)

    MULTI --> HIGHLIGHT: Ctrl+Shift REMOVE (selection becomes empty)
    MULTI --> SELECTION: Ctrl+Shift REMOVE (one remains, primary=None)
    MULTI --> MULTI: Ctrl+Shift REMOVE (more than one remains)

    SELECTION --> WIREEDIT: E on single-wire selection
    WIREEDIT --> SELECTION: Esc / Enter
    WIREEDIT --> WIREEDIT: LMB add point

    MULTI --> SELECTION: Esc / empty-canvas LMB (clear multi)
    SELECTION --> HIGHLIGHT: Esc / empty-canvas LMB (primary=None)
    HIGHLIGHT --> NORMAL: Esc / empty-canvas LMB
    NORMAL --> NORMAL: Esc (no-op resync)

    note right of HIGHLIGHT
        Highlight (auto-tier dimming) is a
        separate visual layer from the bright
        selection rects.
        SET: highlight := relation(hit)
        ADD: highlight := highlight ∪ relation(hit)
        REMOVE: highlight unchanged (asymmetric)
    end note
```

> **Code ↔ spec note.** `gui/selection_fsm.py` still exposes `step_shift_lmb`
> (older Shift = multi semantics) alongside the newer `BasisScope`/`SetOp` axes.
> `SelectTool._decode_modifiers` implements the canonical rev-3 axis
> (Ctrl = ADD, Ctrl+Shift = REMOVE, Alt = RELATION). The diagram follows the
> canonical spec; the legacy `step_shift_lmb` path is a remnant of an earlier
> revision worth reconciling.
