# Wire-Edit Fragment Highlight — Design

Date: 2026-07-25
Branch: `feat/item-id-handle-model`
Related: `project_wire_range_unification_2026_07_24` (whole wire = full-span range),
`project_armed_subtool_lifecycle_2026_07_24` (WireEditTool lifecycle),
`feedback_refresh_highlights_after_mutation`.

## 0. Problem

In wire-edit mode (`[E]` on a selected wire → pick two vertices → `WireEditTool`),
there is **no clear visual indication of which fragment/range is selected**. The
current overlay (`_WireEditOverlay.show_wire`) paints the **whole** wire a uniform
cyan and puts a dot on **every** vertex, recolouring only the **two** picked
endpoint dots orange. There is no highlight of the picked *span* and no dimming
of the rest of the board.

Consequence reported by the user: "I don't know whether I selected anything
before pressing `[F]`." The two-vertex pick can also **silently fail**
(`select_tool.py:_range_picked:2208` — fewer than 2 `WireVertexSelectable`, or
the same vertex twice → `_wire_edit_clear()` returns with **no status message**),
so `[F]` afterward does nothing because nothing is armed. The missing highlight
masks this, so "flip fragmentu nie działa" is partly a *feedback* failure.

## 1. Goal

Make the selected fragment (or whole wire) unmistakable in wire-edit mode:
- On entering wire-edit, **dim the whole board except the edited wire**.
- Draw a **bright, thick highlight over the picked span** — the segments
  `wire.path[start_idx : end_idx + 1]` — on top of the (un-dimmed) edited wire.
- **One mechanism, span decides:** a *fragment* highlights its sub-span; a
  *whole wire* is just the full-span range (`start_idx=0, end_idx=len-1`), so the
  same code highlights the entire path. No `is_full_span` branch in the drawing.
- On exiting wire-edit, restore full board brightness.

Non-goal for the highlight feature: fixing the flip/pick bugs (tracked as
follow-on work in §5 — done AFTER the highlight so the user can *see* the pick
state while we re-test).

## 2. Architecture — where the visual lives

The dim+bright `HighlightStack` is **whole-item only** (`HighlightEntry.wire_ids`
are whole-wire UUIDs; the renderer dims per whole wire-group and the bright
neon-overlay iterates the entire `wire.path` — no sub-path support). The
wire-edit tool already owns the sub-range geometry (`WireRange` +
`wire.path[start_idx:end_idx+1]`) and its own overlay via the `SelectPreview`
Protocol. Decision (user-approved): **extend the wire-edit overlay**, reuse the
dim mechanism as-is. No new sub-path field in `HighlightEntry`.

### 2.1 Dim via HighlightStack (reused as-is)

On `WireEditTool` activation, push a **transient** entry that dims everything
except the edited wire:

```python
from protoboard.controller.highlight_entry import HighlightEntry, PROFILE_DIMMED_2

entry = HighlightEntry(
    wire_ids=frozenset({edited_wire_id}),   # this wire stays full opacity
    profile=PROFILE_DIMMED_2,               # ~0.12 dim for everything else
    transient=True,
    label="wire-edit",
)
session.highlight_stack.push_transient(entry)
```

`PROFILE_DIMMED_2` is the same strong dim already used by the ctx-menu "Highlight
selection (Dimmed #2)" (`select_tool.py:_menu_highlight_selection`). Because the
edited wire is in `wire_ids`, the renderer keeps it at full opacity while
everything else dims — the overlay's cyan + bright-span then sit on top of a
clearly-visible wire.

### 2.2 Bright span in the overlay

`SelectPreview.show_wire_edit_wire` currently takes `(path, marked: set[int])`
and draws cyan-over-whole-path + a dot per vertex (marked ones orange). Extend it
to also receive the **picked span** as a point slice, and draw a bright thick
polyline over it:

- New signature (or added param): the overlay receives the **span points**
  `wire.path[start_idx : end_idx + 1]` (computed tool-side from the `WireRange`),
  in addition to the whole path and marked indices.
- The Qt impl (`_WireEditOverlay.show_wire`) draws:
  1. the cyan context line over the whole path (as today, Z=9.0),
  2. **NEW:** a bright thick polyline over the span points — use the theme's
     bright highlight colour (mirror `_redraw_selected_wire_overlays`'s neon
     yellow) at a wider pen than the cyan, at a Z just above the cyan (e.g. 9.3),
  3. the vertex dots (as today, Z=10.0), endpoints orange.

Whole-wire: the span slice equals the whole path → the bright polyline covers the
entire wire. Fragment: the slice is the sub-range → bright polyline covers only
the picked segments. Same code path.

### 2.3 Data flow (tool side)

`WireEditTool._draw_wire_edit_overlay` (`wire_edit_tool.py:805-827`) already holds
`self._wire_edit` (the `WireRange`). Today it builds `marked = {wire_id:
{start_idx, end_idx}}` and calls `preview.show_wire_edit_wire(path, marked)`.
Change: compute the span slice for the anchored same-wire range
(`wire = _wire_by_id(range.start_wire_id)`;
`span = wire.path[range.start_idx : range.end_idx + 1]`) and pass it through. For
a **cross-wire** range (`range.is_cross`) there is no single-wire span — pass an
empty span (fall back to today's cyan+dots only); cross-wire highlight is out of
scope (see §5).

## 3. Dim lifecycle (correctness)

The transient dim MUST be popped on **every** exit from wire-edit, or the board
stays dimmed afterwards. One `push_transient` on enter, one `pop_transient` on
exit; **do not** re-push on re-arm (would stack entries).

- **Enter** (WireEditTool becomes active, first overlay draw): push once. Guard
  with a boolean/token (`_dim_active`) so re-entrancy can't double-push.
- **`_commit_and_rearm`** / `_rearm_full_span`: dim stays (still editing); only
  the overlay refreshes on the new span. No push/pop.
- **Re-pick after a cut** (`_awaiting_repick`): dim stays; re-arm on the new
  range. No push/pop.
- **`_exit()`** (`[Esc]`, delete-to-empty, return-to-SelectTool): pop the dim.
  This is the single choke point — `_exit` calls `_wire_edit_clear` +
  `_return_to`; pop there so all exit paths are covered.

Idempotency: `pop_transient` only if `_dim_active`; set the flag on push, clear on
pop. If the wire vanished mid-edit (`_rearm_full_span` finds no wire → `_exit`),
the pop in `_exit` still fires.

## 4. Silent-pick feedback (small, supports diagnosis)

`select_tool._range_picked` clears silently on a failed pick. Add a status hint on
the failed-pick branches so the user sees *why* nothing armed:

- `< 2` vertices, or `start_idx == end_idx` → `_status_msg("Wire edit: pick two
  different vertices on one wire.")` before `_wire_edit_clear(); return`.

This is a cheap feedback win and directly supports re-testing "flip nie działa"
(a failed pick is now visible both by the missing dim/highlight AND a message).

## 5. Deferred / follow-on (AFTER the highlight ships)

Done separately so the user can *see* the pick state while we re-test:
- **Re-test flip in the real GUI** with the highlight visible — confirm whether
  "flip nie działa" is silent-pick-failure (now visible) or a real flip bug.
- **Re-arm sub-range bug** (found during investigation): after `[F]` on a picked
  sub-range, `_commit_and_rearm` → `_rearm_full_span` re-arms on the WHOLE wire,
  so a second `[F]` flips the entire wire instead of toggling the fragment.
  Candidate fix: preserve the picked sub-range on re-arm for flip (a side-swap
  does not change path/vertex-count, so the indices stay valid). Confirm intended
  behavior with the user before fixing.
- **Cross-wire span highlight** (the bright span for a cross-wire range) — out of
  scope; cross-wire keeps today's cyan+dots.

## 6. Testing (TDD)

Highlight (this feature):
1. **Dim pushed on enter** — activating `WireEditTool` on a wire pushes exactly
   one transient `HighlightEntry` with the edited wire in `wire_ids` and
   `PROFILE_DIMMED_2`. Assert `session.highlight_stack` has the transient entry.
2. **Dim popped on exit** — `_exit()` (via `[Esc]` / return-to-select) pops it;
   the transient tier is empty afterward. No leftover dim.
3. **Dim survives re-arm** — after `_commit_and_rearm` (e.g. a flip), the
   transient dim entry is still present exactly once (not stacked, not dropped).
4. **Span slice** — for a picked sub-range `[start_idx, end_idx]`, the overlay is
   handed `wire.path[start_idx:end_idx+1]` (assert via a fake/spy SelectPreview
   capturing the span argument). For a full-span range, the span equals the whole
   path.
5. **Cross-wire falls back** — a cross-wire range passes an empty span (no crash,
   cyan+dots only).

Silent-pick feedback:
6. **Failed pick shows a message** — `_range_picked` with <2 vertices or a
   same-vertex pick emits the status hint and does not arm.

Test the tool-level behavior with the headless/GUI harness used by
`tests/gui/test_wire_edit_tool.py` and `tests/gui/test_select_tool.py`
(`_wire_session`, `_v2`, `_key`, `_click`, `_wire_edit_after_handoff`). Assert the
real key/click path where a tool entry point is involved (lesson: test the
key/click path, not just the method).

## 7. Scope / non-goals

- Do not add a sub-path field to `HighlightEntry` or the core renderer (§2).
- Do not fix the flip/re-arm bugs in this feature (deferred, §5).
- Cross-wire span highlight deferred (§5).
