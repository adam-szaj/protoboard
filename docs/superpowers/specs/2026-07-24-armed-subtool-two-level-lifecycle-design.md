# Design — `ArmedSubTool` two-level lifecycle + wire-edit sub-tool extraction

Date: 2026-07-24
Branch: `feat/item-id-handle-model`
Supersedes the deferral in
`~/.claude/plans/zr-b-plan-task-w-w-serialized-crescent.md` §Stage 7.

## 1. Problem

Three armed/sticky tools each hard-wire "commit the current operation" to
"exit the tool", and do it *inconsistently*:

- **`ArmedTransformTool`** (`[R]`/`[G]`): `accept`/`cancel` commit the transform
  **and return to SelectTool** (`_after_verb → _return_to`,
  `armed_transform_tool.py:234-250`). One op per arming; to do another you re-arm.
- **Wire-edit** (`[E]`, an internal sub-mode of `SelectTool`): every committing
  op (`[A]` autoroute, `[F]` flip, `[S]` simplify, `[Del]` delete, and reroute's
  Enter-commit) calls `_wire_edit_clear()` as part of the op
  (`select_tool.py:2422, 3929, 3964, 2440, 2377`), dropping back to plain select.
- **`RouteTool`** (manual routing): **asymmetric** — confirming a wire (`_commit`,
  `route_tool.py:195-227`) correctly *stays* in routing, but `[Esc]` mid-route
  **exits** routing. The exit is not RouteTool's doing: `ToolHost.handle_key_press`
  calls `view.escape_to_select()` **unconditionally** on every `Key_Escape`
  (`tool_host.py:534-545`), on top of RouteTool's own `_cancel`.

The user directive (verbatim intent): unify *how a tool is entered and how
commit/cancel/Esc behave*. Commit and cancel act on the **current operation**
and leave the tool armed; **`[Esc]` is the only thing that exits** — two-stage:
cancel a pending op if there is one, else exit. The RouteTool asymmetry
("Esc mid-route exits but confirm stays") is the concrete bug this fixes.

## 2. Organizing principle — two nested lifecycles

Every armed/sticky tool has two lifecycles:

- **Operation lifecycle (inner):** `commit_op` applies the current op; `cancel_op`
  reverts it. Both return the tool to a clean *armed/ready* state — they do **not**
  exit.
- **Tool lifecycle (outer):** `[Esc]` from a clean state (nothing pending) is the
  **only** exit back to SelectTool.

`[Esc]` is deliberately overloaded: **cancel the pending op if one exists, else
exit the tool** — one key, two stages. This single branch, in one place, is the
entire behaviour and makes all three tools' tables identical:

| | commit op | cancel op | exit tool |
|---|---|---|---|
| ArmedTransform | `[Enter]` → commit, re-arm on selection | `[Esc]`¹ → revert, re-arm | `[Esc]` when clean |
| Wire-edit → `WireEditTool` | `[A]/[F]/[S]/[Del]` → commit, keep fragment armed | `[Esc]`¹ → revert overlay, keep fragment | `[Esc]` when clean |
| RouteTool | `[Enter]` → commit wire, keep routing (already does) | `[Esc]`¹ → clear pending wire, keep routing | `[Esc]` when no points |

¹ two-stage `[Esc]`.

## 3. The `ArmedSubTool` contract (formalized)

Today "ArmedSubTool" exists only in docstrings (`armed_transform.py:99`). We
formalize it as a `Protocol`, splitting op-lifecycle from tool-lifecycle:

```python
class ArmedSubTool(Protocol):
    # --- operation lifecycle (inner) ---
    def commit_op(self) -> bool: ...     # commit current op; True=committed, False=parked (HARD)
    def cancel_op(self) -> bool: ...     # revert pending op; True=something was pending
    def has_pending_op(self) -> bool: ...
    def can_commit(self) -> bool: ...    # False ⇒ HARD conflict ⇒ park (stay armed)

    # --- tool lifecycle (outer) ---
    def on_key(self, inp) -> bool: ...   # op-table dispatch; returns False for verbs it doesn't own
    def can_exit(self) -> bool: ...      # usually == not has_pending_op()
```

The shared `[Esc]` handler (one place):
`if sub.has_pending_op(): sub.cancel_op() else: exit_to_select()`.

**Partial conformance is explicitly allowed** (per
`docs/armed-rotate-and-subtool-delegation.md` §5.3): a tool need not use every
capability. `on_key` returns `False` for verbs it does not own; `can_commit`
may be trivially `True` for a tool with no conflict concept. This is what lets
RouteTool join without a rich op-table.

### 3.1 Conflict / park semantics (kept)

A commit that lands in a HARD conflict **refuses and stays armed**, surfacing the
conflict via the existing amber/red channel (`DragCommitDecision.can_commit` →
`TransformOverlays.show_conflict`, drag_controller's `FloatingPhase.FLOATING`
park). Since `commit_op` now always stays armed, "park" is the natural default —
no special exit case.

## 4. Per-subsystem changes

### 4.1 `ArmedTransformTool` — decouple commit/cancel from exit
- `_cmd_confirm`/`_cmd_cancel` become `commit_op`/`cancel_op`: commit (or revert)
  the transform, then **re-arm** on the (possibly moved) selection instead of
  calling `_return_to`. Remove the `_after_verb → _return_to` coupling for
  commit/cancel.
- Exit only via the host `[Esc]`-when-clean path.
- HARD-conflict park unchanged (now the default, since commit always stays armed).
- `deactivate` safety-net (`armed_transform_tool.py:118-128`) unchanged.

### 4.2 Wire-edit → new `WireEditTool(ControllerTool)` (extraction)
Mirror the shipped `ArmedTransformTool` structure:
- Qt-free `WireEditArm` core in `controller/editing/` + `WireEditTool` in
  `controller/tools/`, entered via factory injection from
  `SelectTool.arm_wire_edit` (`select_tool.py:2104`), exactly as
  `_arm_transform` injects `_armed_transform_factory`.
- Op-table = the **shipped Stage-4/5 range builders** (`[W]` reroute, `[A]`
  autoroute, `[F]` flip, `[S]` simplify, `[Del]` delete). Each becomes
  **commit-and-stay-armed** — remove the `_wire_edit_clear()` calls from op bodies.
- **Preserve every refinement wholesale** (guarded by parity tests, §5):
  net-restricted hit-test, reroute waypoint-pick anchor, cyan/red overlays,
  `WireEditCycle` cluster cycling.
- `[Esc]`: two-stage — cancel a pending reroute waypoint pick if active, else exit.
- Whole-wire select-mode ops keep calling the **same** range builders with
  `range=full_span` (the directive's data-level unification, already shipped).

### 4.3 `RouteTool` — wrap in `ArmedSubTool` (partial) + fix host asymmetry
RouteTool has **no selection and no float** — its state is the growing
`list[Point]`. It therefore participates in `ArmedSubTool` **only at the
lifecycle level** (`commit_op`/`cancel_op`/`can_commit`/two-stage `[Esc]`); it
owns **no op-table verbs** beyond commit/cancel/insulate/side, and `on_key`
returns `False` for everything else. Do **not** give it a float or a rich
op-table to force a fuller fit — the partial conformance is intentional.
- Map `_commit`→`commit_op` (already stays), `_cancel`→`cancel_op`. Add
  `has_pending_op()` = `len(self._points) > 0`.
- **Fix the host bug:** `tool_host.py:542` stops calling `escape_to_select()`
  unconditionally. `[Esc]` routes to the tool first; the host exits to select
  **only** when the tool reports clean (`can_exit()` / did not consume). This
  gives two-stage Esc: first Esc clears the pending route (stay routing), second
  Esc (no points) exits. This host change also delivers two-stage Esc to the
  armed/wire-edit tools uniformly.
- **`can_commit()` (scoped — endpoint-on-pad only this round):** reuse
  `find_wires_crossing_pads` (`pad_conflict.py:507`) via a thin temp-`Wire`
  adapter (wrap the candidate `_points` as a `Wire` with the tool's `side` +
  `insulated`), plus `can_form_t_junction` net-check at bonding endpoints. Map a
  different-net pad hit to a HARD `DragCommitDecision` and drive the existing
  amber/red corner + park gate (same as `drag_controller`). A HARD route
  **refuses commit and stays armed** with the conflict shown.
- **Deferred (docs/todo.md):** general **wire-vs-wire same-side crossing/overlap**
  conflict — no existing primitive covers it; out of scope this round.

## 5. Parity net (gates the swap — CLAUDE.md "Rewrites and swaps")

Before any source change, capture characterization tests via
`tests/fakes/board_view.py::HeadlessBoardView` (API per memory
`project_headless_boardview_test_api`, pattern in
`tests/gui/test_headless_tool_flows.py`):

- **Wire-edit:** each op's resulting command-shape / resulting path. These
  *behaviour* tests are **repointed** to `WireEditTool` at swap time (not deleted
  with the old code); the "exits after op" assertions flip to "stays armed after
  op" as the intended behaviour change.
- **ArmedTransform:** commit/cancel command-shape + HARD-conflict park; add the
  new "stays armed after commit, exits only on clean Esc" assertions.
- **RouteTool:** `_commit` stays / two-stage `_cancel`+Esc; new endpoint-on-alien-
  pad → `can_commit()` False → park.
- **Directive acceptance test:** whole-wire simplify (select mode) and full-span
  fragment simplify (edit mode) still emit **byte-identical** commands after
  extraction.

## 6. Verification

- `uv run pytest` per stage (worktree needs `pythonpath=["src"]`, memory
  `feedback_worktree_pytest_pythonpath`). Green:
  `tests/unit/test_wire_edit_helpers.py`, `test_flip_wire_fragment.py`,
  `tests/gui/test_select_tool_parity.py`, `test_headless_tool_flows.py`,
  `test_simplify_tool.py`.
- `mypy --strict` clean on touched files.
- Smoke via `run-protoboard-designer`: enter `[E]`, pick a fragment, run `[S]`
  then `[F]` **without re-arming** (proves stay-armed); two-stage `[Esc]` exits.
  Manual route: `[Enter]` commits and stays; first `[Esc]` clears pending, second
  exits; a route ending on an alien-net pad refuses with the red corner.
- Conventional Commit per stage; update `docs/core-wire-semantics.md` and
  `docs/armed-rotate-and-subtool-delegation.md` as gaps close.

## 7. Scope boundaries (YAGNI)

- **Not** collapsing flip vs set-side (different ops — memory
  `project_wire_range_unification_2026_07_24` Stage-4 finding).
- **Not** building wire-vs-wire same-side crossing detection (deferred).
- **Not** changing the domain commands — the protocol unifies *control flow*, the
  domain operations stay on their existing commands.
