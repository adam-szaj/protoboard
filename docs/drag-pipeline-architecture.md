# Drag pipeline architecture

This document describes the drag-and-drop pipeline as it stands after the
unified-drag refactor (2026-06-02). For the historical "what went wrong before"
narrative see [archive/bug-drag-skewed-wire-shorts.md](archive/bug-drag-skewed-wire-shorts.md)
and [archive/drag-pipeline-refactor-plan.md](archive/drag-pipeline-refactor-plan.md).

## Actors

- **`SelectTool`** — owns the drag UX state (origin placements, snapped
  position, floating mode, overlay items). Listens to Qt events: press /
  move / release / Esc / Enter / double-click.
- **`DragSet`** (`gui/drag_set.py`) — a frozen dataclass with
  `free_components`, `groups` and `wires`. Captured at press time on every
  drag-arm path (pin, wire, bbox, group bbox) and accessed thereafter via
  `_active_drag_set`. `DragSet.all_component_ids(groups_map)` flattens the
  structure into a single set of component ids for the commit-time command.
- **`DragDetector`** (`services/drag_detector.py`) — pure-logic classifier.
  Inputs: `DragSnapshot` + `DragSet`. Output: `DragCommitDecision`. Consumes
  the injected `PadStrategy` for every per-pad question instead of inlining
  rule 5/6/8 logic.
- **`DragSnapshot`** — immutable view of "what the layout looks like right
  now" for conflict detection. Contains `pad_index` (already current via
  incremental mutation during the drag), `placements`, `schematic`,
  `instances`, `components`.
- **`PadStrategy`** (`services/pad_strategy.py`) — pluggable rule set.
  `DRCEnforcingStrategy` is the default during a drag (rule 6 strict — no
  wire over any pin pad; rule 8 — the `can_place_pin` net gate).
- **`PadIndex`** (`domain/board/pad_state.py`) — single source of truth for
  pad occupancy. **Mutated incrementally** during a drag via
  `remove_component(cid)` + `add_component(cid, new_placement)` rather than
  rebuilt per snap.
- **`CommandStack`** — executes the commit-time `MacroCommand`.
- **`DragConflictOverlay`** (`gui/tools/drag_conflict_overlay.py`) —
  renders the `DragCommitDecision` as Qt scene items (red / yellow rects,
  crosses). Pure rendering, no business logic.
- **`PreferencesDialog ▸ Drag`** — surfaces the soft-conflict preferences
  (`free_wire_conflict`, `stationary_group_wire_conflict`,
  `cross_group_disconnect`) and the default `lock_internal_wires` flag.

## Sequence (single component, single group, or multi/mixed)

```
on_press
  ├─ capture origin placements / wire paths
  ├─ self._active_drag_set = DragSet.from_selection(...)
  ├─ self._drag_pad_index = session.pad_index().clone()
  └─ self._drag_detector = DragDetector(session.strategy_for("drc"))

on_move (~60 fps)
  ├─ snap cursor → new position
  ├─ skip when the snap didn't change
  ├─ in-place mutate self._session.layout.placements[cid] for every dragged component
  ├─ self._drag_pad_index.remove_component(cid) / add_component(cid, new_pl, …)
  ├─ snapshot = DragSnapshot(pad_index=self._drag_pad_index, …)
  ├─ decision = self._drag_detector.classify(snapshot, drag_set, dragged_wires)
  ├─ self._drag_overlay.update(decision) → red/yellow rectangles + crosses
  └─ self._corner_text.show(decision.description)

on_release
  ├─ decision = last on_move's classify (cheap to recompute)
  ├─ if decision.is_clear and auto_commit_on_clear:
  │     synthesise Enter and commit
  ├─ if decision.is_soft (free wire / stationary-group wire / cross-group):
  │     enter floating UX; consult preference → dialog or silent action
  └─ if decision.is_hard:
        enter floating UX; commit is blocked until user moves or cancels

on_commit (Enter or LMB double-click)
  ├─ cmds = [
  │     RemoveWireCommand(wid) for wid in decision.auto_remove_wire_ids,
  │     MoveDragSetCommand(
  │         placements = {cid: (old_pl, new_pl)},
  │         internal_wire_ids = drag_internal_ids,
  │         external_wires_to_drop = decision.cross_group_disconnect_drop_ids,
  │     ),
  │   ]
  ├─ stack.execute(MacroCommand(cmds))
  ├─ optionally schedule autoroute for cross-group reroutes
  ├─ self._drag_pad_index = None
  └─ session.layout_changed.emit()

on_cancel (Esc)
  ├─ revert placements + wire paths in place
  ├─ self._drag_pad_index = None
  └─ session.layout_changed.emit()
```

## State changes — what mutates when

| State | Live drag (on_move) | Commit | Cancel (Esc) |
|---|---|---|---|
| `layout.placements[cid].position` | in-place (live preview) | finalised by `MoveDragSetCommand` | reverted |
| `layout.wires[*].fragments` | in-place for internal wires | finalised by `MoveDragSetCommand` | reverted |
| `self._drag_pad_index` | incremental remove / add | discarded | discarded |
| `session.pad_index()` (cached) | **not touched** — stale until commit | invalidated by `layout_changed` | invalidated |
| `schematic.nets` | **not touched** | mutated by `decision.auto_net_actions` | not touched |

Key rule: the live drag **never** mutates the schematic, **never**
creates or removes wires from `session.layout.wires` directly. The
"wire follows the pin" effect is realised through in-place path mutation
because we have to do that anyway to draw the live preview. The
`DragCommitDecision` only *declares* "after commit, remove these wires
and execute these net actions".

## Drag classification (`drag_classification.py`)

The 5-way partition of wires the detector produces:

- **internal** — both endpoints are `PinKnot`s of components in the drag
  set. The wire travels with the drag.
- **half-orphan internal** — one endpoint is a pin in the drag set, the
  other is a free `JointKnot`/`WirePinKnot` in the drag set's interior.
- **half-orphan external** — one endpoint in the drag set, the other a
  free knot outside.
- **cross-group disconnect** — both endpoints are pins, but one belongs to
  a component outside the drag set. Commit will sever the connection
  unless rerouted.
- **external** — does not touch the drag set; untouched.

## Where `PadStrategy` is injected

`DragDetector.__init__(strategy: PadStrategy)` receives the strategy
**once** at drag start. Inside `classify()` the strategy answers every
per-pad question:

- `strategy.can_place_pin(pad, our_net)` — rule 6 + rule 8 combined
  (foreign wire on a pin, foreign-net pin-on-pin).
- `strategy.can_form_t_junction(pad, …)` — when a dragged wire is about
  to form a T-junction (rare during drags; common during routing).
- `strategy.can_solder_wire_to_pad(…)` — for dragged-wire endpoints that
  land on pads after the move.

`SelectTool` never inspects pad internals directly. All "what is on pad X
after moving component Y" knowledge flows through `DragDetector → strategy
→ pad_index`.

## Soft-conflict resolution (preference-driven)

Three categories of soft conflict, each backed by a preference field in
`app_settings.yaml`. Each preference accepts:
`auto-remove` / `ask-remove` / `auto-reroute` / `ask-reroute` / `block`.

| Category | Default | Trigger |
|---|---|---|
| `drag.free_wire_conflict` | `auto-remove` | A moved pin or moved wire collides with a free wire (not owned by any group). |
| `drag.stationary_group_wire_conflict` | `ask-remove` | Same, but the colliding wire is owned by a stationary group. |
| `drag.cross_group_disconnect` | `ask-reroute` | A wire links a moved element to a stationary one — committing drops the connection unless we autoroute. |

A stationary group with `lock_internal_wires=True` promotes any
collision with its owned wires to **HARD** regardless of the preference —
the group becomes immutable from the outside.

Dialogs (`drag_disconnect_dialog.py`, `drag_ask_remove_dialog.py`,
`wire_conflict_dialog.py`) each carry a "Don't ask again for this
session" checkbox so power users can opt out for the rest of the run
without changing the persistent preference.
