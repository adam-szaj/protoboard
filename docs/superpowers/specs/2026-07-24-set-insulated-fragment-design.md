# Set-insulated on a wire sub-range — design

**Date:** 2026-07-24
**Branch:** `feat/item-id-handle-model` (worktree `pyright-fixes`)
**Status:** Approved design — ready for implementation plan.

Continuation of the wire-range unification work
(`memory/project_wire_range_unification_2026_07_24.md`,
`memory/project_armed_subtool_lifecycle_2026_07_24.md`,
`memory/project_set_insulated_fragment_2026_07_24.md`).

---

## 1. Goal & core insight

Let the user set a **sub-range** of a wire insulated (or bare), not just the
whole wire. `[I]` on a fragment selection toggles that slice's insulation.

`Wire.insulated` is **per-wire** (`domain/layout/wire.py:169`). `Fragment`
carries only `path` + `side` — there is **no** `Fragment.insulated` field. So
"make this middle stretch insulated" cannot be a flag flip: it must physically
**split** one wire into up to three wires
`[bare survivor] — [insulated target] — [bare survivor]`, each a real `Wire`
with its own id.

This is the same split + net-rederivation that `build_delete_command` already
performs when it mints half-wires — the cost §6.2 called "incoherent" is
already paid by delete-slice. It is also physically faithful: a bare wire with
heat-shrink only in the middle *is* three conductors (bare / insulated / bare).

---

## 2. The builder: `build_set_insulated_range_command`

New function in `controller/selection/wire_edit.py`, sibling to
`build_delete_command`, same signature shape plus a target flag:

```python
def build_set_insulated_range_command(
    wires, wire_net_map, state, insulated, add_factory=_default_add_factory,
) -> WireRangeEdit | None: ...
```

`insulated: bool` is the target value for the selected slice. Three cases,
keyed exactly like delete (`n = len(wire.path)`):

### Case A — full-span (`start == 0 and end == n - 1`, same wire)

No cut. Return a `WireRangeEdit` wrapping
`SetWireInsulatedCommand(wire.id, insulated)` (id-preserving, in-place),
`was_cut=False`, `survivor_wire_ids=()`. Mirrors today's whole-wire menu
action; tool stays armed on the same id.

### Case B — same-wire sub-range (the new heart)

Split into **up to three** pieces. Unlike delete (which drops the middle),
set-insulated **re-adds the middle** with the new insulation:

| piece         | path                    | insulated             |
|---------------|-------------------------|-----------------------|
| left survivor | `wire.path[:start + 1]` | `wire.insulated`      |
| **target**    | `wire.path[start:end+1]`| `insulated` (new)     |
| right survivor| `wire.path[end:]`       | `wire.insulated`      |

`RemoveWireCommand(wire.id)` first, then for each piece with `len >= 2`
`add_factory(Wire(id=uuid.uuid4(), path=piece, insulated=…, side=wire.side,
color=wire.color))`. Boundary vertices are shared (`[:start+1]` and `[start:]`
both include index `start`) — the same overlap convention delete uses, so
via/knot points line up. Wrap in
`MacroCommand(description="Insulate wire fragment" | "Bare wire fragment")`.
Degenerate slice touching one end → 2 pieces, not 3.

**No-op guard:** if the slice's would-be `insulated` already equals the wire's,
return `None` (tool shows a status hint). Under the `[I]` toggle this never
fires, but it stays as a safety net for programmatic callers.

### Case C — cross-wire sub-range — **refused**

Set-insulated wants to *keep* the middle, but `excise_cross_wire_range` drops
it, so Case C is not a verbatim excise. Insulating a stretch that crosses a
plated-through via between two wires (possibly on different sides) has murky
semantics. Fragment **flip** already refuses cross-wire today; set-insulated
matches that: return `None` (status hint "select a range within one wire").
Full cross-wire re-add is **deferred** → one line in `docs/todo.md`.

---

## 3. Builder result type & the unified "wait-for-re-pick" tool state

### `WireRangeEdit`

The builder must surface what it minted so the caller can pick the right
post-op behavior (today the caller gets an opaque `Command`):

```python
@dataclass(frozen=True)
class WireRangeEdit:
    command: Command[ComponentState]
    survivor_wire_ids: tuple[uuid.UUID, ...]  # fresh ids; () if whole wire gone
    was_cut: bool                             # True = a sub-range split happened
```

### Post-op decision tree (unified for delete + insulate)

| op result                                | behavior                                   |
|------------------------------------------|--------------------------------------------|
| full-span, no cut (Case A)               | `_commit_and_rearm` — stays armed, same id |
| sub-range cut, ≥1 survivor (Case B)      | **new** `_arm_repick` — disarm, wait       |
| whole wire gone, nothing survived        | `_exit()` — as today (delete full-span)    |

Dispatch reads `WireRangeEdit`:
`was_cut and survivor_wire_ids → _arm_repick`;
`was_cut and not survivor_wire_ids → _exit`;
`not was_cut → _commit_and_rearm`.
**Delete is ported onto this same result type** so both cutting ops share one
decision path (the unification requirement).

### The `_awaiting_repick` flag (do NOT overload `_wire_edit is None`)

`gui/tools/gui_tools.py:223` keys `armed = lambda: self._wire_edit is not None`
and `key_claims` uses it to veto global shortcuts. Signaling "waiting" by
`_wire_edit = None` would make the tool look unarmed and leak globals
(`[W]`=Route, `[S]`, `[F]`, `[I]`) mid-wait. So add:

```python
self._awaiting_repick: bool = False
```

"Waiting" = `_wire_edit is None AND _awaiting_repick`. `key_claims` changes to
`armed = (_wire_edit is not None) or _awaiting_repick`; op keys stay claimed,
globals stay blocked during the wait.

### One deliberate invariant relaxation

`_wire_edit_wire_id` is treated as immutable after `__init__` today.
`_arm_repick` **reassigns** it to a survivor id so the next vertex-pick
tie-breaks against a wire that still exists. Documented at the field and here.

### Re-pick mechanics (near-zero new machinery)

The wait reuses the existing generic pick:
`_arm_pick("wire-vertex", Stop.count(2), on_done=self._range_repicked, …)`.
The `wire-vertex` branch of `_pick_at` already does net-restricted
`hit_wire_vertex` for any anchor. `_range_repicked` ports
`select_tool._range_picked`'s vertex→`WireRange` resolve + normalise (same-wire
swap `start ≤ end`, zero-length abort), then arms the new range in place and
clears `_awaiting_repick`.

`has_pending_op` already returns `True` during a live pick, so two-stage Esc is
already correct: first Esc cancels the in-flight pick, second Esc exits.

---

## 4. Keybinding `[I]` (single-key toggle)

Per `memory/feedback_every_action_has_keybinding` — registered end to end, the
exact path `flip_fragment` already uses:

- **Action-id** (`wire_edit_tool.py:60-65`):
  `_ACT_SET_INSULATED_FRAGMENT = "select-tool.action.set_insulated_fragment"`.
- **command_table** (`:157`): `_ACT_SET_INSULATED_FRAGMENT →
  self._set_insulated_fragment`; add to `displayable_action_ids()` (`:169`).
- **Raw key** (`gui_tools.py` `key_actions()`, `:198`):
  `"select-tool.action.set_insulated_fragment": "I"`.
- **Claim** (`gui_tools.py` `key_claims()`, `:217`):
  `("select-tool.action.set_insulated_fragment", armed)` with the extended
  `armed` from §3.

**Toggle direction:** `[I]` toggles relative to the anchored wire's current
`insulated`. `_set_insulated_fragment()` reads the wire's current value and
passes `not current` as the target. Single key, symmetric with `[F]`. The
whole-wire context menu keeps its explicit "Set Insulated" / "Set Bare"
entries for the direction-explicit case.

---

## 5. Testing (TDD — failing test first, always)

Bottom-up; each level green before the next.

1. **Builder unit tests** (`tests/unit/test_wire_edit_helpers.py`, beside the
   delete/flip goldens):
   - Case A full-span → `SetWireInsulatedCommand`, `was_cut=False`, id preserved.
   - Case B sub-range → `MacroCommand`; execute → 3 wires; **middle**
     `insulated == target`, **outer two** `insulated == original`; all share
     `side`/`color`; boundary vertices line up; `survivor_wire_ids` = fresh ids;
     `was_cut=True`.
   - Case B degenerate (slice touches one end) → 2 pieces.
   - Case C cross-wire → `None` (refused).
   - No-op (slice already at target) → `None`.
   - **Delete ported onto `WireRangeEdit`** → existing delete goldens pass
     through the new result type.
2. **Tool state tests** (headless `HeadlessBoardView` + pointer/key DTOs):
   - Sub-range insulate → `_awaiting_repick is True`, `_wire_edit is None`,
     `_wire_edit_wire_id` reassigned to a survivor.
   - During wait: op keys claimed (globals blocked); `[Esc]` exits.
   - Re-pick a fresh 2-vertex range → re-arms; `_awaiting_repick` False.
   - Full-span insulate → stays armed (no wait).
   - **Delete parity**: delete sub-range now *also* waits (was `_exit`).
3. **mypy --strict** green (`WireRangeEdit` typed; `_awaiting_repick: bool`).
4. **Manual GUI smoke** via the `run-protoboard-designer` skill: load a fixture
   with a real wire, `[I]` a middle slice, confirm three segments render
   (bare / insulated / bare), and re-pick works.

---

## 6. Documentation updates

- **`docs/core-wire-semantics.md` §6.2** (`:229-240`): rewrite the "would be
  incoherent — setting insulation on a fragment has no model representation"
  claim. New text: fragment-insulate **is** feasible via split + re-add (the
  same net-rederivation `delete-slice` already performs); mints an insulated
  middle + bare-end survivors; physically faithful. Update the GAP row (`:158`)
  and the granularity-contract recommendation (`:299`). Cross-wire
  fragment-insulate stays deferred.
- **`docs/todo.md`**: one line — cross-wire fragment set-insulated deferred
  (Case C2), with date and this spec's reference.
- **`CHANGELOG.md`**: entry at the start of implementation (CLAUDE.md §8).

---

## 7. Out of scope / deferred

- Cross-wire fragment set-insulated (Case C2 — full re-add across a via).
- Two-key insulate/bare (`[I]` toggle chosen instead).
- Any change to whole-wire menu semantics (its explicit Set Insulated / Set
  Bare entries are untouched).
