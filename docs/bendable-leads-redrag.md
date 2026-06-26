# Bendable leads for N-pin parts + per-pin re-drag (design)

Status: **SHIPPED** (2026-06-26). All four phases landed —
phase 1 `aebaf1e` (per-pin reach disc), phase 2 `eacd3f5` (click-a-pin entry),
phase 3 `58d25cd` (rigid-grab body-translate + shared `redrag_feasibility`),
phase 4 (overlay). Captures the agreed reach model and the re-drag interaction;
kept as the canonical reference. One behaviour change from the original sketch is
noted in [todo.md](todo.md): clicking the rigid anchor of an SMD chip
body-translates (the single-movable convenience now applies only to a body
click). Deferred items in §5 remain open.

The gap this closes: the reach model and the re-drag UI both effectively assume
**one movable lead per part**. They work for axial 2-pin parts and the SMD 2-pin
free-neighbour case, but a part with N > 2 bendable leads (e.g. TO-92) cannot
have each lead bent independently by the user. The *data model* already reports
every pin of a TO-92 as bendable (`tests/unit/test_package_reach.py::test_to92_bendable`),
but there is no way to choose *which* lead to bend, and the reach is a single
ring shared by all pins rather than a disc per pin.

Related: [docs/footprints/spec.md](footprints/spec.md),
[docs/armed-rotate-and-subtool-delegation.md](armed-rotate-and-subtool-delegation.md),
[docs/drag-pipeline-architecture.md](drag-pipeline-architecture.md),
[docs/gui.md](gui.md), [docs/geometry.md](geometry.md).

---

## 0. Today's model (what changes)

**Reach is a single ring from the fixed origin, shared by all pins.**

- `PinReach` (`domain/components/package/reach.py:46`) holds `origin: Point`
  (always `[0,0]`), `default: Point` (the pin's home hole), and a `ring:
  OffsetRange` — but the ring is `package.board_offset_range()`, the **same
  object for every pin** (`pin_reaches` loops handing each pin the one `ring`).
- `PinReach.is_reachable` measures distance **from the origin**, not from the
  pin's default.
- `PinBendSpec` (`domain/components/definition.py:75`) — the legacy view the
  consumers read — carries `min_reach` / `max_reach` as a euclidean radius
  **from `placement.position` (the component origin)**:
  `is_reachable_from_origin(origin_world, target_world)` measures
  `hypot(target − origin)`. There is no per-pin centre.

**Re-drag moves exactly one auto-selected pin.**

- `SelectTool.begin_redrag` (`gui/tools/select_tool.py:1741`) →
  `_redraggable_pin` returns **a single pin** — the first movable pin in sorted
  order (special-casing a vertically-flipped 2-pin part). The user cannot pick
  which lead.
- The clicked pin is **not remembered**: hit-test (`gui/selection/hit_test.py`)
  resolves a click to the *component*, never carrying which pin was under the
  cursor into selection state.
- `_build_redrag_command` validates the target with
  `is_reachable_from_origin(pl.position, snapped)` (origin-centred ring) and
  emits a `MoveComponentCommand` that only ever sets `pin_offsets[pin]` — it
  never translates the body.

---

## 1. New reach model — per-pin disc

Reach becomes **per-pin**, a euclidean `(min, max)` **radius measured from that
pin's own default hole** — a disc (annulus when `min > 0`; `min ≈ 0` in
practice, so a filled disc). Each pin carries its own centre and its own range.

- The package definition owns the per-pin ranges. A rigid pin has `max == 0`
  (`is_free()` is False); a bendable pin has `max > 0`.
- Distance is **euclidean in any direction** (a disc, not an along-axis
  segment) — confirmed 2026-06-26. The axial solver still *computes* the
  numbers; the change is the **centre** (pin default, not origin) and the
  **per-pin** storage, not the metric.

This is the substantive change. Concretely:

- `PinReach.is_reachable` / `PinBendSpec.is_reachable_from_origin` must measure
  from the **pin's default world position**, not `placement.position`. The
  pin's default world position is `pin_world_position(footprint, pl, pin)`
  computed with that pin's offset removed (the "baseline" already derived in
  `_build_redrag_command`).
- `pin_reaches` must be free to hand each pin a *different* range. (It may still
  resolve to the same numbers for a symmetric package, but the type stops
  implying "one shared ring".)
- `is_reachable_from_origin` is misnamed under the new model (it is now
  "reachable from the pin's default"). Keep the old method as a thin alias if
  any caller still needs origin-relative semantics during migration, but the
  re-drag path moves to pin-default-relative.

**Note for a future enhancement (out of scope, see §5):** a bendable grab that
wants to also translate the body needs the package definition to declare an
*extra* range beyond the per-pin disc. Deferred — the basic version below treats
a bendable grab as a pure single-lead bend.

---

## 2. The unified feasibility rule

One rule governs every re-drag, 2-pin or N-pin, bendable-grab or rigid-grab:

> A re-drag is legal iff, after the induced transform, **every pin's final hole
> lies within its own reach disc of that pin's default**, and no rigid pin is
> forced off a pad it must occupy.

The "induced transform" depends only on whether the **grabbed** pin is bendable:

| Grabbed pin | Transform | Legal when |
|---|---|---|
| **Bendable** | Pure lead bend: only `pin_offsets[grabbed]` changes; body fixed. | Target ∈ grabbed pin's disc ∩ free pads. (Other pins unaffected.) |
| **Rigid** | Body **translates** so the rigid pin reaches the target; every *other* lead absorbs the same world shift via its `pin_offsets`. | Every other pin's final hole ∈ its own disc, and no other rigid pin leaves its pad. |

This is why it "also works for 2-pin parts": grabbing the rigid end of a 2-pin
axial part slides the body, and the other (bendable) lead flexes to stay on its
pad — a body translation validated by the one bendable lead's disc.

---

## 3. The re-drag interaction (basic version)

Entry is **always click-a-pin**. The silent auto-pick (`_redraggable_pin`) is
**removed** (confirmed: "Replace with pick-a-pin").

1. **Enter re-drag** on the selected component (the existing action / menu).
2. **Choose the grabbed pin:**
   - If the component was selected **by clicking a pin**, that pin is the grab
     target. (Requires remembering the clicked pin — see §4.)
   - **Clicking another pin** of the same component **switches** the grab target.
   - If the component was selected **not via a pin**, drop into an explicit
     *pick-a-pin* sub-step: the user must click a pin first.
3. **Pick the target pad:** click a **free pad**. Snap, then apply §2:
   - grabbed pin **bendable** → bend that lead (set `pin_offsets[grabbed]`);
   - grabbed pin **rigid** → translate the body, re-deriving every other
     bendable pin's offset, validated against each disc.
   - Illegal target (out of disc / would push a rigid pin off, or the rigid-grab
     translation is infeasible) → reject with a status hint; nothing commits.
4. **Commit:** a `MoveComponentCommand`. Bend case sets `pin_offsets`; rigid-grab
   case sets `position` **and** the recomputed `pin_offsets` of the other leads.
   Reuse the existing invert pipeline (`unrotate` + bottom-side X-mirror) per
   affected pin.

**Rigid grabbed pin, others bendable:** this is not an error — it is exactly the
rigid-grab row of §2. "Move the rigid pin to that pad" = "translate the body
within what the other leads' discs allow." (Confirmed 2026-06-26.)

### Overlay (Both)

While the target is being chosen, show **both**:

- **Reachable free pads highlighted** — every free pad the grabbed pin can
  legally land on: its disc ∩ free pads, **intersected with the §2 feasibility
  of the other leads** (a rigid-grab pad is only lit if the induced body
  translation keeps all other pins in their discs). Extends today's
  `_draw_redrag_overlay` green-dot ring.
- **Disc outline + validity tint** — draw the grabbed pin's reach-disc outline
  and tint the ghost / cursor green (legal) or red (illegal) as it moves.

---

## 4. Implementation notes (where the work concentrates)

1. **Model (the real change).** `reach.py` / `PinReach` / `PinBendSpec` /
   `effective_bend_specs`: per-pin disc centred on the pin default, reachability
   measured from the pin default. `is_reachable_from_origin` is renamed /
   re-centred (keep an alias during migration). Solver still supplies the
   numbers.
2. **Carry the clicked pin into selection state.** Hit-test already finds the
   nearest pin within `HIT_RADIUS` (`hit_test.py`); selection must remember it
   so re-drag can default the grab to it. (Today selection keeps only the
   component id.)
3. **Replace `_redraggable_pin` auto-pick** with: (a) use the remembered clicked
   pin if any; (b) otherwise a pick-a-pin sub-step; (c) re-click another pin
   switches the grab.
4. **Rigid-grab translate path is new.** Today's `_build_redrag_command` only
   ever bends a lead. The rigid-grab case must translate `position` and
   re-solve every other bendable pin's offset, validating each against its disc
   — genuinely new logic and the riskiest piece. Build it behind the same
   feasibility predicate the overlay uses, so highlight and commit agree.
5. **Overlay feasibility = commit feasibility.** The lit pads and the
   commit-time legality must call the *same* predicate, or the user can click a
   green pad that then refuses (the kind of silent divergence CLAUDE.md warns
   about).

### Suggested phases

- **Phase 1 — model:** per-pin disc reach + pin-default-relative reachability,
  with tests (TO-92 each pin independently; 2-pin parity unchanged). No UI
  change yet; existing single-pin re-drag keeps working through the adapter.
- **Phase 2 — pin selection:** remember the clicked pin; click-a-pin entry +
  re-click switch; remove auto-pick. Parity test: clicking pin 3 of a TO-92
  then re-drag targets pin 3 (today it targets pin 2).
- **Phase 3 — rigid-grab translate:** the body-translation branch + the unified
  feasibility predicate; 2-pin rigid-end-grab slides body. Tests for the
  all-discs-satisfied / rigid-pin-off-pad rejections.
- **Phase 4 — overlay:** reachable-pad highlight ∩ feasibility + disc outline +
  validity tint, driven by the Phase 3 predicate.

Each phase: green CI + a smoke test before the next (per CLAUDE.md §8). Parity
tests for the current single-pin re-drag survive the swap — repoint them at the
new path before deleting the auto-pick, never in the same commit.

---

## 5. Deferred (explicitly out of scope for the basic version)

- **Bendable grab that also translates the body.** When the grabbed pin *is*
  bendable but the target is outside its disc, a future version could fall back
  to translating the body (other leads absorbing it), gated by an **extra range
  declared in the package definition**. Agreed to ship the basic version first
  (pure single-lead bend on a bendable grab); the package-def "extra range"
  knob is the prerequisite. (Confirmed 2026-06-26.)
- **Along-axis (non-disc) reach.** The metric stays euclidean disc; an
  along-lead-axis range was considered and rejected for this iteration.
