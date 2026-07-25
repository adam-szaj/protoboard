# Wire Fragment Per-Side Rendering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (or executing-plans) to implement task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make a flipped wire fragment visible: render wires through the
fragment-aware path (`update_wire_groups_v2`) so each fragment paints on its own
side with a via marker at transitions, extend v2 to keep routing-glow, and make a
bottom-side fragment clearly distinct (not a 0.01 alpha whisper). Retire the v1
renderer so one path remains.

**Spec:** `docs/superpowers/specs/2026-07-26-wire-fragment-side-render-design.md`

**Architecture:** `scene_composer._render_wires` (`scene_composer.py:316`) calls v1
`update_wire_groups`; switch to `update_wire_groups_v2` after extending v2 with
`routing_target_net_id`. v2 already does per-fragment side + via markers + glow +
insulated/bare + bold. Then delete v1.

**Tech stack:** Python 3.11+, PySide6 (QGraphicsScene), pytest + pytest-qt.

## Global Constraints

- Work in the MAIN checkout on branch `main` (that is where the dispatch fixes
  landed as `ed796fb` and where the user runs the app). Do NOT push; no PR.
- `mypy --strict` on touched SOURCE files; no NEW error category (this tree has
  pre-existing systemic `EditState`/`_ViewProto` debt — not findings).
- Conventional Commits, one per task. `git add -u` + explicit new files; never
  `git add -A`/`.`. Untracked junk to LEAVE: `example.py`, `library/ICs/audio.yaml`.
- Keep `@traced()` decorators added in `ed796fb` — do not remove (repo rule:
  `@traced` decorators are permanent).
- Color-vision accessibility: the bottom-vs-top cue must be high-contrast and NOT
  color-only (a pattern/alpha/marker cue, per `user_color_vision_accessibility`).
- Rewrites-and-swaps: v2 is currently UNTESTED and nothing pins v1 — add the
  parity tests FIRST (Task 1), then swap (Task 2), then make it distinct (Task 3),
  then delete v1 (Task 4). Do not delete v1 before v2 is the sole green path.

Key facts (verified 2026-07-26):
- `scene_composer._render_wires` → `update_wire_groups(...)` at `scene_composer.py:316`,
  passing `routing_target_net_id=session.routing_net_id`. Import at `scene_composer.py:30`.
- v1 `update_wire_groups` (`wire_renderer.py:99`) / `_build_wire_group` (`:183`) —
  paints scalar `wire.side`; NO via markers; HAS `routing_target_net_id` → glow.
- v2 `update_wire_groups_v2` (`:266`) / `_build_wire_group_v2` (`:332`) — per-fragment
  `frag.side` alpha/Z, via markers (`:378-386`), glow/insulated/bare/bold; LACKS
  `routing_target_net_id`. Glow currently driven by `glow=False` hardcoded at the
  call in `update_wire_groups_v2` (`:310`/`:322`).
- `LayerState.alpha_for_wire(side)` (`layer_state.py:218`): `1.0 if side==camera_side
  else back_alpha`, then `_vis_alpha(override, ...)`. Project override `wires_top:
  ghost`≈0.89, `wires_bottom: full`≈0.90 → collide.
- Via marker: `via_marker_color` default `#E0E0E0`, `via_marker_radius` default 0.18
  (`wire_renderer.py:379-380`); `_build_via_marker` (`:392`).
- No `tests/` reference `update_wire_groups`/`_v2` (grep empty) — v2 untested.

---

### Task 1: Parity tests for the fragment-aware render (RED first)

**Files:**
- Test: `tests/gui/test_wire_renderer_fragments.py` (create)

**Interfaces:** Consumes `update_wire_groups_v2` (current signature). Produces
tests that FAIL against the v1 path but PASS against v2, pinning: (a) mixed-side
wire → fragments at ≥2 distinct alphas/Z; (b) via marker present at a transition;
(c) all-top wire → single side, no via marker.

- [ ] **Step 1: Write failing tests.** Build a `Wire` with
  `fragments=[top, bottom, top]` (through the controller so knots exist — mirror
  `tests/gui/test_wire_edit_tool.py::_detour_wire` then flip a sub-range, OR
  construct fragments directly if the Wire API allows). Call `update_wire_groups_v2`
  into a `QGraphicsScene` and assert the built group has line items at two
  different `zValue`s (top vs bottom) and ≥1 via-marker item. Add an all-top
  control asserting one side + zero via markers. Also assert (will guide Task 2)
  that with `routing_target_net_id`=the net, glow items appear — mark this test
  `xfail`/skip until Task 2 adds the param, OR write it in Task 2.
- [ ] **Step 2: Run — confirm the mixed-side asserts PASS on v2** (v2 already does
  per-fragment). If they already pass, these tests are the parity guard for the
  swap; good. If constructing mixed fragments is awkward, resolve here.
- [ ] **Step 3: mypy + commit** `test: pin fragment-aware wire render (per-side + via marker)`.

### Task 2: Extend v2 with routing_target_net_id (glow parity)

**Files:**
- Modify: `src/protoboard/gui/renderers/wire_renderer.py` (`update_wire_groups_v2` +
  the `glow=` it passes to `_build_wire_group_v2`)
- Test: `tests/gui/test_wire_renderer_fragments.py` (add glow test)

- [ ] **Step 1: Failing test** — a wire whose net == `routing_target_net_id`
  renders glow items via v2 (RED: v2 has no such param today).
- [ ] **Step 2: Implement** — add `routing_target_net_id: uuid.UUID | None = None`
  to `update_wire_groups_v2`; compute `glow = (routing_target_net_id is not None and
  net_id == routing_target_net_id)` and pass it in place of the hardcoded
  `glow=False` at both build sites. Include it in the fingerprint so a glow change
  rebuilds. Mirror v1's logic exactly (`wire_renderer.py:99` body).
- [ ] **Step 3: GREEN + mypy + commit** `feat(render): route-glow support in fragment-aware wire renderer`.

### Task 3: Swap scene_composer to v2 + make bottom distinct

**Files:**
- Modify: `src/protoboard/gui/scene_composer.py:30` (import) + `:316` (call)
- Modify: `src/protoboard/gui/layer_state.py` and/or `controller/theme.py` for the
  distinct back-wire cue (per spec §3 — pick A+C: a real `wire_back_alpha` gap +
  prominent via markers). Keep it non-color-only.
- Test: `tests/gui/test_wire_renderer_fragments.py` (assert the alpha GAP is
  perceptible — e.g. |top_alpha − bottom_alpha| ≥ a threshold like 0.3)

- [ ] **Step 1: Failing test** — assert a bottom fragment's effective alpha is
  distinctly lower than top's (threshold), and/or the via marker meets a
  visibility bar (radius/contrast). RED against current 0.89/0.90.
- [ ] **Step 2: Implement** — (a) repoint `_render_wires` to `update_wire_groups_v2`
  passing `routing_target_net_id=session.routing_net_id`; drop the now-unused v1
  import. (b) Introduce the distinct back cue: a dedicated wire back alpha clearly
  below the front, applied in `alpha_for_wire`/a wire-specific helper, without
  regressing component-layer dimming. Ensure via markers are prominent.
- [ ] **Step 3: GREEN + mypy + no-gui guard (n/a, gui file) + commit**
  `fix(render): render wires per-fragment side so flip is visible`.

### Task 4: Retire v1 renderer (one path)

**Files:**
- Modify: `src/protoboard/gui/renderers/wire_renderer.py` (delete `update_wire_groups`,
  `_build_wire_group`, their v1-only fingerprint helper if unused)

- [ ] **Step 1: Confirm no callers** — grep `update_wire_groups\b` (not `_v2`) across
  `src/` and `tests/`; expect only the definition. If a test referenced it, repoint
  to v2 first (do not delete a guarded behavior).
- [ ] **Step 2: Delete v1 + run full `tests/gui`** — green.
- [ ] **Step 3: mypy + commit** `refactor(render): remove dead v1 wire renderer`.

### Task 5: Verify + GUI smoke + docs

- [ ] **Step 1** Full `tests/gui` + relevant `tests/unit` — no NEW failures vs
  baseline (known: debug_trace x2, hit_test, library_loader).
- [ ] **Step 2** GUI smoke via `run-protoboard-designer`: load the affected project
  (`/home/yvdev/Documents/ProtoBoard/GuitarHeadPhoneAmp`) OR flip a fragment in a
  fixture; screenshot; confirm the bottom fragment is visibly recessed/distinct
  with a via marker at the boundary. Read the PNG.
- [ ] **Step 3** CHANGELOG entry + update this plan/spec status. Commit
  `docs: changelog for per-fragment wire side rendering`.

---

## Self-Review

- Spec §2 swap → Task 2 (extend) + Task 3 (repoint) + Task 4 (delete). ✓
- Spec §3 distinct bottom → Task 3 (A+C cue, threshold test). ✓
- Spec §4 rewrites-discipline → Task 1 adds the missing parity tests BEFORE the
  swap; v1 deleted only after v2 is sole green path (Task 4). ✓
- Spec §5 tests → Tasks 1-3 tests + Task 5 smoke. ✓
- Risk: `alpha_for_wire` is shared by other callers — Task 3 must add a
  wire-specific back cue WITHOUT changing label/component dimming (spec §3 note;
  `_side_tool_alpha` vs `alpha_for_wire` split already exists in layer_state.py).
