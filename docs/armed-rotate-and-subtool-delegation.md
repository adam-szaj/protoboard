# Armed-rotate mode + sub-tool delegation (design)

Status: **DESIGN ONLY — not implemented.**  Captures the agreed shape of the
new rotate interaction and the larger direction it belongs to (SelectTool
sheds operations to delegated sub-tools).  Implementation is staged; this doc
is the map.

Related: [docs/gui.md](gui.md), [docs/commands.md](commands.md),
[docs/drag-pipeline-architecture.md](drag-pipeline-architecture.md),
[docs/highlight-state-machine.txt](highlight-state-machine.txt).

---

## 1. The rotate interaction (agreed)

Rotation becomes an **armed mode**, modelled on wire-edit (`[E]`):

1. Select an object — a component, a set of components, a group, or a mix of
   components and groups.
2. Press **`[R]`** → arms *float-rotate*.  The selection enters a **floating**
   (preview) state; nothing is committed yet.
3. Rotate the floating selection by **±90°** via several bindings at once:
   - mouse **wheel** (one tick = ±90°),
   - **`[Up]` / `[Down]`** arrows,
   - the existing rotate-CW / rotate-CCW actions.
4. **Pivot model:**
   - *Default:* every selected object rotates about **its own** local pivot
     (local origin `(0,0)` / placement position) — `RotateComponentsCommand`
     per object, group about its origin.
   - *Click a pad:* that pad becomes the **shared pivot** for the **entire
     selection** — `RotateAroundPointCommand(origin=pad_world_pos)`.
   - *Click the same pad again, or partial `[Esc]`:* reset to per-object.
   - The pivot is shown with a marker (reuse the wire-edit overlay idiom).
5. **`[Enter]`** commits; **`[Esc]`** cancels (reverts the float).

Step is **90° only** (grid-aligned; free angle deferred).  Flip is part of the
rotate family (see §3).

### 1.1 Feasibility: several bindings for one action

**Question:** can wheel + `[Up]` + `[Down]` + the configurable rotate action
all drive the same step at once?

**Answer: yes, by design — but only one of them is the *rebindable* binding.**
The `KeyMap` resolves exactly one shortcut per action id (`KeyMap.matches`
→ `resolve` returns a single string).  The established pattern in
`SelectTool.on_key` already OR-matches a configurable action against raw
fallback keys, e.g.:

```python
if (self._key_matches(event, _ACT_DELETE)
        or (key == Qt.Key.Key_Backspace and not ctrl)):
```

So the armed-rotate key handler does the same: the **configurable** binding
stays single and rebindable (`select-tool.rotate.cw` / `.ccw`), while
`[Up]`/`[Down]` are **fixed in-mode conveniences** OR-ed in, and the **wheel**
is routed separately (see §1.2).  This keeps "one action, one rebindable key"
true for the shortcut config while still accepting several physical inputs in
the mode.

### 1.2 Feasibility: wheel drives rotation in-mode

`BoardView.wheelEvent` currently *always* zooms (accumulates ticks → deferred
`_flush_wheel_zoom`).  To let the wheel rotate while float-rotate is armed, add
one seam: before zooming, consult the active tool —

```python
def wheelEvent(self, event):
    tool = self._active_tool
    if tool is not None and tool.wants_wheel(event):   # new tool hook, default False
        tool.on_wheel(event)                            # rotate ±90° per tick
        event.accept()
        return
    # ... existing zoom accumulation ...
```

`wants_wheel` defaults to `False` on `_Tool`, so zoom is unaffected for every
tool/state except an armed float-rotate.  Mirrors the existing `wants_key`
veto seam (board_view.py `_Tool.wants_key`).

---

## 2. The bigger direction — SelectTool delegates to sub-tools

> *"wiele akcji istniało jako część select-toola, teraz będą one delegowane.
> np. wire-simplify będzie przekierowany do modułu edit-wire."*

SelectTool has accreted operations that **belong to a narrower context**.  The
direction: SelectTool stays the *selection + dispatch* surface; context-bound
operations move into **delegated sub-tools** that SelectTool *hands control to*
(arm → operate → disarm is just transfer of control).  The atomic
`[arm, action, disarm]` caller API (a menu/macro arming a sub-tool, invoking
one of its actions, and disarming — all in one shot) is the **eventual** shape,
**explicitly deferred**.  This doc only *identifies* the candidates.

### 2.1 Candidate operations in `select_tool.py` (the identification)

Grouped by their natural owner.  "Armed today" = already an armed sub-mode.

#### A. Wire operations → **wire-edit sub-tool** (already partly armed)

The wire-edit op-table (`_wire_edit_on_key`) is the prototype of a delegated
sub-tool.  These all act on a wire / fragment and should live there, not on
SelectTool:

| Method | Today | Target |
|---|---|---|
| `arm_wire_edit` + `_range_picked` | armed mode (`[E]`) | the sub-tool's entry |
| `_begin_reroute` / `_reroute_*` / `_commit_reroute` | wire-edit op `[W]` | sub-tool op |
| `_autoroute_fragment` | wire-edit op `[A]` | sub-tool op |
| `_delete_fragment` | wire-edit op `[Del]` | sub-tool op |
| `_flip_wire_fragment` | **stub** (deferred, see todo.md) | sub-tool op-table entry |
| `_move_selected_wires_to_side` | SelectTool method (menu) | wire sub-tool / wire-cmd module |
| `_wire_net_map`, `_wire_by_id`, `_refresh_after_wire_op`, `_wire_is_locked` | SelectTool helpers | move with the wire sub-tool |

**wire-simplify** (the example you gave): today *Simplify Selected Wires* is a
`MainWindow` action (`_simplify_selected_wires`) backed by a worker — it is not
on SelectTool, but it is a *wire-context* operation that, under this direction,
should be reachable as a wire-edit / wire sub-tool op (so simplify, reroute,
autoroute, flip, layer-move form one coherent wire op-table) rather than a
free-floating top-level action.

#### B. Transform operations → **float-transform sub-tool** (this doc's mode)

Rotate + flip + rotate-around-point are the same family — geometric transform
of a float selection.  They are scattered across SelectTool today:

| Method | Note |
|---|---|
| `_rotate_selected`, `_rotate_multi` | per-object vs shared-pivot dispatch |
| `_flip_selected`, `_flip_multi` | flip (2-pin only today) |
| `begin_rotate_around_point` / `_begin_rotate_around_point` / `_clear_pivot` | pivot capture |
| `_apply_rotate`, `_remove_crossing_wires` | post-transform wire cleanup |
| `_pivot_capture_pending`, `_rotate_pivot` (state) | becomes the sub-tool's state |

These collapse into one **float-transform** sub-tool: arm on `[R]`, hold the
float + pivot, expose rotate-cw / rotate-ccw / flip / set-pivot as its op-table,
commit one command on `[Enter]`.

#### C. Re-drag → **redrag sub-tool** (already armed)

`begin_redrag` / `_redrag_*` / `_free_bendable_pin` is already a self-contained
armed pick (`Ctrl+R`).  It is the cleanest existing example of the target
shape and would extract with little change.

#### D. Stays on SelectTool (genuinely selection-surface)

Selection set management, hit-testing, rubber-band, group enter/exit, net
relation/highlight, the drag gesture itself, and the **dispatch** that arms the
sub-tools.  SelectTool remains the thing that knows *what is selected* and
*which sub-tool a key/click should be handed to*.

### 2.2 Why this is the right cut (not type-branching)

The sub-tools share a shape already visible in the code: **arm → (op-table |
pick) → commit/cancel**, with a tool-local state bundle and an overlay.
wire-edit and redrag already implement it ad-hoc.  Extracting a small
`ArmedSubTool` protocol (arm / handle-key / handle-wheel / commit / cancel /
draw-overlay) lets SelectTool dispatch uniformly instead of branching on which
mode is active — the same "shared interface over type-branch" principle the
`consider-polymorphism` skill enforces.  **Do that analysis before coding the
protocol** — the three sub-modes must genuinely share the interface, not be
forced into it.

---

## 3. Flip within the transform mode

Flip belongs to the float-transform sub-tool (§2.1 B), invoked by its own
op-table entry (the rotate-family key, e.g. `Alt+R`, or a dedicated in-mode
key).  Live preview on the float; committed together with any pending rotation
as one undo entry.  **Constraint preserved:** flip currently applies only to
2-pin components (`footprint.pin_count() == 2` in `_flip_selected` /
`_flip_multi`); keep that gate until a general flip is designed.

---

## 4. Implementation staging (when scheduled)

1. **Wheel seam** — `_Tool.wants_wheel`/`on_wheel` hooks + `BoardView.wheelEvent`
   consult (no behaviour change while no tool wants the wheel).
2. **Float-transform sub-tool** — extract rotate/flip/pivot (§2.1 B) into an
   armed sub-tool; `[R]` arms it; wheel + arrows + actions drive ±90°; pad-click
   sets shared pivot; `[Enter]`/`[Esc]` commit/cancel; one command per commit.
3. **Wire fragment-flip** — land the deferred `_flip_wire_fragment` as a
   wire-edit op-table entry (todo.md item) — first concrete "operation moves
   into the sub-tool" step.
4. **ArmedSubTool protocol** — once two sub-tools exist, factor the shared
   arm/op/commit shape (only if the polymorphism check passes).
5. **Atomic `[arm, action, disarm]` caller API** — deferred; design once the
   protocol is real.

Nothing above is committed until each stage has a green suite + a smoke test,
per CLAUDE.md §8.
