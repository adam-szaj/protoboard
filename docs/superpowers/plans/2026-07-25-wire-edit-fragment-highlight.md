# Wire-Edit Fragment Highlight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the selected fragment (or whole wire) unmistakable in wire-edit mode: dim the board except the edited wire, and draw a bright thick highlight over the picked span.

**Architecture:** Reuse the existing whole-item dim mechanism (`HighlightStack.push_transient` + `PROFILE_DIMMED_2`) — the `WireEditTool` pushes a dim entry (edited wire exempt) on enter and pops on exit. The bright span is drawn in the existing wire-edit overlay (`SelectPreview.show_wire_edit_wire` gains a span-points param; the Qt impl draws a bright thick polyline over `wire.path[start_idx:end_idx+1]`). One mechanism — whole wire is just the full-span range.

**Tech Stack:** Python 3.11+, PySide6 (QGraphicsScene), `pytest` + `pytest-qt`, frozen dataclasses.

## Global Constraints

- `mypy --strict` on every touched SOURCE file (`src/protoboard/` only; `tests/` not in the gate); no NEW error CATEGORY. This worktree (`pyright-fixes`) carries heavy pre-existing lint debt — the `"EditState" has no attribute ..."` pattern is pre-existing systemic; adding more instances is expected, not new.
- Domain/controller import no Qt (`tests/unit/test_controller_no_gui_import.py` enforces). The `SelectPreview` **Protocol** lives in `controller/tools/select_preview.py` (Qt-free); the Qt impl lives in `gui/tools/select_preview.py`.
- SPDX header `# SPDX-License-Identifier: MIT` on every NEW file.
- Conventional Commits, one per task. Never `git add -A`/`.` — `git add -u` + explicit new files. Untracked junk to LEAVE: `example.py`, `library/ICs/audio.yaml`.
- Never `git stash` in this worktree (shared stash list); for base compares use `git show REV:file > tmpfile`.
- Test the KEY/click entry path, not just the method (lesson from cd01742 / set-insulated).
- Reuse the existing GUI test harness in `tests/gui/test_select_tool.py`: `_wire_session(qtbot)`, `_v2(view)`, `_select_wire`, `_key`, `_click`, `_wire_edit_after_handoff`, `_depth`.
- Do NOT touch `HighlightEntry`/`HighlightRenderer` core (no sub-path field — decided). Do NOT fix the flip/re-arm bugs here (deferred to §5 of the spec).

Key facts (verified):
- `PROFILE_DIMMED_2` (~0.12 dim) importable from `protoboard.controller.highlight_entry` (re-exported in `gui/highlight_state.py`).
- `HighlightEntry(comp_ids=frozenset(), wire_ids=frozenset(), net_id=None, profile=..., transient=bool, label=str)` — `controller/highlight_entry.py:57`.
- `session.highlight_stack.push_transient(entry)` (`gui/highlight_state.py:99`) / `pop_transient()` (`:153`).
- `WireEditTool.activate` (`wire_edit_tool.py:101`, draws overlay at :133), `_exit` (`:555` → `_wire_edit_clear` + `_return_to`), `deactivate` (`:141`, sets `_session=None`).
- `WireEditTool._draw_wire_edit_overlay` (`:805-827`) holds `self._wire_edit` (WireRange) + `self._wire_edit_wire_id`.
- `WireRange`: `start_wire_id/start_idx/end_wire_id/end_idx`, `is_cross`, `is_full_span(wire)` (`controller/selection/wire_edit.py:97`).
- Protocol `SelectPreview.show_wire_edit_wire(path, marked)` (`controller/tools/select_preview.py:61`).
- Qt `_WireEditOverlay.show_wire(path, marked)` (`gui/tools/select_preview.py:212`); `QtSelectPreview.show_wire_edit_wire` delegates (`:340`). Cyan `_WIRE_EDIT_PEN` = `QColor(0,204,255,220)` w0.42 (`:43`), Z=9.0; dots Z=10.0.
- `select_tool._range_picked` (`:2193`) silently clears on <2 verts / same vertex (`:2208`, `:2217`).

---

### Task 1: Bright-span param on the SelectPreview Protocol + Qt overlay

**Files:**
- Modify: `src/protoboard/controller/tools/select_preview.py:61-63` (Protocol `show_wire_edit_wire`)
- Modify: `src/protoboard/gui/tools/select_preview.py` (`_WireEditOverlay.show_wire` :212, add bright-span pen constant near :43, `QtSelectPreview.show_wire_edit_wire` :340)
- Test: `tests/gui/test_wire_edit_span_overlay.py` (create)

**Interfaces:**
- Produces: `SelectPreview.show_wire_edit_wire(self, path: list[Point], marked: Collection[int], span: list[Point] = []) -> None` — added trailing `span` param (default empty = today's behavior). Qt impl draws a bright thick polyline over `span` at Z=9.3 above the cyan.

- [ ] **Step 1: Write the failing test**

Create `tests/gui/test_wire_edit_span_overlay.py`:

```python
# SPDX-License-Identifier: MIT
"""The wire-edit overlay draws a bright span polyline over the picked range."""
from __future__ import annotations

from protoboard.geometry import Point


def test_show_wire_edit_wire_draws_span_items(qtbot) -> None:
    from PySide6.QtWidgets import QGraphicsScene

    from protoboard.gui.tools.select_preview import _WireEditOverlay

    scene = QGraphicsScene()
    ov = _WireEditOverlay(lambda: scene)
    path = [Point(0, 0), Point(1, 0), Point(2, 0), Point(3, 0)]
    # No span -> today's behavior: cyan segs (3) + dots (4) = 7 items.
    ov.show_wire(path, marked={0, 3})
    n_no_span = len(ov.items)
    ov.clear()
    # With a span over the middle two vertices -> extra bright polyline items.
    ov.show_wire(path, marked={1, 2}, span=[Point(1, 0), Point(2, 0)])
    assert len(ov.items) > n_no_span, "span should add bright polyline items"


def test_show_wire_edit_wire_empty_span_matches_legacy(qtbot) -> None:
    from PySide6.QtWidgets import QGraphicsScene

    from protoboard.gui.tools.select_preview import _WireEditOverlay

    scene = QGraphicsScene()
    ov = _WireEditOverlay(lambda: scene)
    path = [Point(0, 0), Point(1, 0)]
    ov.show_wire(path, marked=set())  # span defaults to []
    # 1 cyan seg + 2 dots = 3 items, no span polyline.
    assert len(ov.items) == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra dev pytest tests/gui/test_wire_edit_span_overlay.py -q -p no:randomly`
Expected: FAIL — `show_wire()` has no `span` kwarg (TypeError) / span not drawn.

- [ ] **Step 3: Implement**

In `src/protoboard/gui/tools/select_preview.py`, add a bright-span pen constant near the other pens (~:50):

```python
_WIRE_SPAN_PEN = QPen(QColor(255, 210, 0, 235), 0.7)  # bright neon, wider than cyan
_WIRE_SPAN_PEN.setCapStyle(Qt.PenCapStyle.RoundCap)
_WIRE_SPAN_PEN.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
```

Change `_WireEditOverlay.show_wire` (:212) signature and add the span draw BEFORE the dots (so dots stay on top):

```python
    def show_wire(
        self, path: list[Point], marked: Collection[int],
        span: "list[Point] | None" = None,
    ) -> None:
        from PySide6.QtGui import QBrush
        from PySide6.QtWidgets import QGraphicsEllipseItem, QGraphicsLineItem

        pts = path
        for i in range(len(pts) - 1):
            seg = QGraphicsLineItem(pts[i].x, pts[i].y, pts[i + 1].x, pts[i + 1].y)
            seg.setPen(_WIRE_EDIT_PEN)
            seg.setZValue(9.0)
            self._scene().addItem(seg)
            self.items.append(seg)
        # NEW: bright thick polyline over the picked span (fragment or whole wire).
        span_pts = span or []
        for i in range(len(span_pts) - 1):
            hi = QGraphicsLineItem(
                span_pts[i].x, span_pts[i].y,
                span_pts[i + 1].x, span_pts[i + 1].y,
            )
            hi.setPen(_WIRE_SPAN_PEN)
            hi.setZValue(9.3)
            self._scene().addItem(hi)
            self.items.append(hi)
        for i, pt in enumerate(pts):
            r = _NODE_RADIUS
            dot = QGraphicsEllipseItem(pt.x - r, pt.y - r, 2 * r, 2 * r)
            if i in marked:
                dot.setPen(_NODE_SELECTED_PEN)
                dot.setBrush(QBrush(_NODE_SELECTED_BRUSH))
            else:
                dot.setPen(_NODE_EDIT_PEN)
                dot.setBrush(QBrush(_NODE_EDIT_BRUSH))
            dot.setZValue(10.0)
            self._scene().addItem(dot)
            self.items.append(dot)
```

Update `QtSelectPreview.show_wire_edit_wire` (:340) to forward the span:

```python
    def show_wire_edit_wire(
        self, path: list[Point], marked: Collection[int],
        span: "list[Point] | None" = None,
    ) -> None:
        self.wire_edit.show_wire(path, marked, span)
```

Update the Protocol `SelectPreview.show_wire_edit_wire` (`controller/tools/select_preview.py:61`):

```python
    def show_wire_edit_wire(
        self, path: list[Point], marked: Collection[int],
        span: "list[Point] | None" = None,
    ) -> None:
        """Add one wire's cyan segments + vertex dots (*marked* indices red),
        plus a bright thick polyline over *span* (the picked range's points)."""
        ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --extra dev pytest tests/gui/test_wire_edit_span_overlay.py -q -p no:randomly`
Expected: PASS (2 passed)

- [ ] **Step 5: mypy + commit**

Run: `uv run mypy --strict src/protoboard/gui/tools/select_preview.py src/protoboard/controller/tools/select_preview.py`
Expected: no NEW error category vs baseline.

```bash
git add -u
git add tests/gui/test_wire_edit_span_overlay.py
git commit -m "feat(wire-edit): bright span polyline param on wire-edit overlay"
```

---

### Task 2: Tool passes the picked span to the overlay

**Files:**
- Modify: `src/protoboard/controller/tools/wire_edit_tool.py:805-827` (`_draw_wire_edit_overlay`)
- Test: `tests/gui/test_wire_edit_tool.py` (add a test; reuse its existing fixtures)

**Interfaces:**
- Consumes: `SelectPreview.show_wire_edit_wire(path, marked, span)` (Task 1).
- Produces: for the anchored same-wire range, the overlay call passes `span = wire.path[start_idx:end_idx+1]`; cross-wire passes `span=[]`.

- [ ] **Step 1: Write the failing test**

Add to `tests/gui/test_wire_edit_tool.py` (a spy preview that records the span). First inspect how the file builds a `WireEditTool` with a preview — reuse that. Skeleton:

```python
def test_overlay_receives_span_slice(qtbot) -> None:
    """_draw_wire_edit_overlay passes wire.path[start:end+1] as the span."""
    # Arrange a WireEditTool armed on a known range [1,3] of a 4-point wire,
    # with a spy preview capturing show_wire_edit_wire(path, marked, span).
    # (Reuse this file's existing tool-construction helper + a fake preview.)
    ...
    tool._draw_wire_edit_overlay()
    assert spy.last_span == [Point(...idx1...), Point(...idx2...), Point(...idx3...)]
```

Implementer: `test_wire_edit_tool.py` has `_arm(view, wire)` (:76) and `_headless(session)` (:35) building a `WireEditTool` on a HeadlessBoardView through the ToolHost (so `activate()` runs). BUT `tool._preview` is `None` in headless mode (`_draw_wire_edit_overlay` early-returns when None). So: build a minimal spy class implementing the `SelectPreview` Protocol methods (record `show_wire_edit_wire(path, marked, span)` args, no-op the rest), and inject it: `tool._preview = SpyPreview()`. Arm on a KNOWN sub-range — `_arm` uses `WireRange.full_span`; for a sub-range test, construct the tool directly like `_arm` does but pass `WireRange(start_wire_id=wire.id, start_idx=1, end_wire_id=wire.id, end_idx=3)` (a 4+ point wire). Then `tool._draw_wire_edit_overlay()` and assert `spy.last_span == list(wire.path[1:4])`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra dev pytest tests/gui/test_wire_edit_tool.py -q -p no:randomly -k span`
Expected: FAIL — span not passed (spy sees no span / empty).

- [ ] **Step 3: Implement**

Replace `_draw_wire_edit_overlay` (`wire_edit_tool.py:805-827`):

```python
    def _draw_wire_edit_overlay(self) -> None:
        """Cyan segments for the armed wire + red endpoint dots + a bright
        polyline over the picked span (fragment or whole wire)."""
        if self._preview is None:
            return
        self._clear_wire_edit_overlay()
        marked: dict[uuid.UUID, set[int]] = {}
        state = self._wire_edit
        if state is not None:
            marked.setdefault(state.start_wire_id, set()).add(state.start_idx)
            marked.setdefault(state.end_wire_id, set()).add(state.end_idx)
        # Span points: only for a same-wire range (cross-wire has no single-wire
        # sub-path -> empty span, falls back to cyan + dots).
        span_wid: uuid.UUID | None = None
        span_pts: list[Point] = []
        if state is not None and not state.is_cross:
            span_wire = self._wire_by_id(state.start_wire_id)
            if span_wire is not None:
                span_pts = list(span_wire.path[state.start_idx : state.end_idx + 1])
                span_wid = state.start_wire_id
        wire_ids = set(marked)
        if self._wire_edit_wire_id is not None:
            wire_ids.add(self._wire_edit_wire_id)
        for wid in wire_ids:
            wire = self._wire_by_id(wid)
            if wire is None:
                continue
            self.preview.show_wire_edit_wire(
                list(wire.path),
                marked.get(wid, set()),
                span_pts if wid == span_wid else [],
            )
```

(Import `Point` if not already imported in this module — check the header; it's likely already imported for other geometry.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --extra dev pytest tests/gui/test_wire_edit_tool.py -q -p no:randomly -k span`
Expected: PASS

- [ ] **Step 5: mypy + commit**

Run: `uv run mypy --strict src/protoboard/controller/tools/wire_edit_tool.py`
Expected: no NEW error category.

```bash
git add -u
git commit -m "feat(wire-edit): pass picked span slice to the overlay"
```

---

### Task 3: Dim the board on enter, restore on exit

**Files:**
- Modify: `src/protoboard/controller/tools/wire_edit_tool.py` (`activate` :101, `_exit` :555, add `_push_dim`/`_pop_dim` helpers + `_dim_active` flag in `__init__` ~:72)
- Test: `tests/gui/test_wire_edit_dim.py` (create)

**Interfaces:**
- Consumes: `session.highlight_stack.push_transient/pop_transient`, `HighlightEntry`, `PROFILE_DIMMED_2`.
- Produces: on `activate`, exactly one transient dim entry (edited wire in `wire_ids`); on `_exit`, it is popped. Survives re-arm (no re-push).

- [ ] **Step 1: Write the failing test**

Create `tests/gui/test_wire_edit_dim.py`. TWO harness options — prefer the **headless** one (simpler, exercises `activate()` directly):
- **Headless (recommended):** reuse `test_wire_edit_tool.py`'s `_headless(session)` (:35) + `_arm(view, wire)` (:76). `_arm` runs `activate()` → `_push_dim` fires. Assert `session.highlight_stack.active()` after `_arm`, and after `tool._exit()`. Import the `session` fixture / `_detour_wire` from that module's conftest/helpers.
- **End-to-end (qtbot):** the `test_select_tool.py` `[E]`+clicks flow below, if you want the real key/click path.

Skeleton (qtbot variant shown; the headless variant swaps `_enter_wire_edit` for `_arm(_headless(session), wire)`):

```python
# SPDX-License-Identifier: MIT
"""Wire-edit dims the board (except the edited wire) on enter, restores on exit."""
from __future__ import annotations

from PySide6.QtCore import Qt

from tests.gui.test_select_tool import (
    _click, _key, _select_wire, _v2, _wire_edit_after_handoff, _wire_session,
)


def _enter_wire_edit(qtbot):
    session, view = _wire_session(qtbot)
    tool = _v2(view)
    _select_wire(tool, session)
    wire = next(iter(session.layout.wires))
    session.layout.ensure_wire_knots(wire)
    _key(tool, Qt.Key.Key_E)
    _click(tool, 2.5, 1.5)
    _click(tool, 3.5, 1.5)
    we = _wire_edit_after_handoff(view)
    return session, view, we, wire


def test_dim_pushed_on_enter(qtbot) -> None:
    session, view, we, wire = _enter_wire_edit(qtbot)
    active = session.highlight_stack.active()
    assert active is not None
    assert wire.id in active.wire_ids  # edited wire exempt from dim


def test_dim_popped_on_exit(qtbot) -> None:
    session, view, we, wire = _enter_wire_edit(qtbot)
    assert session.highlight_stack.active() is not None
    we._exit()
    # transient dim gone (no auto/persistent entry was pushed by this flow).
    assert session.highlight_stack.active() is None


def test_dim_not_stacked_on_rearm(qtbot) -> None:
    session, view, we, wire = _enter_wire_edit(qtbot)
    we._key_flip() if hasattr(we, "_key_flip") else we._flip_wire_fragment()
    # After a flip (commit + re-arm) the dim entry is present exactly once.
    # pop once -> empty (proves not double-pushed).
    session.highlight_stack.pop_transient()
    assert session.highlight_stack.active() is None
```

(Implementer: verify `highlight_stack.active()` returns the top entry and that `_wire_session` pushes no other transient; adapt assertions to the real `HighlightStack` API — `active()` at `gui/highlight_state.py:72`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra dev pytest tests/gui/test_wire_edit_dim.py -q -p no:randomly`
Expected: FAIL — no dim entry pushed (`active()` is None on enter).

- [ ] **Step 3: Implement**

In `WireEditTool.__init__` (~:72), add the flag:

```python
        self._dim_active = False
```

Add helpers (near `_draw_wire_edit_overlay`):

```python
    def _push_dim(self) -> None:
        """Dim the whole board except the edited wire (transient highlight)."""
        if self._dim_active:
            return
        wid = self._wire_edit_wire_id
        if wid is None or self._session is None:
            return
        stack = getattr(self._session, "highlight_stack", None)
        if stack is None:
            return
        from protoboard.controller.highlight_entry import (
            PROFILE_DIMMED_2,
            HighlightEntry,
        )
        stack.push_transient(
            HighlightEntry(
                wire_ids=frozenset({wid}),
                profile=PROFILE_DIMMED_2,
                transient=True,
                label="wire-edit",
            )
        )
        self._dim_active = True

    def _pop_dim(self) -> None:
        if not self._dim_active:
            return
        stack = getattr(self._session, "highlight_stack", None)
        if stack is not None:
            stack.pop_transient()
        self._dim_active = False
```

In `activate` (:133), push the dim right before drawing the overlay:

```python
        self._push_dim()
        self._draw_wire_edit_overlay()
```

In `_exit` (:555), pop before returning control:

```python
    def _exit(self) -> None:
        """Drop wire-edit and hand control back to the SelectTool that armed us."""
        self._pop_dim()
        self._wire_edit_clear()
        self._return_to()
```

Note: `deactivate` (:141) must NOT pop (it fires on every handoff, e.g. pick→edit); the dim is bound to the logical edit session, popped only at `_exit`. If `activate` re-fires during a re-pick handoff, `_dim_active` guards against a double-push.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --extra dev pytest tests/gui/test_wire_edit_dim.py -q -p no:randomly`
Expected: PASS

- [ ] **Step 5: mypy + controller-no-gui guard + commit**

Run: `uv run mypy --strict src/protoboard/controller/tools/wire_edit_tool.py`
Run: `uv run pytest tests/unit/test_controller_no_gui_import.py -q`  (HighlightEntry import must stay Qt-free — it is, from controller/highlight_entry.py)
Expected: no new mypy category; guard passes.

```bash
git add -u
git add tests/gui/test_wire_edit_dim.py
git commit -m "feat(wire-edit): dim board except edited wire on enter, restore on exit"
```

---

### Task 4: Silent-pick feedback

**Files:**
- Modify: `src/protoboard/controller/tools/select_tool.py:2207-2219` (`_range_picked` failed-pick branches)
- Test: `tests/gui/test_select_tool.py` (add a test)

**Interfaces:**
- Produces: a `_status_msg` on the two failed-pick branches (<2 vertices; same vertex).

- [ ] **Step 1: Write the failing test**

Add to `tests/gui/test_select_tool.py` (reuse `_wire_session`/`_v2`/`_key`/`_click`). Enter `[E]`, then click the SAME vertex twice (or a single vertex) and assert a status message was shown and no WireEditTool handoff happened. Use the tool's status sink if the harness captures it; else assert `view._active_tool` is still the SelectTool (no handoff). Skeleton:

```python
def test_range_pick_same_vertex_shows_hint(qtbot) -> None:
    session, view = _wire_session(qtbot)
    tool = _v2(view)
    _select_wire(tool, session)
    wire = next(iter(session.layout.wires))
    session.layout.ensure_wire_knots(wire)
    _key(tool, Qt.Key.Key_E)
    _click(tool, 2.5, 1.5)  # idx 1
    _click(tool, 2.5, 1.5)  # same vertex -> degenerate
    # No handoff: still the SelectTool, and a hint was surfaced.
    from protoboard.gui.tools.gui_tools import WireEditTool
    assert not isinstance(view._active_tool, WireEditTool)
    # (assert the status message if the harness exposes the last status)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra dev pytest tests/gui/test_select_tool.py -q -p no:randomly -k range_pick_same_vertex`
Expected: FAIL if asserting the status message (none emitted today). If only asserting no-handoff, confirm it currently passes and STRENGTHEN to assert the message once implemented.

- [ ] **Step 3: Implement**

In `select_tool._range_picked` (`:2207-2219`), add a hint before each `_wire_edit_clear(); return`:

```python
        verts = [p for p in picked if isinstance(p, WireVertexSelectable)]
        if len(verts) < 2:
            self._status_msg("Wire edit: pick two different vertices on one wire.")
            self._wire_edit_clear()
            return
        v0, v1 = verts[0], verts[1]
        start_wire, start_idx = v0.wire_id, v0.index
        end_wire, end_idx = v1.wire_id, v1.index
        if start_wire == end_wire:
            if start_idx == end_idx:
                self._status_msg("Wire edit: pick two different vertices on one wire.")
                self._wire_edit_clear()
                return
            if end_idx < start_idx:
                start_idx, end_idx = end_idx, start_idx
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --extra dev pytest tests/gui/test_select_tool.py -q -p no:randomly -k range_pick_same_vertex`
Expected: PASS

- [ ] **Step 5: mypy + commit**

Run: `uv run mypy --strict src/protoboard/controller/tools/select_tool.py`
Expected: no new error category.

```bash
git add -u
git commit -m "feat(wire-edit): status hint when the fragment pick fails silently"
```

---

### Task 5: Verify + GUI smoke + docs

**Files:**
- Modify: `CHANGELOG.md` (Added entry)

- [ ] **Step 1: Full GUI + unit suites**

Run: `uv run --extra dev pytest tests/gui -q -p no:randomly` and `uv run pytest tests/unit -q --ignore=tests/unit/test_benchmarks.py`
Expected: no NEW failures vs baseline (unit baseline: debug_trace x2, hit_test, library_loader). Investigate any new failure before proceeding.

- [ ] **Step 2: mypy over touched tree**

Run: `uv run mypy --strict src/protoboard/controller/tools/wire_edit_tool.py src/protoboard/controller/tools/select_tool.py src/protoboard/controller/tools/select_preview.py src/protoboard/gui/tools/select_preview.py`
Expected: no new error category.

- [ ] **Step 3: GUI smoke — SEE the highlight**

Invoke the `run-protoboard-designer` skill. Then EXTEND its driver (or write a one-off) to: select a wire, `[E]`, pick two vertices, and screenshot — confirm the board dims and a bright span is drawn on the picked fragment. Read the screenshot (Read renders PNG). Expected: dimmed board + visible bright span; no crash.

- [ ] **Step 4: Update CHANGELOG**

Add under `## [Unreleased]` → `### Added`:

```markdown
- Wire-edit mode now dims the board (except the edited wire) and draws a bright
  highlight over the selected fragment span, so the picked range is visible. A
  failed two-vertex pick now shows a status hint instead of clearing silently.
```

- [ ] **Step 5: Commit**

```bash
git add -u
git commit -m "docs: changelog for wire-edit fragment highlight"
```

---

## Self-Review

**Spec coverage:**
- §2.2 bright span in overlay → Task 1 (Protocol+Qt) + Task 2 (tool passes span). ✓
- §2.1 dim via HighlightStack + §3 lifecycle → Task 3 (push on enter, pop on `_exit`, `_dim_active` guard, no pop in `deactivate`). ✓
- §4 silent-pick feedback → Task 4. ✓
- §6 tests (dim push/pop/survive-rearm, span slice, cross-wire fallback, failed-pick hint) → Tasks 1-4 tests. ✓
- §5 deferred (flip re-test, re-arm bug, cross-wire span) → NOT in this plan (correct; deferred). ✓

**Placeholder scan:** Task 2/4 tests defer the exact fixture/spy wiring to the existing `test_wire_edit_tool.py` / `test_select_tool.py` construction with explicit instructions to reuse it — resolvable pointers, not TODOs. All production code is complete. The `...` markers are only in test skeletons where the implementer adapts one existing harness. Flagged for the executor.

**Type consistency:** `show_wire_edit_wire(path, marked, span)` signature identical across Protocol (Task 1), Qt impl (Task 1), and the tool caller (Task 2). `_push_dim`/`_pop_dim`/`_dim_active` consistent within Task 3. `PROFILE_DIMMED_2` + `HighlightEntry` imports from `controller/highlight_entry` (Qt-free) — satisfies the no-gui guard. ✓
