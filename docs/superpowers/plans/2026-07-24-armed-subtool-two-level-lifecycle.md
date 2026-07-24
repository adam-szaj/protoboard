# ArmedSubTool Two-Level Lifecycle + Wire-Edit Sub-Tool Extraction — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every armed/sticky tool a two-level lifecycle — commit/cancel act on the *current operation* and keep the tool armed; `[Esc]` is the only exit (two-stage) — and extract wire-edit from `SelectTool` into a `WireEditTool(ControllerTool)` under a formal `ArmedSubTool` protocol.

**Architecture:** Formalize the informal `ArmedSubTool` contract as a `Protocol` split into an inner operation-lifecycle (`commit_op`/`cancel_op`/`has_pending_op`/`can_commit`) and an outer tool-lifecycle (`on_key`/`can_exit`). One shared `[Esc]` handler in `ToolHost` does two-stage exit for all tools. `ArmedTransformTool`, the extracted `WireEditTool`, and `RouteTool` all conform (RouteTool partially — lifecycle only).

**Tech Stack:** Python 3.11+, PySide6 (Qt-free `controller/` layer), pytest + `HeadlessBoardView` fake, `mypy --strict`, `uv run pytest`.

## Global Constraints

- Domain/controller layers must not import Qt (`tests/unit/test_controller_no_gui_import.py` enforces `controller/` imports no `gui`).
- `mypy --strict` must pass on every touched file. No `Any` outside documented escape hatches.
- `uv run pytest` in this worktree needs `pythonpath=["src"]` (already set in `pyproject.toml`; memory `feedback_worktree_pytest_pythonpath`). Verify `protoboard.__file__` resolves under the worktree, not the main checkout.
- Conventional Commits (`feat:`/`refactor:`/`test:`/`docs:`). Commit per task.
- Never `git add -A`/`.` — use `git add -u` + explicit new-file adds; sanity-check `git diff --cached --name-only` (memory `feedback_no_git_add_all`).
- Never `gh pr create` (memory `feedback_never_create_pr`). Commit + push to `feat/item-id-handle-model` only.
- **Rewrites-and-swaps rule (CLAUDE.md):** behaviour (parity) tests are repointed to the replacement, never deleted alongside the old code. The parity net (Task 1) lands *before* any swap.
- SPDX header `# SPDX-License-Identifier: MIT` on every new file.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `src/protoboard/controller/tools/armed_subtool.py` (new) | The `ArmedSubTool` Protocol (formal contract). | 2 |
| `tests/gui/test_armed_subtool_lifecycle.py` (new) | Parity net: current commit/cancel/exit behaviour of all 3 tools + directive acceptance test. | 1 |
| `src/protoboard/gui/tool_host.py` | Two-stage `[Esc]`: consult `can_exit()` before `escape_to_select()`. | 3 |
| `src/protoboard/controller/tools/base.py` | Add default `can_exit()` on `ControllerTool` (returns True). | 3 |
| `src/protoboard/controller/tools/armed_transform_tool.py` | Decouple commit/cancel from exit; re-arm instead of `_return_to`; `can_exit`. | 4 |
| `src/protoboard/controller/tools/route_tool.py` | `commit_op`/`cancel_op`/`has_pending_op`/`can_exit`; `can_commit()` endpoint-on-pad gate. | 5, 6 |
| `src/protoboard/controller/editing/wire_edit_arm.py` (new) | Qt-free `WireEditArm` core (op-table state + builders). | 7 |
| `src/protoboard/controller/tools/wire_edit_tool.py` (new) | `WireEditTool(ControllerTool)` — op-table, stay-armed, two-stage Esc. | 8 |
| `src/protoboard/controller/tools/select_tool.py` | `arm_wire_edit` injects the `WireEditTool` factory; wire-edit op bodies + `_wire_edit_*` state removed after swap. | 8 |
| `docs/todo.md` | Track deferred wire-vs-wire same-side crossing conflict. | 6 |
| `docs/core-wire-semantics.md`, `docs/armed-rotate-and-subtool-delegation.md` | Doc reconciliation as gaps close. | 9 |

---

## Task 1: Parity net — characterize current lifecycle of all three tools

**Files:**
- Test: `tests/gui/test_armed_subtool_lifecycle.py` (create)

**Interfaces:**
- Consumes: `tests/fakes/board_view.py::HeadlessBoardView` (memory `project_headless_boardview_test_api`), pattern from `tests/gui/test_headless_tool_flows.py`.
- Produces: the golden assertions every later task must keep green. Task 4/5/8 *flip* specific "exits after op" assertions to "stays armed" — those flips are called out in this file with `# PARITY-FLIP(Task N)` comments.

- [ ] **Step 1: Read the existing headless flow tests to copy the fixture pattern**

Run: `sed -n '1,80p' tests/gui/test_headless_tool_flows.py`
Expected: see how `HeadlessBoardView` is constructed, how a wire/selection is set up, how keys are driven.

- [ ] **Step 2: Write the parity tests (they should PASS against current code)**

```python
# SPDX-License-Identifier: MIT
"""Parity net for the ArmedSubTool two-level-lifecycle refactor.

Captures the CURRENT (pre-refactor) commit/cancel/exit behaviour of
ArmedTransformTool, wire-edit (SelectTool sub-mode) and RouteTool, plus the
directive acceptance test. Tasks 4/5/8 intentionally flip the assertions
marked ``PARITY-FLIP`` from "exits after op" to "stays armed after op".
"""
from __future__ import annotations

import pytest

from tests.fakes.board_view import HeadlessBoardView  # adjust import to real path


class TestWholeWireVsFragmentSimplifyParity:
    """The directive's acceptance test: whole-wire simplify (select mode) and
    full-span fragment simplify (edit mode) must emit byte-identical commands.
    Guards the extraction in Task 8 (must still hold afterwards)."""

    def test_whole_wire_equals_full_span_fragment_simplify(self) -> None:
        from protoboard.controller.selection.wire_edit import (
            WireRange,
            build_simplify_range_command,
        )
        # Build an L-shaped wire (corner-cut is where simplify actually shortens;
        # a straight over pads does not — memory Stage-5 TRAP).
        # ... construct layout + wire via HeadlessBoardView helpers ...
        # whole = build_simplify_range_command(..., WireRange.full_span(wire), ...)
        # frag  = build_simplify_range_command(..., WireRange.full_span(wire), ...)
        # assert whole.serialize() == frag.serialize()  # byte-identical
        pytest.skip("fill in with real HeadlessBoardView wire construction")


class TestArmedTransformExitParity:
    def test_commit_currently_returns_to_select(self) -> None:
        # PARITY-FLIP(Task 4): after Task 4 this becomes "stays armed".
        # Arm rotate on a selection, step +90, confirm; assert active tool is
        # SelectTool today.
        pytest.skip("fill in")

    def test_cancel_currently_returns_to_select(self) -> None:
        # PARITY-FLIP(Task 4): becomes "stays armed" (still selected, re-armed).
        pytest.skip("fill in")

    def test_hard_conflict_parks_armed(self) -> None:
        # MUST stay true across the whole refactor: a HARD-conflict confirm
        # does NOT exit — it parks.
        pytest.skip("fill in")


class TestWireEditExitParity:
    @pytest.mark.parametrize("op_key", ["autoroute", "flip", "simplify", "delete"])
    def test_op_currently_drops_to_plain_select(self, op_key: str) -> None:
        # PARITY-FLIP(Task 8): after extraction each op stays in wire-edit.
        # Enter [E], pick a fragment, run the op; assert self._wire_edit is None
        # today (dropped to plain select).
        pytest.skip("fill in")

    def test_reroute_esc_keeps_fragment(self) -> None:
        # MUST stay true: Esc mid-reroute keeps the fragment selected
        # (route_tool-like two-stage already exists here — the model for the rest).
        pytest.skip("fill in")


class TestRouteToolExitParity:
    def test_commit_stays_in_routing(self) -> None:
        # MUST stay true: Enter commits a wire and RouteTool stays active with
        # empty _points.
        pytest.skip("fill in")

    def test_esc_midroute_currently_exits(self) -> None:
        # PARITY-FLIP(Task 3): after the host fix, first Esc clears points and
        # STAYS routing; only a second (empty) Esc exits.
        pytest.skip("fill in")
```

- [ ] **Step 3: Replace every `pytest.skip` with a real driven scenario**

Use `HeadlessBoardView` exactly as `test_headless_tool_flows.py` does: build a fixture project, add a wire, select it, drive keys via the tool-host key path. Assert on `view.tools.active_tool` type, `select_tool._wire_edit`, and command serialization (`cmd.serialize()` or the command dataclass equality already used in `test_wire_edit_helpers.py`).

- [ ] **Step 4: Run the parity tests — they must PASS against current code**

Run: `uv run pytest tests/gui/test_armed_subtool_lifecycle.py -v`
Expected: PASS (they characterize today's behaviour). The `PARITY-FLIP` tests pass now and will be edited in their named task.

- [ ] **Step 5: Commit**

```bash
git add tests/gui/test_armed_subtool_lifecycle.py
git commit -m "test(wire): parity net for armed-subtool two-level lifecycle"
```

---

## Task 2: Formalize the `ArmedSubTool` protocol

**Files:**
- Create: `src/protoboard/controller/tools/armed_subtool.py`
- Test: `tests/unit/test_armed_subtool_protocol.py` (create)

**Interfaces:**
- Produces: `class ArmedSubTool(Protocol)` with methods `commit_op() -> bool`, `cancel_op() -> bool`, `has_pending_op() -> bool`, `can_commit() -> bool`, `on_key(inp: KeyInput) -> bool`, `can_exit() -> bool`. Tasks 4/5/8 assert their tools are structurally compatible via `isinstance(tool, ArmedSubTool)` (runtime-checkable).

- [ ] **Step 1: Write the failing test**

```python
# SPDX-License-Identifier: MIT
from protoboard.controller.tools.armed_subtool import ArmedSubTool


class _Conforming:
    def commit_op(self) -> bool: return True
    def cancel_op(self) -> bool: return False
    def has_pending_op(self) -> bool: return False
    def can_commit(self) -> bool: return True
    def on_key(self, inp: object) -> bool: return False
    def can_exit(self) -> bool: return True


def test_conforming_object_is_armed_subtool() -> None:
    assert isinstance(_Conforming(), ArmedSubTool)


def test_missing_method_is_not_armed_subtool() -> None:
    class _Partial:
        def commit_op(self) -> bool: return True
    assert not isinstance(_Partial(), ArmedSubTool)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_armed_subtool_protocol.py -v`
Expected: FAIL with `ModuleNotFoundError: ... armed_subtool`

- [ ] **Step 3: Write the protocol**

```python
# SPDX-License-Identifier: MIT
"""The ``ArmedSubTool`` contract — a two-level tool lifecycle.

Inner (operation) lifecycle: ``commit_op`` / ``cancel_op`` act on the *current
operation* and keep the tool armed. Outer (tool) lifecycle: ``can_exit`` gates
the single ``[Esc]`` exit. See
``docs/superpowers/specs/2026-07-24-armed-subtool-two-level-lifecycle-design.md``.

Partial conformance is intentional: a tool with no rich op-table returns
``False`` from ``on_key`` for verbs it does not own; a tool with no conflict
concept may return a constant ``True`` from ``can_commit``.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from protoboard.controller.ports import KeyInput


@runtime_checkable
class ArmedSubTool(Protocol):
    # --- operation lifecycle (inner) ---
    def commit_op(self) -> bool:
        """Commit the current operation. Return True if committed, False if it
        parked (HARD conflict). Either way the tool STAYS armed."""
        ...

    def cancel_op(self) -> bool:
        """Revert the pending operation. Return True if something was pending."""
        ...

    def has_pending_op(self) -> bool:
        """Is there an in-flight operation to commit/cancel?"""
        ...

    def can_commit(self) -> bool:
        """False ⇒ a commit would land in a HARD conflict ⇒ park (stay armed)."""
        ...

    # --- tool lifecycle (outer) ---
    def on_key(self, inp: KeyInput) -> bool:
        """Op-table dispatch. Return False for verbs this tool does not own."""
        ...

    def can_exit(self) -> bool:
        """May ``[Esc]`` exit the tool now? Usually ``not has_pending_op()``."""
        ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_armed_subtool_protocol.py -v`
Expected: PASS

- [ ] **Step 5: mypy + commit**

```bash
uv run mypy --strict src/protoboard/controller/tools/armed_subtool.py
git add src/protoboard/controller/tools/armed_subtool.py tests/unit/test_armed_subtool_protocol.py
git commit -m "feat(wire): formalize ArmedSubTool two-level-lifecycle protocol"
```

---

## Task 3: Two-stage `[Esc]` in ToolHost (the host-level fix)

**Files:**
- Modify: `src/protoboard/controller/tools/base.py` (add `can_exit` default)
- Modify: `src/protoboard/gui/tool_host.py:534-545` (Esc branch)
- Test: extend `tests/gui/test_armed_subtool_lifecycle.py`

**Interfaces:**
- Consumes: `ControllerTool.can_exit()` (new default).
- Produces: the two-stage Esc semantics every tool relies on: `[Esc]` calls `tool.on_key(inp)`; `escape_to_select()` fires **only** when `tool.can_exit()` is True (or no tool).

- [ ] **Step 1: Add the failing test (RouteTool two-stage Esc)**

Edit `TestRouteToolExitParity.test_esc_midroute_currently_exits` → rename to `test_esc_is_two_stage`, assert: with points placed, first Esc leaves `active_tool` a `RouteTool` with empty `_points`; a second Esc exits to SelectTool.

```python
def test_esc_is_two_stage(self) -> None:
    view = ...  # RouteTool active, two points placed
    route = view.tools.active_tool
    view.press_key_escape()          # host Esc path
    assert isinstance(view.tools.active_tool, RouteTool)   # STAYS
    assert route._points == []                              # pending cleared
    view.press_key_escape()          # nothing pending
    assert view.tools.active_tool is view._select_tool      # EXITS
```

- [ ] **Step 2: Run — verify it fails**

Run: `uv run pytest tests/gui/test_armed_subtool_lifecycle.py::TestRouteToolExitParity::test_esc_is_two_stage -v`
Expected: FAIL — today the first Esc already exits (host calls `escape_to_select()` unconditionally).

- [ ] **Step 3: Add `can_exit` default to `ControllerTool`**

In `src/protoboard/controller/tools/base.py`, add to `ControllerTool`:

```python
def can_exit(self) -> bool:
    """May ``[Esc]`` exit this tool now? Tools with a pending operation
    override to return False so the first Esc cancels the op (stay active)
    and only a later Esc exits. Default: always exitable."""
    return True
```

- [ ] **Step 4: Fix the ToolHost Esc branch**

In `src/protoboard/gui/tool_host.py`, replace lines 539-545:

```python
            consumed = False
            if self._active_tool is not None:
                consumed = self._active_tool.on_key(inp)
            view.escape_to_select()
            if consumed:
                event.accept()
            return
```

with:

```python
            consumed = False
            tool = self._active_tool
            if tool is not None:
                consumed = tool.on_key(inp)
            # Two-stage Esc: the tool's on_key cancels a pending operation and
            # stays active; only exit to select when the tool reports it has
            # nothing pending (can_exit). A tool with no pending op returns True
            # → unchanged single-press exit for idle tools.
            if tool is None or tool.can_exit():
                view.escape_to_select()
            if consumed:
                event.accept()
            return
```

- [ ] **Step 5: RouteTool must report pending (temporary shim until Task 5)**

So Task 3 is independently green, add `has_pending_op`/`can_exit` to `RouteTool` now (Task 5 builds the rest of the ArmedSubTool surface on top):

In `src/protoboard/controller/tools/route_tool.py`, add:

```python
def has_pending_op(self) -> bool:
    return len(self._points) > 0

def can_exit(self) -> bool:
    return not self.has_pending_op()
```

And make `_key_cancel` cancel-the-op only (it already calls `_cancel`, which clears points and stays — no tool switch). No change needed to `_cancel`.

- [ ] **Step 6: Run the test — verify it passes**

Run: `uv run pytest tests/gui/test_armed_subtool_lifecycle.py::TestRouteToolExitParity -v`
Expected: PASS (both `test_commit_stays_in_routing` and `test_esc_is_two_stage`).

- [ ] **Step 7: Full regression + mypy**

Run: `uv run pytest tests/gui/test_headless_tool_flows.py tests/gui/test_armed_subtool_lifecycle.py -q && uv run mypy --strict src/protoboard/controller/tools/route_tool.py src/protoboard/controller/tools/base.py`
Expected: PASS. (Watch for other tools relying on the old unconditional Esc-exit — if any test regresses, that tool now needs `can_exit` returning True, which is the default, so it should not.)

- [ ] **Step 8: Commit**

```bash
git add src/protoboard/gui/tool_host.py src/protoboard/controller/tools/base.py src/protoboard/controller/tools/route_tool.py tests/gui/test_armed_subtool_lifecycle.py
git commit -m "fix(route): two-stage Esc — cancel pending op stays in tool, empty Esc exits"
```

---

## Task 4: ArmedTransformTool — commit/cancel stay armed

**Files:**
- Modify: `src/protoboard/controller/tools/armed_transform_tool.py:163-175, 216-250`
- Test: `tests/gui/test_armed_subtool_lifecycle.py::TestArmedTransformExitParity`

**Interfaces:**
- Consumes: `EditController.begin_transform` (re-arm), `controller.armed_transform` (terminal check).
- Produces: `commit_op()`/`cancel_op()`/`has_pending_op()`/`can_commit()`/`can_exit()` on `ArmedTransformTool`. `[Enter]` commits and re-arms; HARD conflict parks; `[Esc]` when clean exits via host.

- [ ] **Step 1: Flip the parity assertions**

Edit `TestArmedTransformExitParity`: `test_commit_currently_returns_to_select` → `test_commit_stays_armed` (after confirm, `active_tool` is still the `ArmedTransformTool`, re-armed on the moved selection; `_armed is not None and not _armed.finished`). Same for cancel. Keep `test_hard_conflict_parks_armed` unchanged.

- [ ] **Step 2: Run — verify it fails**

Run: `uv run pytest tests/gui/test_armed_subtool_lifecycle.py::TestArmedTransformExitParity -v`
Expected: FAIL — today confirm calls `_after_verb → _return_to`.

- [ ] **Step 3: Add the ArmedSubTool surface + re-arm**

In `armed_transform_tool.py` add:

```python
def has_pending_op(self) -> bool:
    return self._is_armed()

def can_commit(self) -> bool:
    armed = self._armed
    return armed is None or armed.can_commit()

def can_exit(self) -> bool:
    # Exit only when nothing is armed/pending; a live transform is a pending op.
    return not self._is_armed()

def commit_op(self) -> bool:
    controller = self._controller
    if controller is None or self._armed is None:
        return False
    committable = self._armed.can_commit()
    controller.accept_transform()
    self._rearm_or_park()
    return committable

def cancel_op(self) -> bool:
    controller = self._controller
    if controller is None or self._armed is None:
        return False
    controller.cancel_transform()
    self._rearm_or_park()
    return True
```

- [ ] **Step 4: Add `_rearm_or_park` (replaces `_after_verb`'s exit for commit/cancel)**

```python
def _rearm_or_park(self) -> None:
    """After a commit/cancel verb: if the transform terminated (committed or
    cancelled cleanly), RE-ARM on the current selection so the tool stays
    active for the next operation; if it parked (HARD conflict), leave it live.
    Only the host [Esc]-when-clean path exits to SelectTool."""
    controller = self._controller
    if controller is None:
        return
    if controller.armed_transform is None:
        # Terminal — re-arm on the (possibly moved) selection instead of
        # returning to SelectTool.
        self._armed = controller.begin_transform(*self._begin_transform_args())
        view = self._view
        if view is not None and self._armed is not None:
            self._status(self._armed.status_hint())
    # else: parked in conflict — stay live, surface already shown.
```

- [ ] **Step 5: Rewire `_cmd_confirm`/`_cmd_cancel` to the op verbs**

Replace `_cmd_confirm` body (`:163-168`) with `return self.commit_op()` and `_cmd_cancel` (`:170-175`) with `return self.cancel_op()`. Leave `_cmd_flip`/`_cmd_rotate`/`_cmd_move`/`_make_step_cmd` on `_after_verb` (they are *within* an operation — stepping, not committing — so they must NOT re-arm; `_after_verb` for a non-terminal step is a no-op today and stays correct).

**Capture `_begin_transform_args`:** in `activate` (`:105`), the args passed to `begin_transform` (selection ids, mode, factories) must be stashed as `self._begin_transform_args` (a zero-arg closure returning them) so re-arm reuses the same entry. Add that stash in `activate`.

- [ ] **Step 6: Run — verify it passes**

Run: `uv run pytest tests/gui/test_armed_subtool_lifecycle.py::TestArmedTransformExitParity -v`
Expected: PASS — commit/cancel stay armed; HARD parks.

- [ ] **Step 7: Regression + mypy**

Run: `uv run pytest tests/gui/ -k "transform or armed" -q && uv run mypy --strict src/protoboard/controller/tools/armed_transform_tool.py`
Expected: PASS. If any existing transform test asserted "returns to select after confirm", that is an intended behaviour change — repoint it to "stays armed" (do not delete).

- [ ] **Step 8: Commit**

```bash
git add src/protoboard/controller/tools/armed_transform_tool.py tests/gui/test_armed_subtool_lifecycle.py
git commit -m "refactor(transform): commit/cancel stay armed; Esc-when-clean exits"
```

---

## Task 5: RouteTool — full ArmedSubTool surface

**Files:**
- Modify: `src/protoboard/controller/tools/route_tool.py`
- Test: `tests/gui/test_armed_subtool_lifecycle.py::TestRouteToolExitParity`

**Interfaces:**
- Consumes: existing `_commit`/`_cancel`/`_points`.
- Produces: `commit_op`/`cancel_op` on RouteTool (aliasing the existing `_commit`/`_cancel`), completing the ArmedSubTool surface begun in Task 3. `can_commit()` still returns True here (conflict gate is Task 6).

- [ ] **Step 1: Failing test — RouteTool is an ArmedSubTool**

```python
def test_route_tool_conforms_to_armed_subtool() -> None:
    from protoboard.controller.tools.armed_subtool import ArmedSubTool
    from protoboard.controller.tools.route_tool import RouteTool
    assert isinstance(RouteTool(), ArmedSubTool)
```

- [ ] **Step 2: Run — verify it fails**

Run: `uv run pytest tests/gui/test_armed_subtool_lifecycle.py::TestRouteToolExitParity::test_route_tool_conforms_to_armed_subtool -v`
Expected: FAIL — `commit_op`/`cancel_op`/`can_commit` missing.

- [ ] **Step 3: Add the surface**

```python
def commit_op(self) -> bool:
    committable = self.can_commit()
    self._commit()   # already stays in routing
    return committable

def cancel_op(self) -> bool:
    had = self.has_pending_op()
    self._cancel()   # clears points, stays routing
    return had

def can_commit(self) -> bool:
    # Conflict gate added in Task 6; no geometric conflict concept yet.
    return True
```

(`has_pending_op`/`can_exit` were added in Task 3.)

- [ ] **Step 4: Run — verify it passes**

Run: `uv run pytest tests/gui/test_armed_subtool_lifecycle.py::TestRouteToolExitParity -v`
Expected: PASS.

- [ ] **Step 5: mypy + commit**

```bash
uv run mypy --strict src/protoboard/controller/tools/route_tool.py
git add src/protoboard/controller/tools/route_tool.py tests/gui/test_armed_subtool_lifecycle.py
git commit -m "refactor(route): RouteTool conforms to ArmedSubTool (lifecycle)"
```

---

## Task 6: RouteTool `can_commit()` — endpoint-on-alien-pad conflict gate

**Files:**
- Modify: `src/protoboard/controller/tools/route_tool.py` (`can_commit`, `_commit`)
- Modify: `docs/todo.md` (defer wire-vs-wire same-side crossing)
- Test: `tests/gui/test_armed_subtool_lifecycle.py` (new class `TestRouteConflictGate`)

**Interfaces:**
- Consumes: `find_wires_crossing_pads` (`src/protoboard/domain/layout/pad_conflict.py:507`), `DragCommitDecision` + `DragSeverity` (`controller/tools/drag_commit_decision.py:37,42`), the overlay port `TransformOverlaysPort.show_conflict` (`controller/tools/transform_overlays.py:22`).
- Produces: a route whose candidate path lands an endpoint/segment on a different-net pad → `can_commit()` False → `commit_op` refuses + shows the red corner + stays armed.

- [ ] **Step 1: Failing test — alien-pad route refuses commit**

```python
class TestRouteConflictGate:
    def test_route_ending_on_alien_net_pad_refuses(self) -> None:
        # Build a board with a pad on net A. Route a wire (net B / unconnected)
        # whose endpoint lands on that pad. commit_op() must return False and
        # the wire must NOT be added; RouteTool stays active with points intact.
        view = ...  # RouteTool active
        # place points ending on the alien pad
        route = view.tools.active_tool
        assert route.can_commit() is False
        added_before = len(view.session.layout.wires)
        assert route.commit_op() is False
        assert len(view.session.layout.wires) == added_before   # not committed
        assert route.has_pending_op()                            # stays armed

    def test_clean_route_commits(self) -> None:
        # A route on empty holes commits (can_commit True).
        ...
```

- [ ] **Step 2: Run — verify it fails**

Run: `uv run pytest tests/gui/test_armed_subtool_lifecycle.py::TestRouteConflictGate -v`
Expected: FAIL — `can_commit` currently returns True unconditionally.

- [ ] **Step 3: Implement `_route_conflict()` via the temp-Wire adapter**

Add to `route_tool.py`:

```python
def _route_conflict(self) -> "DragCommitDecision | None":
    """Classify the candidate route (``self._points``) for a HARD conflict.

    Scoped to endpoint/segment-on-alien-pad this round (wire-vs-wire same-side
    crossing is deferred — see docs/todo.md). Wraps the candidate points as a
    temp ``Wire`` (this tool's side + insulation) and reuses
    ``find_wires_crossing_pads``'s pad/net geometry; a different-net pad hit maps
    to a HARD ``DragCommitDecision`` driving the shared amber/red corner + park.
    """
    from protoboard.controller.tools.drag_commit_decision import (
        DragCommitDecision, DragSeverity,
    )
    from protoboard.domain.layout.pad_conflict import find_wires_crossing_pads
    from protoboard.domain.layout.wire import Wire

    if len(self._points) < 2:
        return None
    candidate = Wire(path=list(self._points), side=self._side,
                     insulated=self._insulated)  # temp; not added
    hits = find_wires_crossing_pads(
        wires=[candidate],
        moved_comp_ids=frozenset(),
        layout_placements=self._session.layout.placements,
        instances=self._session.instances,
        components=self._session.components,
        schematic=self._session.schematic,
    )
    if not hits:
        return None
    return DragCommitDecision(
        severity=DragSeverity.HARD,
        description="Wire endpoint lands on a pad of another net",
    )
```

(Confirm `find_wires_crossing_pads`'s exact kwargs against `pad_conflict.py:507` — the plan's signature mirrors the explorer's report; adapt names if the temp-`Wire` needs a synthetic id.)

- [ ] **Step 4: Wire `can_commit` + `commit_op` to it**

```python
def can_commit(self) -> bool:
    return self._route_conflict() is None

def commit_op(self) -> bool:
    decision = self._route_conflict()
    if decision is not None and decision.is_hard:
        self._show_route_conflict(decision)   # reuse overlay port
        return False                          # park — stay armed
    self._commit()
    return True
```

Add `_show_route_conflict` delegating to the tool's overlay seam (`self._view` transform-overlays port `show_conflict`, mirroring `select_tool._show_conflict_corner`). If RouteTool lacks an overlays port, surface via `notify_status` with a red/high-contrast hint (memory `user_color_vision_accessibility` — not colour-only).

- [ ] **Step 5: Run — verify it passes**

Run: `uv run pytest tests/gui/test_armed_subtool_lifecycle.py::TestRouteConflictGate -v`
Expected: PASS.

- [ ] **Step 6: Record the deferral**

Append to `docs/todo.md`:

```markdown
- **Wire-vs-wire same-side crossing conflict (RouteTool `can_commit`)** —
  deferred 2026-07-24. RouteTool's commit gate currently detects only
  endpoint/segment-on-alien-pad (via `find_wires_crossing_pads`). A general
  wire-vs-wire same-side crossing/overlap detector does not exist and is out of
  scope for the ArmedSubTool lifecycle round. See
  `docs/superpowers/specs/2026-07-24-armed-subtool-two-level-lifecycle-design.md` §4.3.
```

- [ ] **Step 7: mypy + commit**

```bash
uv run mypy --strict src/protoboard/controller/tools/route_tool.py
git add src/protoboard/controller/tools/route_tool.py docs/todo.md tests/gui/test_armed_subtool_lifecycle.py
git commit -m "feat(route): block commit when a route endpoint lands on an alien-net pad"
```

---

## Task 7: Qt-free `WireEditArm` core

**Files:**
- Create: `src/protoboard/controller/editing/wire_edit_arm.py`
- Test: `tests/unit/test_wire_edit_arm.py` (create)

**Interfaces:**
- Consumes: `WireRange`/`WireEditState` + the range builders in `controller/selection/wire_edit.py` (`build_reroute_command`, `build_delete_command`, `build_simplify_range_command`, `autoroute_fragment_path`, `build_flip_fragment_command`), `_net_aware_add_factory` semantics (move the factory into a session-taking free function).
- Produces: `class WireEditArm` holding the fragment `WireRange` + `WireEditCycle`, with methods `reroute(...)`, `autoroute()`, `flip()`, `simplify()`, `delete()`, each returning a `Command | None` (NOT executing — the tool executes + refreshes). Session-free by contract (takes layout/wires/net-map as args), matching `wire_edit.py`.

- [ ] **Step 1: Write failing tests for each op building a command**

```python
# SPDX-License-Identifier: MIT
def test_simplify_full_span_equals_whole_wire() -> None:
    # WireEditArm.simplify() on a full-span range == the whole-wire builder
    # output (the directive acceptance test, unit-level).
    ...

def test_delete_full_span_collapses_to_remove_wire() -> None:
    ...

def test_flip_builds_flip_fragment_command() -> None:
    ...
```

- [ ] **Step 2: Run — verify they fail**

Run: `uv run pytest tests/unit/test_wire_edit_arm.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Extract the core**

Create `wire_edit_arm.py` moving the *command-building* halves of `_commit_reroute`, `_autoroute_fragment`, `_flip_wire_fragment`, `_simplify_wire_fragment`, `_delete_fragment` (select_tool.py) into `WireEditArm` methods. Each returns a `Command | None`; none call `_wire_edit_clear` or `_execute_command` (the tool owns commit + refresh). Keep the net-aware `add_factory` as an injected callable (default plain `AddWireCommand`, per shipped Stage 2). No Qt imports.

- [ ] **Step 4: Run — verify they pass**

Run: `uv run pytest tests/unit/test_wire_edit_arm.py -v`
Expected: PASS.

- [ ] **Step 5: mypy + controller-purity guard + commit**

```bash
uv run mypy --strict src/protoboard/controller/editing/wire_edit_arm.py
uv run pytest tests/unit/test_controller_no_gui_import.py -q
git add src/protoboard/controller/editing/wire_edit_arm.py tests/unit/test_wire_edit_arm.py
git commit -m "refactor(wire): extract Qt-free WireEditArm command-building core"
```

---

## Task 8: `WireEditTool` — extract wire-edit from SelectTool

**Files:**
- Create: `src/protoboard/controller/tools/wire_edit_tool.py`
- Modify: `src/protoboard/controller/tools/select_tool.py` (`arm_wire_edit:2104` injects the factory; remove wire-edit op bodies + `_wire_edit*` state + `_wire_edit_on_key`)
- Modify: `src/protoboard/gui/tools/gui_tools.py` (Qt-composed `WireEditTool` if a `ViewToolMixin` composition is needed, mirroring `RouteTool`/`ArmedTransformTool`)
- Test: `tests/gui/test_armed_subtool_lifecycle.py::TestWireEditExitParity` (flip assertions) + `tests/gui/test_headless_tool_flows.py`

**Interfaces:**
- Consumes: `WireEditArm` (Task 7), `ArmedSubTool` (Task 2), the pick machinery (`_arm_pick`) and overlays currently on SelectTool — these move with the tool or are accessed via the view seam.
- Produces: `WireEditTool(ControllerTool)` with op-table `[W]/[A]/[F]/[S]/[Del]`, each **commit-and-stay-armed**; `[Esc]` two-stage (cancel pending reroute pick → else exit); `has_pending_op` True while a reroute waypoint pick is live.

- [ ] **Step 1: Flip the wire-edit parity assertions**

Edit `TestWireEditExitParity.test_op_currently_drops_to_plain_select` → `test_op_stays_in_wire_edit`: after `[A]/[F]/[S]/[Del]` the tool is still `WireEditTool` with the fragment re-armed (a fresh full/again-selectable fragment), NOT dropped to plain select. Keep `test_reroute_esc_keeps_fragment`. Add: after two consecutive `[S]` then `[F]` without re-arming, both commands executed (the stay-armed proof).

- [ ] **Step 2: Run — verify it fails**

Run: `uv run pytest tests/gui/test_armed_subtool_lifecycle.py::TestWireEditExitParity -v`
Expected: FAIL — today each op calls `_wire_edit_clear()`.

- [ ] **Step 3: Build `WireEditTool` mirroring `ArmedTransformTool`**

Create `wire_edit_tool.py`: `activate` builds a `WireEditArm` from the session + the picked `WireRange`; `command_table` maps `_ACT_REROUTE/_ACT_AUTOROUTE/_ACT_FLIP_FRAGMENT/_ACT_SIMPLIFY_FRAGMENT/_ACT_DELETE/_ACT_CANCEL` to methods that: build the command via `WireEditArm`, `commit_op` executes + `_refresh_after_wire_op` + **re-arm the fragment** (do NOT clear/exit). Reroute keeps its nested waypoint pick (the escape hatch §5.3): `has_pending_op()` True while the pick is live; `cancel_op` drops the waypoints and keeps the fragment (today's `_reroute_cancel`). `can_exit()` = `not has_pending_op()`. Preserve net-restricted hit-test, cyan/red overlays, `WireEditCycle` cluster cycling by moving that code with the tool.

- [ ] **Step 4: Repoint `arm_wire_edit` to inject the factory**

In `select_tool.py:2104` `arm_wire_edit`, after the two-vertex pick resolves (`_range_picked:2188`), instead of setting `self._wire_edit` and staying in SelectTool, `set_tool(self._wire_edit_tool_factory(range, return_to=lambda: set_tool(self)))` — mirroring `_arm_transform:2756-2764`. Remove `_wire_edit_on_key`, the op bodies (`_begin_reroute`.._delete_fragment`), and `_wire_edit*` state fields **only after** the new tool passes the flipped parity tests.

- [ ] **Step 5: Run flipped parity + directive acceptance + full flows**

Run: `uv run pytest tests/gui/test_armed_subtool_lifecycle.py tests/gui/test_headless_tool_flows.py tests/unit/test_wire_edit_helpers.py tests/gui/test_select_tool_parity.py -q`
Expected: PASS. The directive test (`TestWholeWireVsFragmentSimplifyParity`) must still pass — extraction preserved byte-identical commands.

- [ ] **Step 6: mypy + purity guard**

Run: `uv run mypy --strict src/protoboard/controller/tools/wire_edit_tool.py src/protoboard/controller/tools/select_tool.py && uv run pytest tests/unit/test_controller_no_gui_import.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/protoboard/controller/tools/wire_edit_tool.py src/protoboard/controller/tools/select_tool.py src/protoboard/gui/tools/gui_tools.py tests/gui/test_armed_subtool_lifecycle.py
git commit -m "refactor(wire): extract WireEditTool — ops stay armed, two-stage Esc"
```

---

## Task 9: Docs reconciliation + full-suite smoke

**Files:**
- Modify: `docs/core-wire-semantics.md`, `docs/armed-rotate-and-subtool-delegation.md`, `CLAUDE.md` (source-layout note for the two new tool files)

- [ ] **Step 1: Update `armed-rotate-and-subtool-delegation.md`**

Mark §2.1 A (wire-edit sub-tool) and §5.3 as **DONE**: wire-edit is now `WireEditTool` under the `ArmedSubTool` protocol; record the two-level lifecycle (commit/cancel stay armed; two-stage Esc) as the contract change that made the fold-in fit.

- [ ] **Step 2: Update `core-wire-semantics.md`**

Note that whole-wire and full-span-fragment ops now run through `WireEditArm` (control-flow unified on top of the shipped data-level `WireRange`).

- [ ] **Step 3: Update `CLAUDE.md` source layout**

Add `wire_edit_tool.py` / `armed_subtool.py` under `controller/tools/` and `wire_edit_arm.py` under `controller/editing/` in the §5 tree.

- [ ] **Step 4: Full suite**

Run: `uv run pytest -q`
Expected: PASS except the known pre-existing `test_debug_trace::test_logs_args_and_return` failure (memory `project_wire_range_unification_2026_07_24` — unrelated stash on main). Confirm no *new* failures.

- [ ] **Step 5: Real-GUI smoke**

Run the `run-protoboard-designer` skill: enter `[E]`, pick a fragment, run `[S]` then `[F]` without re-arming (stay-armed proof); two-stage `[Esc]` exits. Manual route: `[Enter]` commits and stays; first `[Esc]` clears pending, second exits; a route ending on an alien-net pad refuses with the red corner.

- [ ] **Step 6: Commit + push**

```bash
git add docs/core-wire-semantics.md docs/armed-rotate-and-subtool-delegation.md CLAUDE.md
git commit -m "docs(wire): reconcile armed-subtool + wire-edit-tool extraction"
git push origin feat/item-id-handle-model
```

---

## Self-Review notes

- **Spec §2 (two-level lifecycle):** Tasks 3 (host), 4 (transform), 8 (wire-edit) — covered.
- **Spec §3 (protocol):** Task 2 — covered.
- **Spec §3.1 (park):** Task 4 keeps `test_hard_conflict_parks_armed`; Task 6 adds RouteTool park — covered.
- **Spec §4.1/4.2/4.3:** Tasks 4 / 7+8 / 3+5+6 — covered.
- **Spec §5 (parity net):** Task 1 lands first; behaviour tests repointed not deleted (Tasks 4/8 flip, not remove) — covered.
- **Spec §4.3 deferral:** Task 6 Step 6 records it in `docs/todo.md` — covered.
- **Directive acceptance test:** Task 1 + re-checked Task 8 Step 5 — covered.
- **Type consistency:** `commit_op`/`cancel_op`/`has_pending_op`/`can_commit`/`on_key`/`can_exit` used identically across Tasks 2–8. `_route_conflict`/`_rearm_or_park`/`_begin_transform_args` are task-local helpers, defined where used.
