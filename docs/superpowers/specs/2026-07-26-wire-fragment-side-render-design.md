# Wire Fragment Per-Side Rendering — Design

Date: 2026-07-26
Branch: `main` (fixes landed as `ed796fb`; this renderer work is planned)
Related: `project_wire_side_stale_snapshot_2026_07_25` (Wire.side stale snapshot),
`project_wire_edit_fragment_highlight_2026_07_25` (fragment highlight),
`feedback_ship_features_not_endless_refactor`, the "Rewrites and swaps" rule in CLAUDE.md §8.

## 0. Problem

Flipping a wire fragment to the opposite layer (`[F]` in wire-edit) is **invisible**
even though the model and persistence are correct. Root-caused via
systematic-debugging with three independent confirmations:

1. **The saved model is correct.** After a flip, `layout.yaml` has a wire whose
   `fragments` carry mixed sides (`top` / `bottom` / `top`), and
   `session_commands.yaml` records exactly one `flip_wire_fragment` command
   (dispatch fixed in `ed796fb`).
2. **The renderer never reads per-fragment side.** `scene_composer._render_wires`
   (`scene_composer.py:316`) calls the **v1** `update_wire_groups`
   (`wire_renderer.py:99`), whose builder `_build_wire_group` paints the whole
   wire using the scalar `wire.side` — which is frozen once in
   `Wire.__post_init__` from `fragments[0].side` and never recomputed after a
   flip.
3. **The fragment-aware renderer is dead code.** `update_wire_groups_v2`
   (`wire_renderer.py:266`) + `_build_wire_group_v2` (`:332`) iterate
   `wire.fragments`, apply per-fragment `alpha_for_wire(frag.side)` /
   `z_for_wire(frag.side)`, and draw **via markers** at side transitions — but
   nothing in the codebase calls `update_wire_groups_v2` (grep-confirmed).

A headless render of the flipped wire vs. an all-top wire produced a **pixel diff
of 0** — top and bottom fragments are currently indistinguishable in every
project, regardless of theme.

Secondary issue: in the affected project's `layers.yaml`, `wires_top: ghost`
(→ alpha 0.89) and `wires_bottom: full` (→ back_alpha 0.90) collide, so even the
correct per-side renderer would produce a barely-perceptible 0.01 alpha delta.
The user wants a **clearly distinguishable** bottom fragment.

## 1. Goal

- `scene_composer` renders wires through the **fragment-aware** path, so a
  flipped fragment renders on its own side (per-fragment alpha + Z) with a via
  marker at each side transition.
- A bottom-side fragment is **clearly visually distinct** from a top-side one —
  not a 0.01 alpha whisper. The exact visual (stronger dim, distinct hue/tint,
  or dashed/patterned stroke) is chosen in the plan; the requirement is
  "unmistakable at a glance", honoring the user's color-vision accessibility
  (high-contrast, not color-only — see `user_color_vision_accessibility`).
- **No routing-glow regression.** v1's `routing_target_net_id` (glow on the
  actively-routed net) must survive the swap — v2 currently lacks that param.

Non-goals: reworking the whole layer/ghost model; changing v1's behavior for
callers other than `scene_composer` (there are none in `tests/` — grep-confirmed,
so this is a clean swap with no parity tests to repoint, but see §4).

## 2. Architecture — the swap

`scene_composer._render_wires` calls `update_wire_groups` → switch to
`update_wire_groups_v2`. v2 already has: per-fragment side/alpha/Z, glow,
insulated/bare, bold, via markers. It lacks only `routing_target_net_id`.

Two work items:
1. **Extend v2** to accept `routing_target_net_id` and use it to drive `glow`
   exactly as v1 does (`net_id == routing_target_net_id` → glow=True), so the
   swap loses no behavior.
2. **Repoint** `scene_composer._render_wires` to v2, passing
   `routing_target_net_id=session.routing_net_id` (the arg it already computes).

Then **retire v1** (`update_wire_groups` + `_build_wire_group` + their
fingerprint helper) ONLY once v2 is the sole path and green — or keep v1 briefly
behind the swap and delete in a follow-up commit. Prefer deleting in the same
change to avoid a second dead-code path, per §4.

## 3. Making bottom clearly distinct (the visibility half)

Per-side alpha alone is too subtle in this project. Options (plan picks one,
validates in a real render):

- **A. Stronger back dim for wires.** Give back-side wire fragments a distinctly
  lower alpha than the ghost front (e.g. a dedicated `wire_back_alpha` well below
  `back_alpha`), so a bottom fragment reads as clearly recessed.
- **B. Distinct stroke pattern.** Render back-side fragments with a dashed or
  double-stroked core (pattern, not just color) — robust for color-vision
  accessibility.
- **C. Via markers as the primary signal.** Ensure via markers at transitions
  are prominent (radius/colour/contrast) so the *boundary* is unmistakable even
  if the fill delta is subtle.

Recommendation: **A + C together** — a real alpha gap AND prominent via markers,
both non-color-only cues. B is a fallback if A+C still reads ambiguously.
Whatever is chosen, the plan MUST validate with a headless render (pixel diff
> 0 and eyes-on) that a mixed-side wire is visibly different from an all-top one.

## 4. Rewrites-and-swaps discipline (CLAUDE.md §8)

- **No tests currently pin v1 or v2** (grep of `tests/` for `update_wire_groups`
  is empty). So there are no behavior tests to repoint — but that also means v2
  is UNTESTED. The plan MUST add tests FIRST (TDD): a test asserting a mixed-side
  wire's rendered group has fragments at two different alphas + a via marker,
  and a test that routing-glow still fires post-swap. These become the parity
  guard the swap lacked.
- The via-marker + per-side behavior is the polished functionality that must not
  silently regress; it only exists in v2, so the swap ADDS it — assert it.
- Delete v1 in the same change (or a tracked follow-up) so there is exactly one
  wire-render path afterward — no new dead code.

## 5. Testing (TDD)

1. **Mixed-side render** — build a `Wire` with `fragments = [top, bottom, top]`,
   run the wire-render path, assert the resulting `QGraphicsItemGroup` contains
   line items at ≥2 distinct opacities/Z (top vs bottom) AND ≥1 via marker
   (`QGraphicsPathItem`/ellipse) at a transition. (RED against v1 / pre-swap.)
2. **All-top vs mixed differ** — a headless pixel-diff (or item-level opacity
   set) proving the mixed wire is not identical to the all-top wire.
3. **Routing glow survives** — with `routing_target_net_id` = the wire's net,
   glow items are present post-swap (guards the v2 extension).
4. **No regression** — full `tests/gui` suite green; the app smoke (real
   MainWindow, the affected project) shows the flipped fragment visibly on its
   side with a via marker.

## 6. Scope / non-goals

- Do NOT change `Wire.side` semantics (it stays the construction snapshot; the
  renderer simply must not rely on it for per-fragment paint).
- Do NOT touch the dispatch / preserve-range fixes (already landed, `ed796fb`).
- Camera-flip / mirror behavior unchanged — a bottom fragment with camera on top
  is recessed; flipping the camera still brings it forward (existing behavior).
