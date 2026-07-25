# Entry-Point Semantics Alignment — Design

Date: 2026-07-25
Branch: `feat/item-id-handle-model`
Related: `feedback_same_op_same_code_all_entrypoints`, `project_tools_belong_to_controller`,
`project_wire_side_stale_snapshot_2026_07_25` (commit `cd01742`).

## 0. Problem

An audit of every operation invokable from more than one entry point
(keybinding / main-menu / context-menu) found three operations whose entry
points **do not converge on one code path** — each builds its domain command
independently in two places. Duplicated construction is how the flip-fragment
stale-`wire.side` bug (`cd01742`) slipped in, and it is how a second, still-live
bug survives: **whole-wire "Flip" of a mixed-side wire flattens it to one layer
instead of toggling each fragment.**

### Divergent operations (verified in code @ `cd01742`)

| Op | main-menu path | context-menu path | Divergence |
|---|---|---|---|
| Wire set insulated/bare | `main_window._set_selected_wire_insulated` :1934 — reads `_get_selected_wire_ids()`, `stack.execute`, desc "Set N wires insulated/bare" | `select_tool._menu_set_wire_insulated` :3303 — receives `wire_ids`, `_execute_command`, desc "Set N wires" | Same `SetWireInsulatedCommand`, **two constructions**, different selection source + execution tail + description |
| Wire move-to-side / flip | `main_window._move_selected_wires_to_side` → `select_tool._move_selected_wires_to_side` :3646 — toggle-capable (`side=None`), cross-wire same-side guard | `select_tool._menu_set_wire_side` :3326 — explicit side only, no guard | Different handlers, different logic, two `SetWireSideCommand` builders |
| Lock / unlock | `main_window._lock_toggle` :1819 — own toggle logic + status msg, inline build | `select_tool._menu_lock` :3377 — explicit `locked` bool, `_execute_command` | Toggle-vs-explicit + separate construction |

### Deliberately-separate verbs (NOT divergences — kept distinct)

- **Per-fragment flip** `[F]` (WireEditTool → `FlipWireFragmentCommand`) vs
  **whole-wire flip** (menu/PPM). Distinct domain commands by design.
- **`Side: Top/Bottom`** (`T`/`B`, RouteTool draw-default for *new* wires) vs
  **`Wire: Move to Top/Bottom`** (mutate *existing* selected wires).

## 1. Architecture — the seam

Each divergent op becomes **one unified verb on `EditController`** (the Qt-free
façade in `controller/editing/controller.py`), mirroring the existing `flip` /
`rotate` verbs. Both the menu-bar slot (`main_window`) and the context-menu
handler (`select_tool`) call the **same** façade verb. Command construction
lives in **one** place (the façade's `_targets` helper), never duplicated.

Per the MVC rule (`project_tools_belong_to_controller`): the operation is
**exposed by the controller façade** (the CLI-reachable verb surface) and its
command-building is implemented at the controller layer — the same place
`flip` / `rotate` already live. `main_window` slots and `select_tool` ctx
handlers become thin call-throughs.

Verbs take an **explicit target** — never "toggle". Toggle is resolved by the
caller into a concrete target/verb *before* the façade call, so no verb re-reads
mutable state to decide what it does. `flip_wires` is the exception: it is a
toggle *by definition* (invert each fragment), but it reads no `wire.side` to
choose a direction — it always inverts — so there is no stale-snapshot hazard.

| Op | New façade verb | Replaces (deletes duplication) |
|---|---|---|
| Wire insulated/bare | `set_wires_insulated(ids, *, insulated)` | `main_window._set_selected_wire_insulated` build + `select_tool._menu_set_wire_insulated` build |
| Wire side (explicit) | `set_wires_side(ids, *, side)` | `select_tool._menu_set_wire_side` build; the explicit branch of `_move_selected_wires_to_side` |
| Wire flip (toggle) | `flip_wires(ids)` — per-fragment toggle | the `side=None` branch of `_move_selected_wires_to_side` (**bug fix**) |
| Lock/unlock | `set_locked(ids, *, locked)` | `main_window._lock_toggle` build + `select_tool._menu_lock` build |

Toggle resolution stays at the caller:
- Menu **"Lock / Unlock"**: `main_window._lock_toggle` keeps its all-locked→unlock
  decision (reads current placements), then calls `set_locked(ids, locked=new)`.
- Menu **"Wire: Flip"**: calls `flip_wires(ids)` (no direction to resolve).
- Ctx **"Move to <other> layer"**: computes the explicit other side from the
  wire snapshot (as today), calls `set_wires_side(ids, side=other)`.
- Ctx **"Set insulated" / "Set bare"**: explicit bool → `set_wires_insulated`.

`flip` / `rotate` (`controller.py:668` / `:556`) are the working template:
public verb → `_targets` helper folds all commands into one macro → `_execute`.
The new verbs follow the same shape.

## 2. Mixed-wire flip fix

### The bug

`select_tool._move_selected_wires_to_side(None)` (whole-wire **Flip**,
`:3646`) is a flatten path:

1. `:3662` computes `sides = {w.side for w in wires}`. For a single mixed wire
   `[top,bottom,top]`, `w.side` is the stale construction snapshot
   (`fragments[0].side` = `top`), so `sides = {"top"}`; the guard passes and
   `resolved_side = "bottom"`.
2. `:3679` builds `SetWireSideCommand(side="bottom")`, whose `apply` sets
   `[side] * len(fragments)` (`component_cmds.py:823`) → **all fragments →
   bottom**. The mixed wire is flattened.

Two compounding faults: (a) reads stale `wire.side` (same root cause as
`cd01742`); (b) `SetWireSideCommand` is a flatten command, structurally wrong
for "flip".

### The fix — a per-fragment toggle command

New `FlipWireSideCommand` in `component_cmds.py`, sibling to
`SetWireSideCommand`, toggling **each fragment independently**. Uses the new
`WireCollection.get_or_raise()` (see §2a) instead of `.get()` + manual
`if None: raise` — no manual `for wire in wires` loop (the pre-existing loops in
`component_cmds.py` are legacy lint-debt, left untouched per surgical-changes;
new code is written clean).

```python
@dataclass
class FlipWireSideCommand(Command[ComponentState]):
    """Flip a whole wire by toggling EACH fragment's side independently.

    A mixed wire [top,bottom,top] becomes [bottom,top,bottom] — it stays
    mixed, inverted. Distinct from SetWireSideCommand, which flattens all
    fragments to one side ("Move to <side>").
    """
    wire_id: uuid.UUID
    _old_fragment_sides: list[str] = field(default_factory=list, init=False, repr=False)

    @property
    def description(self) -> str:
        return "Flip wire"

    def apply(self, state: ComponentState) -> None:
        wire = state.layout.wires.get_or_raise(self.wire_id)
        self._old_fragment_sides = [f.side for f in wire.fragments]
        flipped = ["bottom" if s == "top" else "top" for s in self._old_fragment_sides]
        wire.fragments = rebuild_fragments(wire.fragments, flipped)
        wire.side = wire.fragments[0].side  # keep the stale snapshot self-consistent

    def revert(self, state: ComponentState) -> None:
        wire = state.layout.wires.get(self.wire_id)
        if wire is None or not self._old_fragment_sides:
            return
        wire.fragments = rebuild_fragments(wire.fragments, self._old_fragment_sides)
        wire.side = wire.fragments[0].side
```

### 2a. `WireCollection` lookup ergonomics

The `.get()` + `if x is None: raise/return/continue` idiom repeats ~15× across
`wire_utils.py`, `drag_cmds.py`, and the wire commands. Add two methods to
`WireCollection` (`wire.py`), alongside the existing `.get() -> Wire | None`:

```python
def get_or_raise(
    self,
    wire_id: uuid.UUID,
    exc: Callable[[uuid.UUID], Exception] | Exception | None = None,
) -> Wire:
    """Return the wire, or raise. Default miss -> KeyError(wire_id).

    ``exc`` may be a ready Exception or a factory taking the missing id;
    the hook returns/builds an Exception — it never returns a Wire (recovery
    is get_or_return's job).
    """
    wire = self.get(wire_id)
    if wire is not None:
        return wire
    if exc is None:
        raise KeyError(wire_id)
    raise exc(wire_id) if callable(exc) else exc

def get_or_return(
    self,
    wire_id: uuid.UUID,
    instead_val: Callable[[uuid.UUID], Wire] | Wire,
) -> Wire:
    """Return the wire, or a fallback (eager value or lazy factory)."""
    wire = self.get(wire_id)
    if wire is not None:
        return wire
    return instead_val(wire_id) if callable(instead_val) else instead_val
```

Scope: **`WireCollection` only** this task. Generalizing to `placements` /
`instances` / `components` (a shared id-keyed-collection mixin, ~232 `.get()`
sites) is a noted follow-up, out of scope here. Existing callers are **not**
mass-migrated; `FlipWireSideCommand` uses `get_or_raise` as the first adopter,
and the new wire-side / insulated verbs adopt it where they build commands.

### Wiring into the verb

`flip_wires(ids)` builds one `FlipWireSideCommand` per selected wire (macro if
>1). It reads **no** `wire.side` to decide anything, so the stale-snapshot fault
cannot recur. The old `sides` "different layers" guard (`:3663`) is **deleted**
for the flip path: a per-fragment toggle is well-defined for any mix, so
multi-wire mixed selections just work.

`set_wires_side(ids, side)` keeps `SetWireSideCommand` (explicit flatten —
"Move to Top/Bottom" semantics unchanged).

### Why a dedicated command (not a macro of `FlipWireFragmentCommand`)

`FlipWireFragmentCommand` toggles one fragment's live side; `flip_wires` could
emit a macro of it over every fragment. We choose the dedicated
`FlipWireSideCommand` instead: one command = one undo step with a clean "Flip
wire" description, no per-fragment range plumbing, mirrors `SetWireSideCommand`'s
structure. The fragment command stays the tool-level `[E]→[F]` op.

## 3. Testing (TDD — failing test first)

Domain-level (no Qt), the load-bearing cases:

1. **Mixed-wire flip toggles per fragment** — wire `[top,bottom,top]`,
   `FlipWireSideCommand.apply` → `[bottom,top,bottom]`; `revert` restores
   `[top,bottom,top]`. **This is the bug regression test — write it first, watch
   it fail against the current flatten path.**
2. **Uniform wire flip** — `[top,top]` → `[bottom,bottom]` (flip still works for
   the common case).
3. **`set_wires_side` still flattens** — mixed wire + explicit `side="top"` →
   `[top,top,top]` (Move-to-side semantics preserved).
4. **Multi-wire mixed flip** — two wires, different mixes, one `flip_wires`
   call → both inverted, one macro / one undo. No "different layers" refusal.

Parity (same-op-same-code) — for each unified verb, assert the menu path and
the ctx path reach the **same** façade verb and produce identical layout state:

5. `set_wires_insulated` — menu slot vs ctx handler → identical result + one
   command class.
6. `set_locked` — menu toggle (via `_lock_toggle`) vs ctx explicit → identical
   result for the same target set.
7. `set_wires_side` — menu "Move to bottom" vs ctx "Move to bottom" → identical.

Test the **key/click path**, not just the method (lesson from `cd01742` and
set-insulated: a gate can be wired to the method but bypassed by the raw-key
dispatch). Use the Headless BoardView API where a tool entry point is involved.

## 4. Scope / non-goals

- Do **not** rewrite the 6 other pre-existing manual `for wire in wires` loops
  in `component_cmds.py` (surgical-changes; not this task).
- Do **not** merge the deliberately-separate verbs (`Side: Top/Bottom` draw
  default vs `Move to <side>`; per-fragment `[F]` vs whole-wire flip).
- Component flip single/multi split (`_flip_selected` vs `_flip_multi`) is
  low-risk and already routes through `EditController.flip` for multi — left as
  a follow-up note, not in this task.

## 5. Convergent ops (already correct — no change)

Delete, Move-component-to-side, Simplify/Unweave/Merge wires, Endpoint↔wire-pin
convert. Listed for completeness; the audit confirmed each reaches one handler
from all entry points.
