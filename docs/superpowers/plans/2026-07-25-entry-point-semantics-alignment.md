# Entry-Point Semantics Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route the three divergent multi-entry wire/lock operations through single `EditController` verbs, and fix whole-wire "Flip" so a mixed-side wire toggles per fragment instead of flattening.

**Architecture:** Each op becomes one façade verb on `EditController` (mirroring the existing `flip`/`rotate` verbs); the menu-bar slot and the context-menu handler both call it, deleting duplicated command construction. A new `FlipWireSideCommand` toggles each fragment's side independently; `WireCollection` gains `get_or_raise`/`get_or_return` lookup helpers.

**Tech Stack:** Python 3.11+, `pytest` (+ `pytest-qt` where a GUI slot is exercised), frozen/mutable domain dataclasses, `ruamel.yaml` (unaffected here).

## Global Constraints

- `mypy --strict` must pass. No `Any` outside documented escape hatches.
- SPDX header `# SPDX-License-Identifier: MIT` on every new file.
- Domain layer (`domain/`) imports no Qt.
- Conventional Commits (`feat:`, `fix:`, `test:`, `refactor:`).
- `Side = Literal["top", "bottom"]` (`src/protoboard/types.py:25`).
- Surgical changes only: do NOT rewrite the ~6 pre-existing manual `for wire in wires` loops in `component_cmds.py`; do NOT generalize the new collection helpers beyond `WireCollection` this task.
- Test the KEY/click entry path, not just the method (lesson from `cd01742` and set-insulated).
- `git add -u` + explicit `git add <new-file>`; never `git add -A`/`.`.

---

### Task 1: `WireCollection.get_or_raise` / `get_or_return`

**Files:**
- Modify: `src/protoboard/domain/layout/wire.py` (add two methods to `WireCollection`, after `.get()` at :332)
- Test: `tests/unit/test_wire_collection_lookup.py` (create)

**Interfaces:**
- Consumes: existing `WireCollection.get(wire_id, default=None) -> Wire | None` (wire.py:332); `Callable` already imported (`collections.abc.Callable`, wire.py:24).
- Produces:
  - `WireCollection.get_or_raise(self, wire_id: uuid.UUID, exc: Callable[[uuid.UUID], Exception] | Exception | None = None) -> Wire`
  - `WireCollection.get_or_return(self, wire_id: uuid.UUID, instead_val: Callable[[uuid.UUID], Wire] | Wire) -> Wire`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_wire_collection_lookup.py`:

```python
# SPDX-License-Identifier: MIT
"""Tests for WireCollection.get_or_raise / get_or_return."""
import uuid

import pytest

from protoboard.domain.layout.wire import Fragment, Wire, WireCollection


def _wire() -> Wire:
    frag = Fragment(path=[(0, 0), (1, 0)], side="top")
    return Wire(id=uuid.uuid4(), fragments=[frag], insulated=True)


def test_get_or_raise_hit_returns_wire() -> None:
    w = _wire()
    coll = WireCollection([w])
    assert coll.get_or_raise(w.id) is w


def test_get_or_raise_miss_raises_keyerror_with_id() -> None:
    coll = WireCollection([])
    missing = uuid.uuid4()
    with pytest.raises(KeyError) as ei:
        coll.get_or_raise(missing)
    assert missing in ei.value.args


def test_get_or_raise_miss_uses_exc_factory() -> None:
    coll = WireCollection([])
    missing = uuid.uuid4()
    with pytest.raises(ValueError, match="boom"):
        coll.get_or_raise(missing, exc=lambda k: ValueError(f"boom {k}"))


def test_get_or_raise_miss_uses_exc_instance() -> None:
    coll = WireCollection([])
    with pytest.raises(RuntimeError, match="ready"):
        coll.get_or_raise(uuid.uuid4(), exc=RuntimeError("ready"))


def test_get_or_return_hit_returns_wire() -> None:
    w = _wire()
    coll = WireCollection([w])
    other = _wire()
    assert coll.get_or_return(w.id, other) is w


def test_get_or_return_miss_returns_eager_value() -> None:
    coll = WireCollection([])
    fallback = _wire()
    assert coll.get_or_return(uuid.uuid4(), fallback) is fallback


def test_get_or_return_miss_calls_factory_with_id() -> None:
    coll = WireCollection([])
    missing = uuid.uuid4()
    seen: list[uuid.UUID] = []

    def make(k: uuid.UUID) -> Wire:
        seen.append(k)
        return _wire()

    coll.get_or_return(missing, make)
    assert seen == [missing]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_wire_collection_lookup.py -v`
Expected: FAIL — `AttributeError: 'WireCollection' object has no attribute 'get_or_raise'`

- [ ] **Step 3: Write minimal implementation**

In `src/protoboard/domain/layout/wire.py`, immediately after the existing `get` method (ends ~:333), add:

```python
    def get_or_raise(
        self,
        wire_id: uuid.UUID,
        exc: "Callable[[uuid.UUID], Exception] | Exception | None" = None,
    ) -> "Wire":
        """Return the wire for *wire_id*, or raise on a miss.

        ``exc`` may be a ready ``Exception`` or a factory taking the missing
        id; the hook returns/builds the exception to raise (it never returns a
        ``Wire`` — recovery is :meth:`get_or_return`'s job). Default miss raises
        ``KeyError(wire_id)``.
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
        instead_val: "Callable[[uuid.UUID], Wire] | Wire",
    ) -> "Wire":
        """Return the wire for *wire_id*, or a fallback on a miss.

        *instead_val* is either an eager ``Wire`` or a lazy factory taking the
        missing id.
        """
        wire = self.get(wire_id)
        if wire is not None:
            return wire
        return instead_val(wire_id) if callable(instead_val) else instead_val
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_wire_collection_lookup.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: mypy + commit**

Run: `uv run mypy --strict src/protoboard/domain/layout/wire.py`
Expected: no new errors.

```bash
git add -u
git add tests/unit/test_wire_collection_lookup.py
git commit -m "feat(wire): WireCollection.get_or_raise / get_or_return lookup helpers"
```

---

### Task 2: `FlipWireSideCommand` (per-fragment toggle)

**Files:**
- Modify: `src/protoboard/domain/commands/component_cmds.py` (add class after `SetWireSideCommand`, which ends at :837)
- Test: `tests/unit/test_flip_wire_side_command.py` (create)

**Interfaces:**
- Consumes: `WireCollection.get_or_raise` (Task 1); `rebuild_fragments(fragments, sides)` (wire.py:78); `Command[ComponentState]`, `ComponentState` (already imported in component_cmds.py).
- Produces: `FlipWireSideCommand(wire_id: uuid.UUID)` — a `Command[ComponentState]` whose `apply` inverts each fragment's `side` and sets `wire.side = wire.fragments[0].side`; `revert` restores the pre-apply fragment sides.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_flip_wire_side_command.py`:

```python
# SPDX-License-Identifier: MIT
"""FlipWireSideCommand toggles each fragment's side (mixed wire stays mixed)."""
import uuid

from protoboard.domain.commands.component_cmds import FlipWireSideCommand
from protoboard.domain.layout.wire import Fragment, Wire, WireCollection


class _State:
    """Minimal ComponentState stand-in: only .layout.wires is touched."""

    class _Layout:
        def __init__(self, wires: WireCollection) -> None:
            self.wires = wires

    def __init__(self, wires: WireCollection) -> None:
        self.layout = _State._Layout(wires)


def _mixed_wire() -> Wire:
    return Wire(
        id=uuid.uuid4(),
        fragments=[
            Fragment(path=[(0, 0), (1, 0)], side="top"),
            Fragment(path=[(1, 0), (2, 0)], side="bottom"),
            Fragment(path=[(2, 0), (3, 0)], side="top"),
        ],
        insulated=True,
    )


def test_flip_mixed_wire_toggles_each_fragment() -> None:
    w = _mixed_wire()
    state = _State(WireCollection([w]))
    FlipWireSideCommand(wire_id=w.id).apply(state)  # type: ignore[arg-type]
    assert [f.side for f in w.fragments] == ["bottom", "top", "bottom"]


def test_flip_revert_restores_original_sides() -> None:
    w = _mixed_wire()
    state = _State(WireCollection([w]))
    cmd = FlipWireSideCommand(wire_id=w.id)
    cmd.apply(state)  # type: ignore[arg-type]
    cmd.revert(state)  # type: ignore[arg-type]
    assert [f.side for f in w.fragments] == ["top", "bottom", "top"]


def test_flip_uniform_wire() -> None:
    w = Wire(
        id=uuid.uuid4(),
        fragments=[Fragment(path=[(0, 0), (1, 0)], side="top")],
        insulated=False,
    )
    state = _State(WireCollection([w]))
    FlipWireSideCommand(wire_id=w.id).apply(state)  # type: ignore[arg-type]
    assert [f.side for f in w.fragments] == ["bottom"]
    assert w.side == "bottom"


def test_flip_missing_wire_raises_keyerror() -> None:
    state = _State(WireCollection([]))
    import pytest

    with pytest.raises(KeyError):
        FlipWireSideCommand(wire_id=uuid.uuid4()).apply(state)  # type: ignore[arg-type]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_flip_wire_side_command.py -v`
Expected: FAIL — `ImportError: cannot import name 'FlipWireSideCommand'`

- [ ] **Step 3: Write minimal implementation**

In `src/protoboard/domain/commands/component_cmds.py`, after `SetWireSideCommand` (after :837, before `FuseTouchingWiresCommand` at :840), add:

```python
@dataclass
class FlipWireSideCommand(Command[ComponentState]):
    """Flip a whole wire by toggling EACH fragment's side independently.

    A mixed wire ``[top, bottom, top]`` becomes ``[bottom, top, bottom]`` — it
    stays mixed, inverted. Distinct from :class:`SetWireSideCommand`, which
    flattens every fragment to one side ("Move to <side>"). Reads no direction
    off ``wire.side`` (it always inverts), so it cannot flatten a mixed wire the
    way the old ``side=None`` toggle did (see docs/todo.md, commit cd01742).
    """

    wire_id: uuid.UUID
    _old_fragment_sides: list[str] = field(
        default_factory=list, init=False, repr=False,
    )

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Flip wire"

    def apply(self, state: ComponentState) -> None:
        wire = state.layout.wires.get_or_raise(self.wire_id)
        self._old_fragment_sides = [f.side for f in wire.fragments]
        flipped = [
            "bottom" if s == "top" else "top" for s in self._old_fragment_sides
        ]
        wire.fragments = rebuild_fragments(wire.fragments, flipped)  # type: ignore[arg-type]
        wire.side = wire.fragments[0].side  # type: ignore[assignment]

    def revert(self, state: ComponentState) -> None:
        if not self._old_fragment_sides:
            return
        wire = state.layout.wires.get_or_raise(self.wire_id)
        wire.fragments = rebuild_fragments(
            wire.fragments, self._old_fragment_sides,  # type: ignore[arg-type]
        )
        wire.side = wire.fragments[0].side  # type: ignore[assignment]
```

Verify `rebuild_fragments` is in the wire imports at component_cmds.py:30 (it imports from `protoboard.domain.layout.wire`); if absent, add it to that import block.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_flip_wire_side_command.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: mypy + commit**

Run: `uv run mypy --strict src/protoboard/domain/commands/component_cmds.py`
Expected: no new errors.

```bash
git add -u
git add tests/unit/test_flip_wire_side_command.py
git commit -m "feat(wire): FlipWireSideCommand — per-fragment toggle for whole-wire flip"
```

---

### Task 3: `EditController` verbs — `set_wires_insulated`, `set_wires_side`, `flip_wires`, `set_locked`

**Files:**
- Modify: `src/protoboard/controller/editing/controller.py` (add four verbs after `add_wire`, which starts :768 — place them after the wire section, before `resolve_placement` at :821)
- Test: `tests/unit/test_edit_controller_wire_lock_verbs.py` (create)

**Interfaces:**
- Consumes: `SetWireInsulatedCommand`, `SetWireSideCommand`, `LockComponentCommand` (`protoboard.domain.commands.component_cmds`); `FlipWireSideCommand` (Task 2); `MacroCommand`, `Command`, `ComponentState` (`protoboard.domain.commands.base`); `self._execute(cmd) -> bool` (controller.py:1524); `self._state.layout.wires` / `.placements`.
- Produces (public verbs — later tasks call these):
  - `set_wires_insulated(self, wire_ids: Iterable[uuid.UUID], *, insulated: bool) -> None`
  - `set_wires_side(self, wire_ids: Iterable[uuid.UUID], *, side: Side) -> None`
  - `flip_wires(self, wire_ids: Iterable[uuid.UUID]) -> None`
  - `set_locked(self, component_ids: Iterable[uuid.UUID], *, locked: bool) -> None`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_edit_controller_wire_lock_verbs.py`. Use the existing session/controller fixture pattern — check `tests/unit/test_edit_controller_add_wire.py` for the canonical fixture that builds an `EditorSession`/`HeadlessSession` with `.edit_controller`; reuse its setup helper. Skeleton:

```python
# SPDX-License-Identifier: MIT
"""EditController wire/lock verbs unify the multi-entry ops (one code path)."""
import uuid

# Reuse the session-building helper from the sibling add_wire test.
from tests.unit.test_edit_controller_add_wire import _session, _place  # HeadlessSession builder + component placer


def _add_mixed_wire(session) -> uuid.UUID:
    """Add a wire, then split it so it has top+bottom fragments.

    If a direct fragment-side constructor is available in the test utils, use
    it; otherwise build via edit_controller.add_wire then a SetWireSideCommand
    on one fragment. Return the wire id.
    """
    ...


def test_flip_wires_mixed_stays_mixed(session) -> None:
    wid = _add_mixed_wire(session)
    before = [f.side for f in session.layout.wires.get_or_raise(wid).fragments]
    session.edit_controller.flip_wires([wid])
    after = [f.side for f in session.layout.wires.get_or_raise(wid).fragments]
    assert after == ["bottom" if s == "top" else "top" for s in before]


def test_set_wires_side_flattens(session) -> None:
    wid = _add_mixed_wire(session)
    session.edit_controller.set_wires_side([wid], side="top")
    sides = {f.side for f in session.layout.wires.get_or_raise(wid).fragments}
    assert sides == {"top"}


def test_set_wires_insulated(session) -> None:
    wid = _add_mixed_wire(session)
    session.edit_controller.set_wires_insulated([wid], insulated=False)
    assert session.layout.wires.get_or_raise(wid).insulated is False


def test_set_locked_locks_component(session) -> None:
    cid = ...  # place a component via edit_controller.place_component / fixture
    session.edit_controller.set_locked([cid], locked=True)
    assert session.layout.placements[cid].locked is True
```

Note for the implementer: fill the `...` using the concrete fixture found in `test_edit_controller_add_wire.py`. Do not invent a session API — copy the sibling test's construction verbatim.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_edit_controller_wire_lock_verbs.py -v`
Expected: FAIL — `AttributeError: 'EditController' object has no attribute 'flip_wires'`

- [ ] **Step 3: Write minimal implementation**

In `src/protoboard/controller/editing/controller.py`, after `add_wire` (before `resolve_placement` at :821), add:

```python
    def set_wires_insulated(
        self, wire_ids: "Iterable[uuid.UUID]", *, insulated: bool
    ) -> None:
        """Set every wire in *wire_ids* insulated/bare as one macro.

        The single unified builder for the "Wire: Set Insulated / Bare" op —
        both the menu-bar slot and the context-menu handler call this.
        """
        from protoboard.domain.commands.base import MacroCommand
        from protoboard.domain.commands.component_cmds import SetWireInsulatedCommand

        ids = list(wire_ids)
        if not ids:
            return
        cmds: list[Command[ComponentState]] = [
            cast("Command[ComponentState]",
                 SetWireInsulatedCommand(wire_id=w, insulated=insulated))
            for w in ids
        ]
        verb = "insulated" if insulated else "bare"
        cmd = cmds[0] if len(cmds) == 1 else MacroCommand(
            cmds, description=f"Set {len(cmds)} wires {verb}",
        )
        self._execute(cmd)

    def set_wires_side(
        self, wire_ids: "Iterable[uuid.UUID]", *, side: "Side"
    ) -> None:
        """Flatten every wire in *wire_ids* onto *side* (explicit "Move to").

        The single unified builder for "Wire: Move to Top/Bottom Layer".
        Distinct verb from :meth:`flip_wires` (toggle).
        """
        from protoboard.domain.commands.base import MacroCommand
        from protoboard.domain.commands.component_cmds import SetWireSideCommand

        ids = list(wire_ids)
        if not ids:
            return
        cmds: list[Command[ComponentState]] = [
            cast("Command[ComponentState]", SetWireSideCommand(wire_id=w, side=side))
            for w in ids
        ]
        cmd = cmds[0] if len(cmds) == 1 else MacroCommand(
            cmds, description=f"Move {len(cmds)} wires to {side}",
        )
        self._execute(cmd)

    def flip_wires(self, wire_ids: "Iterable[uuid.UUID]") -> None:
        """Flip every wire in *wire_ids* by toggling each fragment's side.

        A mixed wire stays mixed (inverted); no ``wire.side`` is read, so a
        mixed wire is never flattened (the old ``side=None`` bug). The single
        unified builder for the "Wire: Flip to Opposite Layer" op.
        """
        from protoboard.domain.commands.base import MacroCommand
        from protoboard.domain.commands.component_cmds import FlipWireSideCommand

        ids = list(wire_ids)
        if not ids:
            return
        cmds: list[Command[ComponentState]] = [
            cast("Command[ComponentState]", FlipWireSideCommand(wire_id=w))
            for w in ids
        ]
        cmd = cmds[0] if len(cmds) == 1 else MacroCommand(
            cmds, description=f"Flip {len(cmds)} wires",
        )
        self._execute(cmd)

    def set_locked(
        self, component_ids: "Iterable[uuid.UUID]", *, locked: bool
    ) -> None:
        """Set the lock flag on every component in *component_ids* as one macro.

        The single unified builder for "Lock / Unlock". The toggle decision
        (all-locked -> unlock) stays with the caller, which passes an explicit
        *locked*.
        """
        from protoboard.domain.commands.base import MacroCommand
        from protoboard.domain.commands.component_cmds import LockComponentCommand

        placements = self._state.layout.placements
        cmds: list[Command[ComponentState]] = [
            cast("Command[ComponentState]",
                 LockComponentCommand(component_id=c, locked=locked))
            for c in component_ids
            if c in placements and placements[c].locked != locked
        ]
        if not cmds:
            return
        verb = "Lock" if locked else "Unlock"
        cmd = cmds[0] if len(cmds) == 1 else MacroCommand(
            cmds, description=f"{verb} {len(cmds)} component(s)",
        )
        self._execute(cmd)
```

Ensure `Side` is importable in this module — it uses `from protoboard.types import Side` under `TYPE_CHECKING` (add to the existing TYPE_CHECKING block if absent; the string annotation `"Side"` keeps runtime clean). `cast`, `Iterable`, `Command`, `ComponentState` are already imported in this file (used by `flip`/`rotate`); verify and add any missing to the existing import blocks.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_edit_controller_wire_lock_verbs.py -v`
Expected: PASS

- [ ] **Step 5: mypy + commit**

Run: `uv run mypy --strict src/protoboard/controller/editing/controller.py`
Expected: no new errors.

```bash
git add -u
git add tests/unit/test_edit_controller_wire_lock_verbs.py
git commit -m "feat(controller): unified set_wires_insulated/side, flip_wires, set_locked verbs"
```

---

### Task 4: Rewire wire-insulated entry points to the verb

**Files:**
- Modify: `src/protoboard/gui/main_window.py:1934-1954` (`_set_selected_wire_insulated`)
- Modify: `src/protoboard/controller/tools/select_tool.py:3303-3324` (`_menu_set_wire_insulated`)
- Test: `tests/unit/test_wire_insulated_entrypoint_parity.py` (create)

**Interfaces:**
- Consumes: `EditController.set_wires_insulated(wire_ids, *, insulated)` (Task 3); `self._session.edit_controller`; `main_window._get_selected_wire_ids()`; `select_tool` receives `wire_ids: set[uuid.UUID]`.
- Produces: both handlers now delegate; no local command construction remains.

- [ ] **Step 1: Write the failing parity test**

Create `tests/unit/test_wire_insulated_entrypoint_parity.py`. Build a session with two selected wires (reuse the fixture from Task 3's test / `test_edit_controller_add_wire.py`). Assert: driving the menu slot and driving the ctx handler on the same selection both leave every wire `insulated=<target>` and produce identical layout state. Skeleton:

```python
# SPDX-License-Identifier: MIT
"""Menu and context-menu 'set insulated' reach the same verb (parity)."""
from tests.unit.test_edit_controller_add_wire import _session, _place  # HeadlessSession builder + component placer


def test_ctx_handler_sets_insulated(session_with_two_wires) -> None:
    session, tool, wids = session_with_two_wires
    tool._menu_set_wire_insulated(set(wids), False)
    assert all(session.layout.wires.get_or_raise(w).insulated is False for w in wids)


def test_menu_and_ctx_agree(session_with_two_wires) -> None:
    session, tool, wids = session_with_two_wires
    # ctx path
    tool._menu_set_wire_insulated(set(wids), True)
    ctx_state = [session.layout.wires.get_or_raise(w).insulated for w in wids]
    # reset + menu path (reuse main_window slot if a MainWindow is in the fixture,
    # else assert both call edit_controller.set_wires_insulated with same args)
    ...
    assert ctx_state == [True, True]
```

Implementer: if the fixture has no `MainWindow`, assert parity at the verb boundary — monkeypatch `edit_controller.set_wires_insulated` and confirm both `main_window._set_selected_wire_insulated` and `select_tool._menu_set_wire_insulated` call it with the same `(wire_ids, insulated)`. This is the same-op-same-code assertion.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_wire_insulated_entrypoint_parity.py -v`
Expected: FAIL (ctx handler still builds its own command / behaviour differs, or the monkeypatched verb is not called).

- [ ] **Step 3: Rewire both handlers**

Replace `src/protoboard/gui/main_window.py:1934-1954` body with:

```python
    def _set_selected_wire_insulated(self, insulated: bool) -> None:
        wire_ids = self._get_selected_wire_ids()
        if not wire_ids:
            return
        self._session.edit_controller.set_wires_insulated(
            wire_ids, insulated=insulated
        )
        self._session.layout_changed.emit()
```

Replace `src/protoboard/controller/tools/select_tool.py:3303-3324` (`_menu_set_wire_insulated`) body with:

```python
    def _menu_set_wire_insulated(
        self, wire_ids: set[uuid.UUID], insulated: bool
    ) -> None:
        if not wire_ids:
            return
        self._session.edit_controller.set_wires_insulated(
            wire_ids, insulated=insulated
        )
        self._session.layout_changed.emit()
```

Remove now-unused local imports (`SetWireInsulatedCommand`, `MacroCommand`, `cast`, `Command`, `ComponentState`) from each method body only — do not touch module-level imports used elsewhere.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_wire_insulated_entrypoint_parity.py tests/unit/test_edit_controller_wire_lock_verbs.py -v`
Expected: PASS

- [ ] **Step 5: mypy + commit**

Run: `uv run mypy --strict src/protoboard/gui/main_window.py src/protoboard/controller/tools/select_tool.py`
Expected: no new errors.

```bash
git add -u
git add tests/unit/test_wire_insulated_entrypoint_parity.py
git commit -m "refactor(wire): route menu+ctx set-insulated through EditController verb"
```

---

### Task 5: Rewire wire side/flip entry points + delete the flatten-flip bug path

**Files:**
- Modify: `src/protoboard/controller/tools/select_tool.py:3646-3691` (`_move_selected_wires_to_side` — split into flip vs move, delete the stale-`wire.side` guard) and `:3326-3344` (`_menu_set_wire_side`)
- Modify: `src/protoboard/gui/main_window.py:1956-1967` (`_move_selected_wires_to_side` wrapper) and `:1100` slot (`_wire_flip_action`)
- Test: `tests/unit/test_wire_flip_mixed_entrypoint.py` (create)

**Interfaces:**
- Consumes: `EditController.flip_wires(wire_ids)` and `set_wires_side(wire_ids, *, side)` (Task 3); `select_tool._selected_by_kind("wire")`.
- Produces: menu "Wire: Flip" → `flip_wires`; menu/ctx "Move to <side>" → `set_wires_side`. The `side=None` flatten path is gone.

- [ ] **Step 1: Write the failing test (the bug regression)**

Create `tests/unit/test_wire_flip_mixed_entrypoint.py`. Using the Headless BoardView / session fixture, select a single mixed wire `[top,bottom,top]` and invoke the **whole-wire flip entry** (`tool._flip_selected_wires()` — the new method, see Step 3 — or the main_window `_wire_flip_action`). Assert the wire is now `[bottom,top,bottom]` (stays mixed), NOT all-bottom.

```python
# SPDX-License-Identifier: MIT
"""Whole-wire flip of a mixed wire toggles per fragment (regression for cd01742 sibling bug)."""
...

def test_whole_wire_flip_mixed_stays_mixed(session_with_mixed_wire) -> None:
    session, tool, wid = session_with_mixed_wire
    before = [f.side for f in session.layout.wires.get_or_raise(wid).fragments]
    assert before == ["top", "bottom", "top"]  # precondition
    tool._flip_selected_wires()  # new whole-wire flip entry
    after = [f.side for f in session.layout.wires.get_or_raise(wid).fragments]
    assert after == ["bottom", "top", "bottom"]  # toggled, NOT flattened


def test_move_to_side_still_flattens(session_with_mixed_wire) -> None:
    session, tool, wid = session_with_mixed_wire
    tool._move_selected_wires_to_side("bottom")
    sides = {f.side for f in session.layout.wires.get_or_raise(wid).fragments}
    assert sides == {"bottom"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_wire_flip_mixed_entrypoint.py -v`
Expected: FAIL — `_flip_selected_wires` missing, or (if wired to old path) wire flattened to all-bottom.

- [ ] **Step 3: Split the tool method + delete the guard**

In `src/protoboard/controller/tools/select_tool.py`, replace `_move_selected_wires_to_side` (:3646-3691) with a **move-only** method (explicit side, no `None`, no toggle guard) plus a **new flip method**:

```python
    def _move_selected_wires_to_side(self, target_side: str) -> None:
        """Flatten all selected whole wires onto *target_side* (explicit Move).

        "Wire: Move to Top/Bottom Layer". The per-fragment toggle flip is
        :meth:`_flip_selected_wires`; the per-fragment [F] op is WireEditTool's.
        """
        wire_ids = self._selected_by_kind("wire")
        if not wire_ids:
            return
        from protoboard.types import Side
        from typing import cast
        self._session.edit_controller.set_wires_side(
            wire_ids, side=cast(Side, target_side)
        )
        self._session.layout_changed.emit()
        self._status_msg(
            f"Moved {len(wire_ids)} wire(s) to {target_side} layer."
        )

    def _flip_selected_wires(self) -> None:
        """Flip all selected whole wires by toggling each fragment's side.

        Mixed wires stay mixed (inverted) — no flatten, no "different layers"
        refusal (the old stale-wire.side guard is gone; see cd01742).
        """
        wire_ids = self._selected_by_kind("wire")
        if not wire_ids:
            return
        self._session.edit_controller.flip_wires(wire_ids)
        self._session.layout_changed.emit()
        self._status_msg(f"Flipped {len(wire_ids)} wire(s).")
```

Replace `_menu_set_wire_side` (:3326-3344) body with a delegation:

```python
    def _menu_set_wire_side(self, wire_ids: set[uuid.UUID], side: str) -> None:
        if not wire_ids:
            return
        from protoboard.types import Side
        from typing import cast
        self._session.edit_controller.set_wires_side(wire_ids, side=cast(Side, side))
        self._session.layout_changed.emit()
```

In `src/protoboard/gui/main_window.py`: the `_move_selected_wires_to_side(side: str | None)` wrapper (:1956) currently forwards `None` for flip. Change `_wire_flip_action` (:1100) to call the tool's new flip method, and make the wrapper accept only `str`:

```python
    def _move_selected_wires_to_side(self, side: str) -> None:
        tool = self._get_select_tool()
        if tool is None:
            return
        tool._move_selected_wires_to_side(side)

    def _wire_flip_action(self) -> None:
        tool = self._get_select_tool()
        if tool is None:
            return
        tool._flip_selected_wires()
```

Check callers of `_move_selected_wires_to_side(None)`: `grep -rn "_move_selected_wires_to_side(None\|_move_selected_wires_to_side(\s*None" src/` — the only `None` caller should be the old `_wire_flip_action`; after this change there are none. If ACTION_WIRE_FLIP's `shortcut_args` passed `None`, confirm the slot is now `_wire_flip_action` (no args) in actions.py:210 (it already is — slot `_wire_flip_action`).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_wire_flip_mixed_entrypoint.py -v`
Expected: PASS

- [ ] **Step 5: mypy + commit**

Run: `uv run mypy --strict src/protoboard/controller/tools/select_tool.py src/protoboard/gui/main_window.py`
Expected: no new errors.

```bash
git add -u
git add tests/unit/test_wire_flip_mixed_entrypoint.py
git commit -m "fix(wire): whole-wire flip of a mixed wire toggles per fragment, not flatten"
```

---

### Task 6: Rewire lock entry points to the verb

**Files:**
- Modify: `src/protoboard/gui/main_window.py:1819-1861` (`_lock_toggle`)
- Modify: `src/protoboard/controller/tools/select_tool.py:3377-3394` (`_menu_lock`)
- Test: `tests/unit/test_lock_entrypoint_parity.py` (create)

**Interfaces:**
- Consumes: `EditController.set_locked(component_ids, *, locked)` (Task 3).
- Produces: `_lock_toggle` keeps the all-locked→unlock *decision* (reads placements), then calls `set_locked`; `_menu_lock` delegates with its explicit bool.

- [ ] **Step 1: Write the failing parity test**

Create `tests/unit/test_lock_entrypoint_parity.py`: place ≥2 components, assert `_menu_lock(ids, True)` locks all; assert `_lock_toggle` on an all-locked selection unlocks all (and on a mixed selection locks all). Verify both go through `edit_controller.set_locked` (monkeypatch to record calls if no full MainWindow fixture).

```python
# SPDX-License-Identifier: MIT
"""Menu toggle and ctx explicit lock both reach EditController.set_locked."""
...

def test_ctx_lock_all(session_with_components) -> None:
    session, tool, cids = session_with_components
    tool._menu_lock(set(cids), True)
    assert all(session.layout.placements[c].locked for c in cids)


def test_menu_toggle_unlocks_when_all_locked(session_with_components) -> None:
    session, mw, cids = session_with_components
    for c in cids:  # arrange: all locked
        session.layout.placements[c].locked = True
    mw._lock_toggle()
    assert all(not session.layout.placements[c].locked for c in cids)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_lock_entrypoint_parity.py -v`
Expected: FAIL (handlers still build their own commands / verb not called).

- [ ] **Step 3: Rewire both handlers**

Replace `select_tool._menu_lock` (:3377-3394) body:

```python
    def _menu_lock(self, comp_ids: set[uuid.UUID], locked: bool) -> None:
        if not comp_ids:
            return
        self._session.edit_controller.set_locked(comp_ids, locked=locked)
        self._session.layout_changed.emit()
```

Replace `main_window._lock_toggle` (:1819-1861) — keep the toggle decision, delegate the build:

```python
    def _lock_toggle(self) -> None:
        """Toggle lock on every component in the current selection.

        If every component is already locked -> unlock all; else lock all
        (idempotent). The all-locked decision stays here (it reads current
        placements); the command build is EditController.set_locked.
        """
        tool = self._get_select_tool()
        if tool is None:
            return
        targets: set[uuid.UUID] = set()
        if tool._selected_ids:
            targets.update(tool._selected_ids)
        if tool._selected_id is not None:
            targets.add(tool._selected_id)
        if not targets:
            self._status.showMessage("Nothing selected to lock.", 3000)
            return
        placements = self._session.layout.placements
        valid = [cid for cid in targets if cid in placements]
        if not valid:
            return
        all_locked = all(placements[cid].locked for cid in valid)
        new_locked = not all_locked
        self._session.edit_controller.set_locked(valid, locked=new_locked)
        self._session.layout_changed.emit()
        self._update_undo_redo()
        verb = "Locked" if new_locked else "Unlocked"
        self._status.showMessage(f"{verb} component(s).", 3000)
```

Remove the now-unused local imports in each method (`LockComponentCommand`, `MacroCommand`, `cast`, `ComponentState`) — method-body imports only.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_lock_entrypoint_parity.py -v`
Expected: PASS

- [ ] **Step 5: mypy + commit**

Run: `uv run mypy --strict src/protoboard/gui/main_window.py src/protoboard/controller/tools/select_tool.py`
Expected: no new errors.

```bash
git add -u
git add tests/unit/test_lock_entrypoint_parity.py
git commit -m "refactor(lock): route menu toggle + ctx lock through EditController.set_locked"
```

---

### Task 7: Full-suite regression + docs

**Files:**
- Modify: `docs/todo.md` (note the follow-up: generalize `get_or_raise`/`get_or_return` to `placements`/`instances`/`components`; component flip single/multi split)
- Modify: `CHANGELOG.md` (entry for the mixed-wire flip fix + verb unification)

- [ ] **Step 1: Run the full unit suite**

Run: `uv run pytest tests/unit -q`
Expected: PASS (no regressions). Investigate any failure before proceeding — do not `-x`-skip.

- [ ] **Step 2: Run mypy over touched trees**

Run: `uv run mypy --strict src/protoboard/domain src/protoboard/controller src/protoboard/gui/main_window.py`
Expected: no new errors.

- [ ] **Step 3: Smoke-test the app**

Invoke the `run-protoboard-designer` skill (headless driver). Manually confirm no crash on launch and wire ops are reachable. Expected: screenshots saved, no traceback.

- [ ] **Step 4: Update docs/todo.md and CHANGELOG.md**

Add to `docs/todo.md`:

```markdown
- Generalize `WireCollection.get_or_raise` / `get_or_return` to the other
  id-keyed collections (`placements`, `instances`, `components`) via a shared
  mixin — deferred from the 2026-07-25 entry-point alignment (WireCollection-only).
- Component "Flip" single/multi split: menu single -> `_flip_selected`, multi ->
  `_flip_multi`; converge both on `EditController.flip` (low-risk, deferred).
```

Add to `CHANGELOG.md` (under Unreleased/Fixed and Changed):

```markdown
### Fixed
- Whole-wire "Flip" of a mixed-side wire now toggles each fragment
  independently (stays mixed, inverted) instead of flattening to one layer.

### Changed
- Wire set-insulated/bare, move-to-side, flip, and component lock/unlock now
  route through single `EditController` verbs from both menu and context-menu
  (same-op-same-code).
```

- [ ] **Step 5: Commit**

```bash
git add -u
git commit -m "docs: changelog + todo for wire-flip fix and verb unification"
```

---

## Self-Review

**Spec coverage:**
- §1 seam + 4 verbs → Task 3 (verbs), Tasks 4/5/6 (rewiring). ✓
- §2 mixed-wire fix + `FlipWireSideCommand` → Task 2 + Task 5. ✓
- §2a `get_or_raise`/`get_or_return` → Task 1. ✓
- §3 tests (mixed flip, uniform, flatten preserved, multi-wire, parity ×3) → Tasks 2, 3, 4, 5, 6. ✓
- §4 non-goals (no loop rewrite, no cross-collection generalize, no verb-merge) → Global Constraints + Task 7 todo. ✓

**Placeholder scan:** Task 3/4/6 tests reuse the verified sibling fixture `_session()` + `_place()` from `test_edit_controller_add_wire.py` (confirmed to exist: `_session() -> HeadlessSession` at :45, `_place(session, x, y, refdes)` at :50, `edit_controller.add_wire(path, insulated=, side=)` idiom throughout). All command/verb bodies are complete code. The `...` markers appear only inside test skeletons where the implementer must adapt the mixed-wire setup and select-wire steps using that fixture — a real, resolvable pointer, not a "TODO". Flagged for the executor's attention.

**Type consistency:** verb names (`set_wires_insulated`, `set_wires_side`, `flip_wires`, `set_locked`) are identical across Task 3 (definition) and Tasks 4/5/6 (calls). `FlipWireSideCommand(wire_id=...)` identical in Task 2 and Task 3. `get_or_raise` signature identical in Task 1 and its uses in Task 2. ✓
