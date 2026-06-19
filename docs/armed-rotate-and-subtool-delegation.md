# Armed-rotate mode + sub-tool delegation (design)

Status: **armed-rotate + armed-move SHIPPED** (2026-06-19); the sub-tool
delegation direction (§2) is still design-only.  Captures the agreed shape of
the armed transform interactions and the larger direction they belong to
(SelectTool sheds operations to delegated sub-tools).

Shipped so far:

* `[R]` arms float-rotate — wheel / `[↑]` / `[↓]` / `[R]` step ±90° (each a
  real undoable command), click a pad = shared pivot for the whole selection
  (re-click resets), `[Enter]` accepts, `[Esc]` cancels the whole session
  (undoes every step since arming).
* `[G]` arms keyboard float-move ("grab") — arrows / WASD step the anchor one
  grid cell, reusing the whole `DragController` engine (snap / conflict-classify
  / live preview) via a new `nudge_to`; `[Enter]` drops, `[Esc]` cancels.
* Wheel seam: `_Tool.wants_wheel` / `on_wheel` consulted by
  `BoardView.wheelEvent` before zooming (default off → zoom unchanged).
* Both are namespaced KeyMap actions (`select-tool.action.rotate` / `.move` /
  `.flip`), rebindable, and surface in the `[?]` overlay + RMB menu.

Still design-only: the §2 delegation refactor and the atomic
`[arm, action, disarm]` caller API.

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

## 4. Implementation staging

1. **Wheel seam** — ✅ DONE (2026-06-19).  `_Tool.wants_wheel`/`on_wheel` hooks +
   `BoardView.wheelEvent` consult (no behaviour change while no tool wants the
   wheel).
2. **Armed float-rotate** — ✅ DONE (2026-06-19).  `[R]` arms; wheel / `[↑]` /
   `[↓]` / `[R]` step ±90° (each a real undoable command via `_rotate_multi`/
   `_apply_rotate`, so crossing wires are cleaned); pad-click sets a shared
   pivot (re-click resets to per-object); `[Enter]` accepts; `[Esc]` cancels the
   whole session (undoes back to the arm-time undo-stack floor).  A legacy
   `begin_rotate_around_point` pivot capture seeds the shared pivot, converging
   the two pivot routes.  *Superseded by stage 5:* the tool-local `_RotateArm`
   was replaced by the `ArmedRotate` sub-tool; rotate is now preview-then-commit
   (see stage 5) rather than per-step-commit.
3. **Armed keyboard float-move** — ✅ DONE (2026-06-19).  `[G]` ("grab") arms a
   `DragController` driven by the keyboard: arrows / WASD step the anchor one
   grid cell via the new `DragController.nudge_to`, reusing the entire drag
   engine (snap / conflict-classify / live preview); `[Enter]` confirms (CONFIRM
   intent → commit), `[Esc]` cancels (CANCEL intent).
4. **Wire fragment-flip** — land the deferred `_flip_wire_fragment` as a
   wire-edit op-table entry (todo.md item) — first concrete "operation moves
   into the sub-tool" step.
5. **ArmedSubTool protocol** — ✅ DONE (2026-06-19).  `gui/selection/armed.py`
   defines the `ArmedSubTool` Protocol (arm → on_key/on_wheel/on_click →
   can_commit → accept/cancel + finished).  `armed_rotate.ArmedRotate` and
   `armed_move.ArmedMove` implement it; SelectTool holds one
   `self._armed: ArmedSubTool | None` and dispatches uniformly (the
   `_rotate_arm` / `_move_arm_*` tool-local state and the parallel
   `_rotate_arm_on_key` / `_move_arm_on_key` methods are gone).  **Scope:** rotate
   + move folded in; wire-edit + redrag deliberately left ad-hoc (their op-table
   / pick shape is a looser fit — fold in later only if it maps cleanly).
   **Conditional-disarm contract** (the requirement that drove the protocol):
   `accept()` commits + finishes *only if* `can_commit()`; otherwise it parks
   armed in the conflict.  Rotate is now **preview-then-commit** (steps preview
   off the undo stack; accept commits ONE rotation by the net angle → one undo
   entry); a previewed rotate that lands in *any* conflict (SOFT or HARD) parks
   rather than auto-committing + dropping wires — the pre-refactor feel restored.
   Move wraps `DragController`, whose FLOATING-on-HARD already is the contract.
6. **Atomic `[arm, action, disarm]` caller API** — deferred; now a thin helper
   over the real protocol (arm → invoke one op → accept()), and because accept()
   is conditional, a programmatic op on a conflicting selection parks armed
   rather than forcing an illegal commit.

Nothing above is committed until each stage has a green suite + a smoke test,
per CLAUDE.md §8.

---

## 5. The `ArmedSubTool` protocol (design for stage 5)

**Polymorphism verdict (consider-polymorphism skill):** *Using* a shared
interface.  SelectTool's `on_key` / `on_press` / `wheelEvent` currently branch
on *which* armed mode is live (`if self._rotate_arm` / `elif
self._move_arm_active` / `elif self._wire_edit`).  The modes share one shape —
**arm → (handle key / wheel / click) → accept-if-committable / cancel** — so the
caller should hold one `self._armed: ArmedSubTool | None` and dispatch through
it without knowing the concrete mode.  The branches and the parallel
`_rotate_arm_on_key` / `_move_arm_on_key` / `_wire_edit_on_key` collapse into one
dispatch.

### 5.1 The conditional-disarm contract (the key requirement)

`[arm, action, disarm]` is **not** unconditionally atomic: `disarm` is
*conditional on the resulting state being committable*.  If an action leaves the
sub-tool in a conflict it cannot accept (a HARD overlap), the sub-tool **stays
armed** and waits for the user — exactly as the floating drag already does
(`DragController.on_release` holds `FloatingPhase.FLOATING` when
`not decision.can_commit`).  This restores the pre-refactor feel: an illegal
rotate/move does not silently commit-and-cleanup, it parks in a conflict state
the user resolves (step again to a legal spot, or cancel).

So the protocol needs a **commit-readiness** query, and `accept()` returns
whether it *actually* disarmed:

```python
class ArmedSubTool(Protocol):
    def on_key(self, event: QKeyEvent) -> bool: ...     # consumed?
    def on_wheel(self, event: QWheelEvent) -> bool: ...
    def on_click(self, scene_pt: Point) -> bool: ...    # e.g. set pad pivot
    def can_commit(self) -> bool: ...                   # False ⇒ HARD conflict
    def accept(self) -> bool: ...   # commit + disarm IF can_commit; else stay
    def cancel(self) -> None: ...   # always revert + disarm
    def status_hint(self) -> str: ...
```

SelectTool dispatch becomes uniform:

```python
if self._armed is not None:
    if self._armed.on_key(event):       # the sub-tool owns its keys
        if self._armed.finished:        # accept() that committed, or cancel()
            self._armed = None
        return
```

`accept()` semantics: classify the current state; if `can_commit()` → commit
(one undo entry) + mark finished; else **leave armed**, surface the conflict
(corner text / status), wait.  This is where the current rotate's auto-commit +
`_remove_crossing_wires` is *replaced* by "stay armed on conflict" — the
behaviour the user wants back.

### 5.2 Three current implementations to fold in

* **float-rotate** (`_RotateArm`) — `can_commit` runs `classify_drag_commit`
  over the rotated `comp_ids` (rotate moves positions, so the same classifier
  applies); HARD ⇒ stay armed.  Today it auto-commits each step and drops
  crossing wires; under the contract a step that lands HARD parks instead.
* **keyboard-move** (`_move_arm_active` + `DragController`) — already *has*
  `can_commit` via `last_decision.can_commit` and the FLOATING hold; it is the
  reference implementation.  Wrapping it in the protocol is mostly adapter work.
* **wire-edit** (`_wire_edit` + `_wire_edit_on_key` op-table) — its ops
  (reroute / autoroute / delete) are the op-table variant of "handle key";
  folding it in unifies the third ad-hoc armed mode.

Redrag is a pick (a degenerate armed sub-tool) and can join later.

### 5.3 Caveats for the implementation

* **Don't force wire-edit in if it fights the interface.**  The skill's rule:
  the three modes must *genuinely* share the shape.  rotate + move clearly do
  (both float a transform, both have a commit decision).  wire-edit's op-table
  is a looser fit (its "conflict" is per-op, e.g. reroute validity) — fold it in
  only if `can_commit` / `accept` map cleanly; otherwise leave it ad-hoc and
  note why.
* The shared **conflict surface** (corner text amber/red, status hint) already
  exists for drag (`_drag_preview` / `DragCornerText`); the protocol should
  route every sub-tool's conflict through it so the feedback is identical.
* Keep each sub-tool's command-commit on the existing domain commands
  (`RotateComponentsCommand` / `RotateAroundPointCommand` / `MoveDragSetCommand`)
  — the protocol unifies *control flow*, not the domain operations.

### 5.4 What this unblocks (stage 6)

With `ArmedSubTool` real, the atomic `[arm, action, disarm]` caller API is a thin
helper: `arm a sub-tool → invoke one op → accept()`.  Because `accept()` is
conditional, a menu/macro that triggers e.g. "rotate 90°" on a selection that
would conflict leaves the user in the armed conflict state rather than forcing
an illegal commit — same contract, now reachable programmatically.
