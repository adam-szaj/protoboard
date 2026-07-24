# Set-insulated on a Wire Sub-range — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `[I]` toggle a *sub-range* of a wire between bare and insulated by physically splitting the wire into bare/insulated/bare segments, and unify delete + set-insulated so that after any cut the tool waits for a fresh 2-vertex re-pick instead of exiting.

**Architecture:** A new pure builder `build_set_insulated_range_command` in `controller/selection/wire_edit.py` mints up-to-three wires (left survivor / insulated target / right survivor) through the net-aware `add_factory`, mirroring `build_delete_command`'s split. A small frozen `WireRangeEdit` result surfaces `was_cut` + minted `survivor_wire_ids` so the tool can choose its post-op behavior. `WireEditTool` gains an `_awaiting_repick` state: after a sub-range cut it disarms the fragment and reuses the existing generic `_arm_pick("wire-vertex", …)` to wait for a new range. Delete is ported onto the same result via a parallel `build_delete_range_edit` / `arm.delete_range` (the existing `build_delete_command` / `arm.delete` stay intact for their current callers).

**Tech Stack:** Python 3.11+, PySide6 (GUI seam only), pytest + pytest-qt, `ruamel.yaml` (unaffected), mypy --strict.

## Global Constraints

- `mypy --strict` must pass; no `Any` outside documented escape hatches.
- SPDX header `# SPDX-License-Identifier: MIT` on every new file.
- Domain/controller layers stay Qt-free (`controller/` imports no `protoboard.gui`; enforced by `tests/unit/test_controller_no_gui_import.py`).
- `frozen=True` dataclasses where practical.
- Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`).
- Every UI action has an action-id + keybinding (`memory/feedback_every_action_has_keybinding`).
- Never `git add -A`/`.`; stage with `git add -u` + explicit new-file adds; sanity-check `git diff --cached --name-only`. Do NOT stage the pre-existing untracked `example.py` / `library/ICs/audio.yaml`.
- TDD: failing test first, always.
- Cross-wire set-insulated is OUT OF SCOPE (refused with a status hint; deferred to `docs/todo.md`).

## File Structure

- `src/protoboard/controller/selection/wire_edit.py` — **modify**: add `WireRangeEdit` dataclass, `build_set_insulated_range_command`, `build_delete_range_edit`; extend `__all__`.
- `src/protoboard/controller/editing/wire_edit_arm.py` — **modify**: add `arm.set_insulated(...)` and `arm.delete_range(...)`, both returning `WireRangeEdit`.
- `src/protoboard/controller/tools/wire_edit_tool.py` — **modify**: add `_awaiting_repick` field, `_ACT_SET_INSULATED_FRAGMENT`, `command_table`/`displayable_action_ids` entries, `_set_insulated_fragment`, `_arm_repick`, `_range_repicked`, and repoint `_delete_fragment` onto `arm.delete_range` + the shared decision helper.
- `src/protoboard/gui/tools/gui_tools.py` — **modify**: `key_actions` (`"I"`), `key_claims` (extended `armed`).
- `tests/unit/test_wire_edit_helpers.py` — **modify**: builder tests for set-insulated + `build_delete_range_edit`.
- `tests/unit/test_wire_edit_set_insulated_tool.py` — **create**: headless tool-state tests (wait-for-re-pick, delete parity, keybinding claim).
- `docs/core-wire-semantics.md` — **modify**: §6.2 / GAP row / recommendation.
- `docs/todo.md` — **modify**: cross-wire deferral line.
- `CHANGELOG.md` — **modify**: feature entry.

---

### Task 0: CHANGELOG entry

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add the entry**

Under the top `## [Unreleased]` (or equivalent latest) section's "Added":

```markdown
- Wire fragment set-insulated: `[I]` toggles a sub-range of a wire between bare
  and insulated by splitting it into bare/insulated/bare segments. After a
  cut, wire-edit waits for a fresh range pick (delete unified onto the same
  flow).
```

- [ ] **Step 2: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: changelog entry for wire fragment set-insulated"
```

---

### Task 1: `WireRangeEdit` result type + `build_set_insulated_range_command` (builder)

**Files:**
- Modify: `src/protoboard/controller/selection/wire_edit.py` (add near `build_delete_command`, `:446`; extend `__all__` at `:77`)
- Test: `tests/unit/test_wire_edit_helpers.py` (beside the delete goldens, imports at `:34`)

**Interfaces:**
- Consumes: `Wire` (fields `id, path, insulated, side, color`), `WireEditState`/`WireRange` (attrs `start_wire_id, end_wire_id, start_idx, end_idx, is_cross`, classmethod `full_span(wire)`), `AddWireFactory = Callable[[Wire], Command[ComponentState]]`, `_default_add_factory`, `_find_wire`, `RemoveWireCommand`, `MacroCommand`, `SetWireInsulatedCommand`.
- Produces:
  - `@dataclass(frozen=True) class WireRangeEdit` with fields `command: Command[ComponentState]`, `survivor_wire_ids: tuple[uuid.UUID, ...]`, `was_cut: bool`.
  - `build_set_insulated_range_command(wires: Sequence[Wire], wire_net_map: dict[uuid.UUID, uuid.UUID], state: WireEditState, insulated: bool, add_factory: AddWireFactory = _default_add_factory) -> WireRangeEdit | None`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_wire_edit_helpers.py`. Reuse the file's existing wire/`WireRange`/`nm` fixture idioms (see the delete goldens around `:340-370` for the exact `Wire(...)` + `WireRange` construction used in this file; mirror them). A straight 5-point same-side wire on `side="top"`, `insulated=False`:

```python
class TestBuildSetInsulatedRangeCommand:
    def _wire(self):
        # 5 collinear points, one fragment (same side), bare.
        return Wire(
            id=uuid.uuid4(),
            path=[Point(0, 0), Point(1, 0), Point(2, 0), Point(3, 0), Point(4, 0)],
            insulated=False,
            side="top",
            color=None,
        )

    def test_full_span_returns_set_insulated_command_id_preserving(self):
        wire = self._wire()
        nm = {wire.id: uuid.uuid4()}
        state = WireRange.full_span(wire)
        result = build_set_insulated_range_command([wire], nm, state, insulated=True)
        assert result is not None
        assert result.was_cut is False
        assert result.survivor_wire_ids == ()
        assert isinstance(result.command, SetWireInsulatedCommand)
        assert result.command.wire_id == wire.id
        assert result.command.insulated is True

    def test_sub_range_splits_into_three_middle_insulated(self):
        wire = self._wire()
        nm = {wire.id: uuid.uuid4()}
        state = WireRange(wire.id, 1, wire.id, 3)  # middle slice idx 1..3
        result = build_set_insulated_range_command([wire], nm, state, insulated=True)
        assert result is not None
        assert result.was_cut is True
        # Two fresh survivors (left + right); target is also fresh but the
        # survivors are what the tool re-arms against.
        assert len(result.survivor_wire_ids) == 2
        # Execute against a layout and inspect the three wires.
        wires = _execute_on_layout(result.command, [wire])
        by_len = sorted(wires, key=lambda w: len(w.path))
        # left = path[:2] (2 pts), right = path[3:] (2 pts), target = path[1:4] (3 pts)
        assert len(wires) == 3
        target = max(wires, key=lambda w: len(w.path))
        others = [w for w in wires if w is not target]
        assert target.insulated is True
        assert all(w.insulated is False for w in others)
        assert all(w.side == "top" for w in wires)

    def test_sub_range_touching_end_yields_two_pieces(self):
        wire = self._wire()
        nm = {wire.id: uuid.uuid4()}
        state = WireRange(wire.id, 0, wire.id, 2)  # slice at the left end
        result = build_set_insulated_range_command([wire], nm, state, insulated=True)
        assert result is not None
        assert result.was_cut is True
        wires = _execute_on_layout(result.command, [wire])
        assert len(wires) == 2  # insulated target (idx 0..2) + bare right (idx 2..4)

    def test_noop_when_slice_already_at_target_returns_none(self):
        wire = self._wire()  # bare
        nm = {wire.id: uuid.uuid4()}
        state = WireRange(wire.id, 1, wire.id, 3)
        result = build_set_insulated_range_command([wire], nm, state, insulated=False)
        assert result is None  # already bare, no change

    def test_cross_wire_is_refused(self):
        w1 = self._wire()
        w2 = Wire(id=uuid.uuid4(), path=[Point(4, 0), Point(5, 0)],
                  insulated=False, side="top", color=None)
        nm = {w1.id: uuid.uuid4(), w2.id: uuid.uuid4()}
        state = WireRange(w1.id, 2, w2.id, 1)  # cross-wire
        assert state.is_cross
        result = build_set_insulated_range_command([w1, w2], nm, state, insulated=True)
        assert result is None
```

Add a small helper at the top of the test module (only if the file has no equivalent; check first — the delete goldens likely already execute commands). It applies the command to a throwaway `ComponentState`/layout seeded with `initial` wires and returns the resulting wire list:

```python
def _execute_on_layout(cmd, initial):
    from protoboard.controller.editing.headless_session import HeadlessSession
    session = HeadlessSession.blank()
    for w in initial:
        session.layout.add_wire(w)  # seed
    cmd.apply(session.component_state)  # adapt to this file's existing apply idiom
    return list(session.layout.wires)
```

> NOTE for implementer: this file already exercises `build_delete_command` end to end — reuse its exact execution harness rather than inventing `_execute_on_layout` if one exists. Match the file's established `apply`/state idiom.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_wire_edit_helpers.py::TestBuildSetInsulatedRangeCommand -v`
Expected: FAIL — `ImportError: cannot import name 'build_set_insulated_range_command'` (and `WireRangeEdit`).

- [ ] **Step 3: Implement `WireRangeEdit` + the builder**

In `src/protoboard/controller/selection/wire_edit.py`, add after `build_delete_command` (`:493`):

```python
@dataclass(frozen=True)
class WireRangeEdit:
    """Outcome of a range edit that may have cut a wire.

    ``was_cut`` is True when the op split the wire (increased the wire count),
    in which case ``survivor_wire_ids`` are the freshly-minted surviving pieces
    the tool should re-arm against. For an id-preserving full-span op both are
    empty / False.
    """

    command: "Command[ComponentState]"
    survivor_wire_ids: tuple[uuid.UUID, ...]
    was_cut: bool


def build_set_insulated_range_command(
    wires: Sequence[Wire],
    wire_net_map: dict[uuid.UUID, uuid.UUID],
    state: WireEditState,
    insulated: bool,
    add_factory: AddWireFactory = _default_add_factory,
) -> WireRangeEdit | None:
    """Set the selected sub-range's insulation, splitting the wire if needed.

    Same-wire full span → an id-preserving ``SetWireInsulatedCommand``.
    Same-wire sub-range → split into up to three pieces (left survivor /
    insulated target / right survivor), each a fresh ``Wire`` minted through
    *add_factory*; the target carries the new *insulated*, the survivors keep
    the original.  Cross-wire is refused (``None``).  A no-op (slice already at
    *insulated*) also returns ``None``.
    """
    if state.is_cross:
        return None  # Cross-wire set-insulated deferred (see docs/todo.md).

    wire = _find_wire(wires, state.start_wire_id)
    if wire is None:
        return None
    if wire.insulated == insulated:
        return None  # no-op

    start, end = state.start_idx, state.end_idx
    n = len(wire.path)
    if start == 0 and end == n - 1:
        return WireRangeEdit(
            command=cast(
                "Command[ComponentState]",
                SetWireInsulatedCommand(wire_id=wire.id, insulated=insulated),
            ),
            survivor_wire_ids=(),
            was_cut=False,
        )

    left_path = list(wire.path[:start + 1])
    target_path = list(wire.path[start:end + 1])
    right_path = list(wire.path[end:])
    cmds: list[Command[ComponentState]] = [
        cast("Command[ComponentState]", RemoveWireCommand(wire_id=wire.id))
    ]
    survivors: list[uuid.UUID] = []
    for path, keep_insulated, is_survivor in (
        (left_path, wire.insulated, True),
        (target_path, insulated, False),
        (right_path, wire.insulated, True),
    ):
        if len(path) >= 2:
            new_id = uuid.uuid4()
            cmds.append(add_factory(Wire(
                id=new_id,
                path=path,
                insulated=keep_insulated,
                side=wire.side,
                color=wire.color,
            )))
            if is_survivor:
                survivors.append(new_id)
    macro_desc = "Insulate wire fragment" if insulated else "Bare wire fragment"
    return WireRangeEdit(
        command=cast(
            "Command[ComponentState]", MacroCommand(cmds, description=macro_desc)
        ),
        survivor_wire_ids=tuple(survivors),
        was_cut=True,
    )
```

Extend `__all__` (`:77`): add `"WireRangeEdit"` and `"build_set_insulated_range_command"` (keep alphabetical order of the list). Ensure `dataclass` is imported (it is used by other dataclasses in this module; confirm the import line at the top).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_wire_edit_helpers.py::TestBuildSetInsulatedRangeCommand -v`
Expected: PASS (all five).

- [ ] **Step 5: mypy**

Run: `uv run mypy --strict src/protoboard/controller/selection/wire_edit.py`
Expected: no new errors (baseline clean for this file).

- [ ] **Step 6: Commit**

```bash
git add src/protoboard/controller/selection/wire_edit.py tests/unit/test_wire_edit_helpers.py
git commit -m "feat(wire): build_set_insulated_range_command + WireRangeEdit result"
```

---

### Task 2: `build_delete_range_edit` (delete adopts `WireRangeEdit`, existing builder untouched)

**Files:**
- Modify: `src/protoboard/controller/selection/wire_edit.py` (add after Task 1's builder; extend `__all__`)
- Test: `tests/unit/test_wire_edit_helpers.py`

**Interfaces:**
- Consumes: `build_delete_command`, `WireRangeEdit`, `WireEditState`, `_find_wire`.
- Produces: `build_delete_range_edit(wires, wire_net_map, state, add_factory=_default_add_factory) -> WireRangeEdit | None` — wraps `build_delete_command` and derives `was_cut` / `survivor_wire_ids`.

> Rationale: `build_delete_command` returns a bare `Command` and has existing callers/tests. This wrapper leaves it intact and gives the tool the richer result. It re-derives `was_cut` from the same-wire full-span test and captures the fresh survivor ids by intercepting the factory.

- [ ] **Step 1: Write the failing tests**

```python
class TestBuildDeleteRangeEdit:
    def _wire(self):
        return Wire(id=uuid.uuid4(),
                    path=[Point(0, 0), Point(1, 0), Point(2, 0), Point(3, 0), Point(4, 0)],
                    insulated=False, side="top", color=None)

    def test_full_span_delete_is_not_a_cut(self):
        wire = self._wire()
        nm = {wire.id: uuid.uuid4()}
        state = WireRange.full_span(wire)
        result = build_delete_range_edit([wire], nm, state)
        assert result is not None
        assert result.was_cut is True          # a cut happened...
        assert result.survivor_wire_ids == ()  # ...but nothing survived → tool exits

    def test_sub_range_delete_reports_two_survivors(self):
        wire = self._wire()
        nm = {wire.id: uuid.uuid4()}
        state = WireRange(wire.id, 1, wire.id, 3)
        result = build_delete_range_edit([wire], nm, state)
        assert result is not None
        assert result.was_cut is True
        assert len(result.survivor_wire_ids) == 2

    def test_invalid_cross_returns_none(self):
        w1 = self._wire()
        w2 = Wire(id=uuid.uuid4(), path=[Point(9, 9), Point(10, 9)],
                  insulated=False, side="top", color=None)
        nm = {w1.id: uuid.uuid4(), w2.id: uuid.uuid4()}
        state = WireRange(w1.id, 2, w2.id, 1)  # disconnected nets → excise None
        result = build_delete_range_edit([w1, w2], nm, state)
        assert result is None
```

> NOTE: For `test_full_span_delete_is_not_a_cut`, `build_delete_command` returns a bare `RemoveWireCommand` for full-span. The wrapper classifies that as `was_cut=True, survivors=()` so the tool's decision tree routes it to `_exit()` (whole wire gone). Confirm this matches the decision table in the spec §3.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_wire_edit_helpers.py::TestBuildDeleteRangeEdit -v`
Expected: FAIL — `cannot import name 'build_delete_range_edit'`.

- [ ] **Step 3: Implement the wrapper**

Add after `build_set_insulated_range_command`:

```python
def build_delete_range_edit(
    wires: Sequence[Wire],
    wire_net_map: dict[uuid.UUID, uuid.UUID],
    state: WireEditState,
    add_factory: AddWireFactory = _default_add_factory,
) -> WireRangeEdit | None:
    """``build_delete_command`` wrapped as a ``WireRangeEdit``.

    Captures the fresh survivor ids by wrapping *add_factory*, and reports
    ``was_cut=True`` whenever a delete actually removed wire (always, for a
    valid range).  Full-span delete removes the whole wire → no survivors →
    the tool exits; a sub-range delete leaves the two halves as survivors.
    """
    survivors: list[uuid.UUID] = []

    def _capture(wire: Wire) -> "Command[ComponentState]":
        survivors.append(wire.id)
        return add_factory(wire)

    cmd = build_delete_command(wires, wire_net_map, state, _capture)
    if cmd is None:
        return None
    return WireRangeEdit(
        command=cmd, survivor_wire_ids=tuple(survivors), was_cut=True
    )
```

Extend `__all__`: add `"build_delete_range_edit"`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_wire_edit_helpers.py::TestBuildDeleteRangeEdit -v`
Expected: PASS.

- [ ] **Step 5: Regression — old delete tests still green**

Run: `uv run pytest tests/unit/test_wire_edit_helpers.py tests/unit/test_wire_edit_arm.py -v`
Expected: PASS (Task 2 added nothing that changes `build_delete_command`).

- [ ] **Step 6: Commit**

```bash
git add src/protoboard/controller/selection/wire_edit.py tests/unit/test_wire_edit_helpers.py
git commit -m "feat(wire): build_delete_range_edit wraps delete as WireRangeEdit"
```

---

### Task 3: `WireEditArm.set_insulated` + `WireEditArm.delete_range`

**Files:**
- Modify: `src/protoboard/controller/editing/wire_edit_arm.py` (imports at `:33`; methods after `delete` at `:122-131`)
- Test: `tests/unit/test_wire_edit_arm.py`

**Interfaces:**
- Consumes: `build_set_insulated_range_command`, `build_delete_range_edit`, `WireRangeEdit`, `self._add_factory`.
- Produces:
  - `arm.set_insulated(wires: Sequence[Wire], wire_net_map: dict[uuid.UUID, uuid.UUID], state: WireEditState, insulated: bool) -> WireRangeEdit | None`
  - `arm.delete_range(wires: Sequence[Wire], wire_net_map: dict[uuid.UUID, uuid.UUID], state: WireEditState) -> WireRangeEdit | None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_wire_edit_arm.py` (mirror the existing `arm.delete` tests at `:123`/`:173` for wire/state construction):

```python
def test_arm_set_insulated_sub_range_reports_survivors():
    wire = Wire(id=uuid.uuid4(),
                path=[Point(0, 0), Point(1, 0), Point(2, 0), Point(3, 0), Point(4, 0)],
                insulated=False, side="top", color=None)
    nm = {wire.id: uuid.uuid4()}
    state = WireRange(wire.id, 1, wire.id, 3)
    result = WireEditArm().set_insulated([wire], nm, state, insulated=True)
    assert result is not None and result.was_cut is True
    assert len(result.survivor_wire_ids) == 2

def test_arm_delete_range_full_span_no_survivors():
    wire = Wire(id=uuid.uuid4(),
                path=[Point(0, 0), Point(1, 0), Point(2, 0)],
                insulated=False, side="top", color=None)
    nm = {wire.id: uuid.uuid4()}
    result = WireEditArm().delete_range([wire], nm, WireRange.full_span(wire))
    assert result is not None and result.survivor_wire_ids == ()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_wire_edit_arm.py -k "set_insulated or delete_range" -v`
Expected: FAIL — `AttributeError: 'WireEditArm' object has no attribute 'set_insulated'`.

- [ ] **Step 3: Implement the arm methods**

In `wire_edit_arm.py`, extend the import block (`:33`) to include:

```python
    build_delete_range_edit,
    build_set_insulated_range_command,
    WireRangeEdit,
```

Add after `delete` (`:131`):

```python
    def set_insulated(
        self,
        wires: Sequence[Wire],
        wire_net_map: dict[uuid.UUID, uuid.UUID],
        state: WireEditState,
        insulated: bool,
    ) -> "WireRangeEdit | None":
        """Build the set-insulated range edit; split pieces go through the factory."""
        return build_set_insulated_range_command(
            wires, wire_net_map, state, insulated, self._add_factory
        )

    def delete_range(
        self,
        wires: Sequence[Wire],
        wire_net_map: dict[uuid.UUID, uuid.UUID],
        state: WireEditState,
    ) -> "WireRangeEdit | None":
        """Delete as a ``WireRangeEdit`` (survivors surfaced for re-arm)."""
        return build_delete_range_edit(
            wires, wire_net_map, state, self._add_factory
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_wire_edit_arm.py -k "set_insulated or delete_range" -v`
Expected: PASS.

- [ ] **Step 5: mypy**

Run: `uv run mypy --strict src/protoboard/controller/editing/wire_edit_arm.py`
Expected: no new errors.

- [ ] **Step 6: Commit**

```bash
git add src/protoboard/controller/editing/wire_edit_arm.py tests/unit/test_wire_edit_arm.py
git commit -m "feat(wire): WireEditArm.set_insulated + delete_range returning WireRangeEdit"
```

---

### Task 4: Tool state — `_awaiting_repick`, `_arm_repick`, `_range_repicked`, shared post-op decision

**Files:**
- Modify: `src/protoboard/controller/tools/wire_edit_tool.py` (field at `:90-93`; `_delete_fragment` `:372`; `_commit_and_rearm` `:389`; pick machinery `:470-548`; `_wire_edit_clear` `:427`)
- Test: `tests/unit/test_wire_edit_set_insulated_tool.py` (create) — but the tool-state assertions for THIS task focus on delete parity + the wait mechanics; the `[I]` command entry is Task 5.

**Interfaces:**
- Consumes: `arm.delete_range`, `arm.set_insulated`, `WireRangeEdit`, `WireRange`, `WireEditCycle`, `Stop`, existing `_arm_pick`, `_pick_at` (`wire-vertex` branch), `_wire_by_id`, `_execute_command`, `_refresh_after_wire_op`, `_draw_wire_edit_overlay`, `_exit`.
- Produces:
  - field `self._awaiting_repick: bool`
  - `_apply_range_edit(edit: WireRangeEdit | None, empty_msg: str) -> None` — the shared decision helper both delete and set-insulated call.
  - `_arm_repick(survivor_wire_ids: tuple[uuid.UUID, ...]) -> None`
  - `_range_repicked(picked: list[Selectable]) -> None`

- [ ] **Step 1: Write the failing test (delete parity — sub-range delete now waits)**

Create `tests/unit/test_wire_edit_set_insulated_tool.py`. Use the headless tool-driving API (see `memory/project_headless_boardview_test_api`; model construction on existing wire-edit tool tests — grep `tests/` for `WireEditTool(` and `HeadlessBoardView` to copy the exact setup harness). SPDX header first line.

```python
# SPDX-License-Identifier: MIT
"""Wire-edit set-insulated + unified wait-for-re-pick tool behaviour."""
import uuid

# ... imports mirrored from the nearest existing WireEditTool headless test ...

def test_sub_range_delete_now_waits_for_repick(wire_edit_tool_on_5pt_wire):
    tool = wire_edit_tool_on_5pt_wire  # armed on a full-span 5-pt wire
    # Arm a middle sub-range (idx 1..3) via the tool's range arm path,
    # then delete.
    _arm_sub_range(tool, 1, 3)
    tool._delete_fragment()
    assert tool._awaiting_repick is True     # was _exit() before — now waits
    assert tool._wire_edit is None
    # anchor reassigned to a surviving wire that still exists
    assert tool._wire_edit_wire_id is not None
    assert tool._wire_by_id(tool._wire_edit_wire_id) is not None
```

> The implementer writes the `wire_edit_tool_on_5pt_wire` fixture + `_arm_sub_range` helper by copying the closest existing headless wire-edit test's setup. If none arms a sub-range directly, construct the `WireRange(wire.id, 1, wire.id, 3)` and assign `tool._wire_edit` before calling the op (the ops read `self._wire_edit`).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_wire_edit_set_insulated_tool.py::test_sub_range_delete_now_waits_for_repick -v`
Expected: FAIL — `AttributeError: ... '_awaiting_repick'` (or delete still exits, assertion fails).

- [ ] **Step 3: Add the field**

In `__init__` (`:90-93`), after `self._wire_edit_start`:

```python
        # True while, after a sub-range cut, the tool is disarmed and waiting
        # for the user to pick a fresh 2-vertex range (or [Esc] to leave).
        self._awaiting_repick: bool = False
```

In `_wire_edit_clear` (`:427`) add `self._awaiting_repick = False` alongside the other resets so a full exit clears the wait too.

- [ ] **Step 4: Implement the shared decision helper + re-pick + repoint delete**

Add near `_commit_and_rearm` (`:389`):

```python
    def _apply_range_edit(
        self, edit: "WireRangeEdit | None", empty_msg: str
    ) -> None:
        """Execute a range edit and choose the post-op behavior.

        No edit (refused / no-op) → status hint, stay as-is.  Not a cut
        (full-span, id-preserving) → re-arm on the same wire.  A cut with
        survivors → wait for a re-pick.  A cut that removed the whole wire
        (no survivors) → exit.
        """
        if edit is None:
            self._status(empty_msg)
            self._refresh_after_wire_op()
            return
        self._execute_command(edit.command)
        self._refresh_after_wire_op()
        if not edit.was_cut:
            self._rearm_full_span()
        elif edit.survivor_wire_ids:
            self._arm_repick(edit.survivor_wire_ids)
        else:
            self._exit()

    def _arm_repick(self, survivor_wire_ids: tuple[uuid.UUID, ...]) -> None:
        """Disarm the fragment and wait for a fresh 2-vertex range pick."""
        from protoboard.controller.selection.hit_test import WireEditCycle
        from protoboard.controller.selection.selectable import Stop

        # Reassign the anchor to a surviving wire that still exists (the one
        # field whose set-once-in-__init__ assumption we deliberately relax).
        anchor = next(
            (wid for wid in survivor_wire_ids if self._wire_by_id(wid) is not None),
            None,
        )
        if anchor is None:
            self._exit()
            return
        self._clear_wire_edit_overlay()
        self._wire_edit = None
        self._wire_edit_wire_id = anchor
        self._wire_edit_cycle = WireEditCycle()
        self._wire_edit_start = None
        self._awaiting_repick = True
        self._status(
            "Wire cut  ·  pick two vertices to select a new range  ·  [Esc] Done",
            0,
        )
        self._arm_pick(
            "wire-vertex",
            Stop.count(2),
            on_done=self._range_repicked,
        )

    def _range_repicked(self, picked: list["Selectable"]) -> None:
        """Resolve two picked vertices into a new armed range (port of
        SelectTool._range_picked): same-wire swap to start<=end, zero-length
        abort; cross-wire kept as-is."""
        from protoboard.controller.selection.adapters import WireVertexSelectable
        from protoboard.controller.selection.wire_edit import WireRange

        verts = [p for p in picked if isinstance(p, WireVertexSelectable)]
        if len(verts) < 2:
            return  # keep waiting; caller re-arms the pick if needed
        v0, v1 = verts[0], verts[1]
        start_wire, start_idx = v0.wire_id, v0.index
        end_wire, end_idx = v1.wire_id, v1.index
        if start_wire == end_wire:
            if start_idx == end_idx:
                return  # zero-length, ignore
            if end_idx < start_idx:
                start_idx, end_idx = end_idx, start_idx
        self._awaiting_repick = False
        self._wire_edit = WireRange(start_wire, start_idx, end_wire, end_idx)
        self._wire_edit_wire_id = start_wire
        self._draw_wire_edit_overlay()
        self._status(
            "Fragment selected  ·  [W] Reroute  ·  [A] Autoroute  ·  "
            "[F] Flip layer  ·  [S] Simplify  ·  [I] Insulate/bare  ·  "
            "[Del] Delete  ·  [Esc] Cancel",
            0,
        )
```

> IMPLEMENTER: confirm `WireVertexSelectable` exposes `.wire_id` and `.index` (grep the adapter). If the attribute is `.idx`, use that. Mirror exactly what `select_tool._range_picked` (`:2193-2240`) reads.

Repoint `_delete_fragment` (`:372-385`) to the shared path:

```python
    def _delete_fragment(self) -> None:
        """Delete the selected fragment — ``[Del]``.

        Full-span → whole wire gone → exit.  Sub-range → the two halves
        survive → wait for a fresh range pick (unified with set-insulated)."""
        state = self._wire_edit
        arm = self._arm
        if state is None or arm is None:
            return
        edit = arm.delete_range(
            list(self._session.layout.wires), self._wire_net_map(), state
        )
        self._apply_range_edit(edit, "Delete: nothing to remove.")
```

Add the `WireRangeEdit` import to the tool's TYPE_CHECKING block (grep the existing `if TYPE_CHECKING:` and add `from protoboard.controller.selection.wire_edit import WireRangeEdit`).

- [ ] **Step 5: Run the parity test**

Run: `uv run pytest tests/unit/test_wire_edit_set_insulated_tool.py::test_sub_range_delete_now_waits_for_repick -v`
Expected: PASS.

- [ ] **Step 6: Full-span delete still exits (regression)**

Add + run:

```python
def test_full_span_delete_still_exits(wire_edit_tool_on_5pt_wire):
    tool = wire_edit_tool_on_5pt_wire  # armed full-span
    exited = {"v": False}
    tool._return_to = lambda: exited.__setitem__("v", True)
    tool._delete_fragment()
    assert exited["v"] is True
    assert tool._awaiting_repick is False
```

Run: `uv run pytest tests/unit/test_wire_edit_set_insulated_tool.py -v`
Expected: PASS.

- [ ] **Step 7: mypy + broader regression**

Run: `uv run mypy --strict src/protoboard/controller/tools/wire_edit_tool.py`
Run: `uv run pytest tests/unit/test_wire_edit_arm.py tests/unit/test_wire_edit_helpers.py tests/unit/test_wire_edit_set_insulated_tool.py -v`
Expected: PASS, no new mypy errors.

- [ ] **Step 8: Commit**

```bash
git add src/protoboard/controller/tools/wire_edit_tool.py tests/unit/test_wire_edit_set_insulated_tool.py
git commit -m "feat(wire): unify delete + set-insulated onto wait-for-re-pick after a cut"
```

---

### Task 5: `[I]` command wiring — action-id, command_table, `_set_insulated_fragment` (toggle)

**Files:**
- Modify: `src/protoboard/controller/tools/wire_edit_tool.py` (`_ACT_*` `:60-65`; `command_table` `:157`; `displayable_action_ids` `:169`; new `_set_insulated_fragment` near `_flip_wire_fragment` `:330`)
- Test: `tests/unit/test_wire_edit_set_insulated_tool.py`

**Interfaces:**
- Consumes: `arm.set_insulated`, `_apply_range_edit`, `_wire_by_id`, `self._wire_edit_wire_id`.
- Produces: `_ACT_SET_INSULATED_FRAGMENT = "select-tool.action.set_insulated_fragment"`; `_set_insulated_fragment() -> None`.

- [ ] **Step 1: Write the failing tests**

```python
def test_set_insulated_sub_range_toggles_and_waits(wire_edit_tool_on_5pt_wire):
    tool = wire_edit_tool_on_5pt_wire  # bare wire
    _arm_sub_range(tool, 1, 3)
    tool._set_insulated_fragment()
    # wire was bare → toggled to insulated on the middle; a cut happened
    assert tool._awaiting_repick is True
    wires = list(tool._session.layout.wires)
    assert len(wires) == 3
    assert any(w.insulated for w in wires)     # the insulated middle
    assert any(not w.insulated for w in wires) # the bare survivors

def test_set_insulated_full_span_stays_armed(wire_edit_tool_on_5pt_wire):
    tool = wire_edit_tool_on_5pt_wire  # armed full-span, bare
    tool._set_insulated_fragment()
    assert tool._awaiting_repick is False
    assert tool._wire_edit is not None          # re-armed full span
    assert list(tool._session.layout.wires)[0].insulated is True

def test_set_insulated_action_id_in_command_table(wire_edit_tool_on_5pt_wire):
    tool = wire_edit_tool_on_5pt_wire
    assert "select-tool.action.set_insulated_fragment" in tool.command_table()
    assert "select-tool.action.set_insulated_fragment" in tool.displayable_action_ids()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_wire_edit_set_insulated_tool.py -k set_insulated -v`
Expected: FAIL — no `_set_insulated_fragment` / id absent from `command_table`.

- [ ] **Step 3: Implement**

Add the action id with the other `_ACT_*` (`:60-65`):

```python
_ACT_SET_INSULATED_FRAGMENT = "select-tool.action.set_insulated_fragment"
```

In `command_table()` (`:157`) add the mapping entry:

```python
            _ACT_SET_INSULATED_FRAGMENT: self._set_insulated_fragment,
```

In `displayable_action_ids()` (`:169`) add `_ACT_SET_INSULATED_FRAGMENT` to the returned list.

Add the handler after `_flip_wire_fragment` (`:342`):

```python
    def _set_insulated_fragment(self) -> None:
        """Toggle the selected range's insulation — ``[I]``.

        Reads the anchored wire's current ``insulated`` and targets the
        opposite, so a single key flips bare<->insulated for the slice.
        Full-span → id-preserving toggle (stays armed).  Sub-range → split
        into bare/insulated/bare and wait for a re-pick."""
        state = self._wire_edit
        arm = self._arm
        if state is None or arm is None:
            return
        wire = self._wire_by_id(state.start_wire_id)
        if wire is None:
            return
        edit = arm.set_insulated(
            list(self._session.layout.wires),
            self._wire_net_map(),
            state,
            insulated=not wire.insulated,
        )
        self._apply_range_edit(
            edit, "Insulate: pick a range within one wire (two vertices apart)."
        )
```

Update the two `_status` op-hint strings (`activate` `:130` and `_rearm_full_span` `:416`) to include `[I] Insulate/bare` between `[S] Simplify` and `[Del] Delete`, matching the string already used in `_range_repicked`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_wire_edit_set_insulated_tool.py -k set_insulated -v`
Expected: PASS.

- [ ] **Step 5: mypy**

Run: `uv run mypy --strict src/protoboard/controller/tools/wire_edit_tool.py`
Expected: no new errors.

- [ ] **Step 6: Commit**

```bash
git add src/protoboard/controller/tools/wire_edit_tool.py tests/unit/test_wire_edit_set_insulated_tool.py
git commit -m "feat(wire): [I] set-insulated fragment command (toggle) on WireEditTool"
```

---

### Task 6: GUI keybinding — `key_actions` `[I]` + `key_claims` extended `armed`

**Files:**
- Modify: `src/protoboard/gui/tools/gui_tools.py` (`key_actions` `:208-215`; `key_claims` `:223-230`)
- Test: `tests/unit/test_wire_edit_set_insulated_tool.py` (claim assertion via the controller tool) — GUI key_actions is a thin map; assert it at the gui-tool layer if a gui-tool test harness exists, else assert the claim predicate.

**Interfaces:**
- Consumes: `self._wire_edit`, `self._awaiting_repick`.
- Produces: `[I]` → `select-tool.action.set_insulated_fragment` in `key_actions`; extended `armed` predicate + new claim tuple in `key_claims`.

- [ ] **Step 1: Write the failing test (claim predicate covers the wait state)**

Because `key_actions`/`key_claims` live on the GUI subclass (`gui_tools.py:184`), assert via that class if the test suite constructs it headlessly (grep `tests/` for `gui_tools import WireEditTool`). Otherwise assert the predicate logic directly:

```python
def test_insulate_key_registered_and_claimed_during_wait():
    from protoboard.gui.tools.gui_tools import WireEditTool as GuiWireEditTool
    tool = GuiWireEditTool()  # or the suite's standard construction
    assert tool.key_actions()["select-tool.action.set_insulated_fragment"] == "I"
    claims = dict((aid, pred) for aid, pred in tool.key_claims())
    assert "select-tool.action.set_insulated_fragment" in claims
    # Claimed while armed:
    tool._wire_edit = object()
    tool._awaiting_repick = False
    assert claims["select-tool.action.set_insulated_fragment"]() is True
    # Claimed while awaiting re-pick (globals must stay blocked):
    tool._wire_edit = None
    tool._awaiting_repick = True
    assert claims["select-tool.action.set_insulated_fragment"]() is True
    # Not claimed when fully idle:
    tool._awaiting_repick = False
    assert claims["select-tool.action.set_insulated_fragment"]() is False
```

> If `GuiWireEditTool()` needs Qt/ctor args the suite provides via a fixture, use that fixture. The behavioural core is the three predicate assertions.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_wire_edit_set_insulated_tool.py -k claimed -v`
Expected: FAIL — key absent / predicate only checks `_wire_edit`.

- [ ] **Step 3: Implement**

In `key_actions()` (`:208`), add between the simplify and delete entries:

```python
            "select-tool.action.set_insulated_fragment": "I",
```

In `key_claims()` (`:217`), change `armed` and add the tuple:

```python
        armed = lambda: (  # noqa: E731
            self._wire_edit is not None or self._awaiting_repick
        )
        return (
            ("select-tool.action.reroute", armed),
            ("select-tool.action.autoroute", armed),
            ("select-tool.action.flip_fragment", armed),
            ("select-tool.action.simplify_fragment", armed),
            ("select-tool.action.set_insulated_fragment", armed),
            ("select-tool.action.delete", armed),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_wire_edit_set_insulated_tool.py -k claimed -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/protoboard/gui/tools/gui_tools.py tests/unit/test_wire_edit_set_insulated_tool.py
git commit -m "feat(wire): bind [I] to set-insulated fragment; claim it during re-pick wait"
```

---

### Task 7: Docs — §6.2 correction, todo deferral

**Files:**
- Modify: `docs/core-wire-semantics.md` (`:158`, `:229-240`, `:299-302`)
- Modify: `docs/todo.md`

- [ ] **Step 1: Rewrite §6.2** (`:229-240`)

Replace the "would be incoherent — setting insulation on a fragment has no model representation …" passage with:

```markdown
### 6.2 "Fragment" mixes two granularities — but insulate-on-a-slice is feasible

`insulated` and `color` are per-wire; `side` is per-fragment. Because
`Fragment` has no `insulated` field, "insulate this sub-range" cannot be a flag
flip — but it **is** feasible, and is now implemented, by the same split +
net-rederivation `delete-slice` already performs: the wire is cut into an
insulated middle plus bare-end survivors (`build_set_insulated_range_command`,
`controller/selection/wire_edit.py`). This is physically faithful — a bare
wire with heat-shrink only in the middle *is* three conductors. **Same-wire
ranges are supported; cross-wire set-insulated is deferred** (murky semantics
across a plated-through via — matches the cross-wire flip refusal). The
"same ops everywhere" goal therefore covers per-slice set-insulated too, via
split, not just the genuinely per-fragment ops.
```

- [ ] **Step 2: Update the GAP row** (`:158`)

Change the `Set bare / insulated` row's fragment column from the GAP note to:

```markdown
| **Set bare / insulated** | W3 ✅ | ✅ (same-wire, via split — `build_set_insulated_range_command`; cross-wire deferred) | — |
```

- [ ] **Step 3: Update the recommendation** (`:299-302`)

Adjust the "keep per-wire attributes whole-wire" recommendation to note set-insulated is now a per-slice op via split (bring it in line with §6.2 above; keep color/wire-pin whole-wire).

- [ ] **Step 4: Add the todo deferral**

Append to `docs/todo.md`:

```markdown
- **Cross-wire fragment set-insulated** (deferred 2026-07-24): same-wire
  set-insulated on a sub-range ships via split
  (`build_set_insulated_range_command`); the cross-wire case (re-adding the
  excised interior across a plated-through via as insulated wires) is deferred
  for murky join semantics. Spec:
  `docs/superpowers/specs/2026-07-24-set-insulated-fragment-design.md`.
```

- [ ] **Step 5: Commit**

```bash
git add docs/core-wire-semantics.md docs/todo.md
git commit -m "docs(wire): §6.2 fragment set-insulated is feasible via split; defer cross-wire"
```

---

### Task 8: Full regression + GUI smoke

**Files:** none (verification only)

- [ ] **Step 1: Full unit suite**

Run: `uv run pytest tests/unit -q`
Expected: all pass (baseline was 223 green for the wire-edit area; no reductions).

- [ ] **Step 2: mypy --strict on all touched modules**

Run: `uv run mypy --strict src/protoboard/controller/selection/wire_edit.py src/protoboard/controller/editing/wire_edit_arm.py src/protoboard/controller/tools/wire_edit_tool.py src/protoboard/gui/tools/gui_tools.py`
Expected: no new errors vs. baseline.

- [ ] **Step 3: Controller-no-gui guard**

Run: `uv run pytest tests/unit/test_controller_no_gui_import.py -v`
Expected: PASS (no `protoboard.gui` import leaked into `controller/`).

- [ ] **Step 4: GUI smoke via the run skill**

Invoke the `run-protoboard-designer` skill: load a fixture with a real wire, arm wire-edit (`[E]`), pick a middle sub-range, press `[I]`, confirm three rendered segments (bare / insulated / bare) and that the tool then waits for a fresh range pick. `[Esc]` returns to select. Capture a screenshot.

- [ ] **Step 5: Final commit (if the smoke run produced any fixture/screenshot to keep — usually none)**

No code commit expected here; if docs/screenshots are added, stage them explicitly.

---

## Self-Review

**1. Spec coverage:**
- §1 split-not-toggle → Task 1 builder. ✓
- §2 Case A/B/C → Task 1 (`full_span` → `SetWireInsulatedCommand`; sub-range split; cross-wire `None`). ✓
- §2 no-op guard → Task 1 `test_noop_...`. ✓
- §3 `WireRangeEdit` → Task 1; delete ported → Tasks 2–4 (`build_delete_range_edit`, `arm.delete_range`, `_apply_range_edit`). ✓
- §3 `_awaiting_repick` (not overloading `_wire_edit is None`) → Task 4 + claim in Task 6. ✓
- §3 `_wire_edit_wire_id` reassign → Task 4 `_arm_repick`. ✓
- §3 re-pick reuses `_arm_pick`/`_pick_at` → Task 4. ✓
- §4 `[I]` toggle + action-id + command_table + key_actions/key_claims → Tasks 5–6. ✓
- §5 tests (builder / tool-state / mypy / GUI smoke) → Tasks 1–6, 8. ✓
- §6 docs (§6.2 / GAP / recommendation / todo / CHANGELOG) → Tasks 0, 7. ✓
- §7 out-of-scope (cross-wire, two-key) → cross-wire refused (Task 1) + deferred (Task 7). ✓

**2. Placeholder scan:** No "TBD"/"handle edge cases"/"similar to Task N". Two explicit IMPLEMENTER notes point at concrete grep targets (test harness reuse, `WireVertexSelectable` attr name) — these are verification steps, not deferrals; the code to write is fully shown. ✓

**3. Type consistency:** `WireRangeEdit(command, survivor_wire_ids, was_cut)` used identically in Tasks 1–5. `build_set_insulated_range_command(wires, wire_net_map, state, insulated, add_factory)` and `arm.set_insulated(wires, wire_net_map, state, insulated)` consistent. `_apply_range_edit(edit, empty_msg)`, `_arm_repick(survivor_wire_ids)`, `_range_repicked(picked)` names stable across Tasks 4–5. Action id `select-tool.action.set_insulated_fragment` / `_ACT_SET_INSULATED_FRAGMENT` consistent Tasks 5–6. ✓

## Execution Handoff

Two execution options — see below.
