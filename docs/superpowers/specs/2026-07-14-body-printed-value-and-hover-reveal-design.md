<!-- SPDX-License-Identifier: MIT -->
# Body-printed component value + hover-reveal for hidden layers

**Date:** 2026-07-14
**Status:** Approved (brainstorming complete)
**Author:** Adam Szaj (with Claude Code)

## Goal

Two related component-labelling features:

1. **Body-printed value.** Render the component's `value` field *on its body*
   (top of the body renderer's Z-stack), scaled to the package and legible on
   any body colour, for most components — not just SMD resistors as today. For
   R/L/C the printed text is a code (digit-code); for everything else it is the
   raw `value` (which, for TO92/TO220/DIP-N/… concrete parts, is the part
   number).

2. **Hover-reveal when a layer is hidden.** When the `labels` layer is hidden,
   dwelling ~1 s over a component shows its `refdes value` in a tooltip at the
   cursor. When the `pin_names` layer is hidden, hover **+ Alt** renders that one
   component's pin names exactly as the pin-names layer would — a per-component
   visibility override, *not* a tooltip.

## Context — what exists today (verified 2026-07-14)

- **Body-printed SMD code:** `SmdChipRenderer` in
  `gui/renderers/packages/prism_kinds.py:106-154` prints a digit-code on the
  chip body. Text colour is a hardcoded class constant `CODE_COLOR = "#e8e8e8"`
  (`prism_kinds.py:74`). Text is a `QGraphicsSimpleTextItem` scaled
  *geometrically* to fit between the terminal caps (`scale = min(avail_len /
  br.width(), body_w * 0.75 / br.height())`), rotated along the pad axis, placed
  above the caps in Z. Only `RESISTOR`/`INDUCTOR` decode a code
  (`gui/renderers/value_codes.py:91-102`; `smd_code()` produces `22k`→`223`,
  `4.7`→`4R7`).
- **refdes/value overlay (separate mechanism, unchanged by this work):**
  `component_renderer.py:608-614` draws a floating `"R5 4.7k"` label above pin 1,
  zoom-independent monospace font, colour `theme.refdes_color`, gated by the
  `labels` layer. **This overlay stays as-is.** The new body-print is a third,
  independent thing living inside the body renderer.
- **`value` field:** `ComponentInstance.value: str`
  (`domain/components/instance.py:43`). There is **no** separate `part_number`
  field — `value` is the single field for both roles. `''`/`'?'` denote
  "unset" (generic templates seed `default_value: "?"`,
  `definition.py:238`).
- **Body base colour:** resolved by `_resolve_colors()`
  (`gui/renderers/packages/base.py:459-479`), precedence: instance
  `appearance.color` → package `.color` → theme `comp_fill_color`. Returns
  `body_fill: QColor`. **This is the input to the contrast algorithm.**
- **Layers:** `LayerOverrides` (`gui/layer_state.py:114-133`) has independent
  `labels` and `pin_names` toggles (`LayerVisibility` = FULL/GHOST/HIDDEN).
  Renderers gate via `alpha_for_label(side)` (`:229`) and
  `alpha_for_pin_names(side)` (`:269`); `alpha == 0.0` ⇒ don't draw.
- **Pin-name rendering:** `component_renderer.py:617-636`, per pin, gated by
  `alpha_for_pin_names`, `ItemIgnoresTransformations` font, colour
  `theme.pin_label_fallback_color`.
- **Hover:** `tool_host.py:88-90` has a **33 ms coalescing throttle**
  (`ParkLatestThrottle`), *not* a dwell timer. `_fire_hover`
  (`tool_host.py:462-468`) calls `tool.empty_action(point, mods)` → returns a
  `HoverEvent` (`controller/ports/hover.py:22-33`, carries `point` + `objects`,
  **no mods**) and emits `board_view.hover_published`
  (`board_view.py:83,317-319`). **`hover_published` has no subscriber; there is
  no ~1 s dwell timer and no pointer-tooltip widget for board items.**
- **Timer discipline:** all GUI timers go through `gui/throttle.py`
  (`TimerWrap` / `ParkLatestThrottle`) per the timer-consolidation convention —
  no raw `QTimer`.

## Feature 1 — body-printed value

### Units (layered per CLAUDE.md §3)

1. **`value_text.py` (pure, domain-side) — *what* to print.**
   `body_print_text(component_class, value) -> str | None`
   - `RESISTOR` / `INDUCTOR` / `CAPACITOR` → coded string.
     - R/L: reuse existing `smd_code()` (ohms / µH).
     - C: **new** EIA 3-digit code in pF (e.g. `100 nF` → `104`). Implemented
       alongside the R/L path in `value_codes.py`. See "Capacitor coding" below.
   - anything else → the raw `value` string (part number for concrete parts).
   - `value` is `''` or `'?'` → return `None` (caller draws nothing).
   Pure: no Qt, unit-testable headless.

2. **`text_contrast.py` (pure, math) — *what colour*.**
   `best_ink(body_color: QColor) -> QColor`
   - `lum = wcag_relative_luminance(body_color)` over sRGB channels:
     `chan_lin(c) = c/12.92 if c ≤ 0.03928 else ((c+0.055)/1.055) ** 2.4`
     (c in 0..1); `L = 0.2126·R + 0.7152·G + 0.0722·B`.
   - palette = `DARK_INKS` if `lum > 0.5` else `LIGHT_INKS`.
     - `DARK_INKS` (for light bodies): `#000000` black, `#2b2b2b` graphite,
       `#0d1b3e` navy.
     - `LIGHT_INKS` (for dark bodies): `#ffffff` white, `#c8c8c8` light-grey,
       `#ffd400` yellow.
   - return `max(palette, key=lambda ink: contrast_ratio(lum, lum(ink)))` where
     `contrast_ratio(a, b) = (max(a,b)+0.05)/(min(a,b)+0.05)`.
   - The **0.5 luminance threshold picks the palette**; **contrast picks the ink
     within it**. Two-stage, correctness-by-construction: a dark body can never
     receive a dark ink regardless of arithmetic.
   - Palettes are plain module-level constant lists of **hex strings** — tunable
     in one line (e.g. drop yellow if it fires too eagerly on mid-dark bodies).
     The core math works on `(r, g, b)` floats; the palette hex strings are
     converted only where a candidate is compared. `best_ink` accepts and
     returns `QColor` at the boundary (it is a plain value type), but the
     luminance/contrast functions themselves take `(r,g,b)` tuples so they are
     importable and testable with zero Qt.

3. **`body_label.py` (gui) — *how* to draw.**
   `make_body_label(text, ink, body_rect, *, alpha, ...) ->
   QGraphicsSimpleTextItem` — geometric scale-to-fit (generalising
   `SmdChipRenderer._code_label`), placed at top of Z. `ink` alpha multiplied by
   `paint.alpha` (ghost-layer parity with today's `CODE_COLOR`). Shared by all
   body renderers.

### Wiring

- Each body renderer, after drawing its body, calls:
  `text = body_print_text(component_class, instance.value)`; if not `None`,
  `ink = best_ink(body_fill)` (the `body_fill` from `_resolve_colors()`),
  then `make_body_label(text, ink, body_rect, alpha=paint.alpha)`.
- `SmdChipRenderer` is refactored to route through the shared helper (its
  bespoke `_code_label` + hardcoded `CODE_COLOR` are replaced by
  `best_ink` + `make_body_label`), preserving its current geometry/rotation.
- **Scope:** any component with a body to print on **and** a non-empty printable
  string (`body_print_text` returned non-`None`). Pin-headers / bare connectors
  without a solid body naturally have no body renderer to host the label, so
  they fall out without an explicit blocklist.

### Capacitor coding

`CAPACITOR` gets a 3-digit EIA code in pF (`smd_code`-style: two significant
digits + decimal-exponent), e.g. `100 nF`=`100000 pF`→`104`, `22 pF`→`220`,
`4.7 pF`→`4R7`. Colour-code for capacitors is **not** implemented (YAGNI —
modern SMD/through-hole caps do not use band colour-codes). Tracked in
`docs/todo.md`.

## Feature 2 — hover-reveal for hidden layers

Two **different** mechanisms sharing one dwell/hover pipeline.

### Shared pipeline

- **Dwell timer:** `TimerWrap(1000, on_dwell, restart_on_arm=True)` — a
  trailing-edge debounce: every mouse move re-arms it; it fires only after ~1 s
  of *stillness*. Reuses `gui/throttle.py`; no raw `QTimer`. Armed from
  `_fire_hover` (which already holds `point`, `mods`, and the hovered component
  id).
- **`HoverEvent` gains `mods`.** Today it carries only `point` + `objects`;
  `_fire_hover` already has `mods` in hand (`tool_host.py:467`). Add `mods` (and
  ensure the hovered component id is available) to the Qt-free port so the dwell
  handler can branch on Alt. `SelectTool.empty_action` already resolves the
  component under the cursor.
- **`hover_published` gets a subscriber** (currently none) — the dwell handler
  below.
- Any move (re-arm) or a change of hovered component clears whatever is showing
  and recomputes on the next dwell; leave clears it.

### 2A — `labels` hidden ⇒ tooltip at cursor

- Condition: `alpha_for_label == 0` (labels HIDDEN) **and** a component under
  the cursor **and** Alt **not** held.
- On dwell: show a small pointer-anchored widget (`HoverTooltip`, new, in
  `gui/`) with **`refdes value`** (e.g. `R5 4.7k`) — the same string the
  hidden refdes/value overlay would show. The string is built by a pure helper
  (`refdes` + `value`), rendered by the gui widget.
- It is a single string, so a tooltip is the right form.

### 2B — `pin_names` hidden + Alt ⇒ per-component visibility override

- Condition: `alpha_for_pin_names == 0` (pin_names HIDDEN) **and** a component
  under the cursor **and** Alt **held**.
- On dwell: render **that one component's** pin names exactly as the `pin_names`
  layer would (same font/colour/position, `component_renderer.py:617-636`) — as
  if the layer were switched on for it alone. **Not** a tooltip: pin names are
  spatial (each name belongs above a specific pin) and lose meaning if extracted
  to a floating box.
- Mechanism: a **hovered-component override** carried in `LayerState`. The scene
  refresh treats `alpha_for_pin_names` as FULL for the hovered `cid` while the
  layer is globally HIDDEN. The renderer asks for the *effective* alpha
  (global + hovered-override) per component.
- Clearing: change of hovered `cid`, release of Alt, move, or leave clears the
  override and triggers a refresh.
- **Cost (accepted trade-off):** the override forces a scene refresh on
  enter/exit of a component in Alt-hover mode. This only happens while Alt is
  held and after the 1 s dwell, so the frequency is low. Documented as a
  conscious cost.

### Layer-state single source of truth

Both 2A and 2B read the same `LayerState` (`alpha_for_label == 0` /
`alpha_for_pin_names == 0`) that gates the static rendering, so the reveal
appears exactly when the static text disappeared — consistency guaranteed by one
source of truth.

### Boundary note

Decision policy ("which reveal, what text") is controller-friendly and pure
where practical (string building, layer-visibility predicate); the `QWidget`
tooltip and the `LayerState` override read/refresh are Qt and live in `gui/`.

## Testing strategy

- **`value_text`** (pure): R/L/C code cases, part-number pass-through,
  `''`/`'?'` → `None`. Headless.
- **`text_contrast`** (pure): known-luminance bodies pick the expected palette;
  best-contrast ink chosen; white body → dark ink, black body → light ink,
  saturated blue body → light ink (luminance, not hue). Headless.
- **`body_label`** (gui): produces a scaled item within the body rect;
  `SmdChipRenderer` parity — same geometry/rotation as before the refactor.
- **Feature-2 dwell:** timer fires after stillness, re-arms on move; `labels`
  hidden + no-Alt → tooltip string `refdes value`; `pin_names` hidden + Alt →
  hovered-override makes that component's pin names render while globally hidden;
  clears on move/leave/Alt-release/cid-change.
- **Layer parity:** with layers visible, nothing new triggers (no tooltip, no
  override) — reveal is strictly a hidden-layer affordance.
- **Smoke:** `run-protoboard-designer` headless driver still renders.

## Non-goals / YAGNI

- No capacitor colour-code.
- No new `part_number` field — `value` stays the single field.
- The refdes/value overlay above pin 1 is unchanged.
- No fix for rotated pin-name overlap (`docs/todo.md` deferred item).

## Follow-ups → `docs/todo.md`

- Capacitor colour-code (dropped as YAGNI; revisit only if a real need appears).
- Future `short-spec` / `short-description` instance field: once components
  carry richer spec data, the 2A tooltip should show that instead of bare
  `refdes value`.
- Yellow-ink tuning: if `#ffd400` fires too eagerly on mid-dark bodies, drop it
  from `LIGHT_INKS`.

## Success criteria

1. Most components render their `value` on the body (R/L/C coded, rest raw part
   number); empty/`'?'` prints nothing.
2. Body-print text colour is chosen automatically from body luminance:
   light body → dark ink, dark body → light ink, best-contrast within the
   palette. Legible on every body colour.
3. `SmdChipRenderer` routes through the shared helper; its on-body code looks
   the same (geometry/rotation) but colour is now luminance-driven.
4. `labels` hidden + ~1 s dwell → `refdes value` tooltip at cursor; clears on
   move/leave.
5. `pin_names` hidden + Alt-hover → that component's pin names render as if the
   layer were on for it alone; clears on Alt-release/move/leave/cid-change.
6. With layers visible, no reveal triggers.
7. Pure units (`value_text`, `text_contrast`) are headless-testable; only
   drawing/tooltip/override touch Qt.
8. Full suite green; smoke passes.
