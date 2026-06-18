# TODO

Open follow-ups that don't fit into the closed plans archived under
[docs/archive/](archive/).  Each entry has enough context to pick up
the work cold.

## SelectTool v2 — deferred parity items (after the Faza 6 swap)

The SelectTool rewrite (`gui/tools/select_tool_v2.py`) reached full
integration parity in Faza 6a, with two consciously-deferred gaps:

1. **Selection-filter enforcement.** `set_filter`/`filter` hold the
   `SelectionFilter` state and mirror it to the status-bar widget, but
   `SelectToolV2._filters()` still returns an empty `SelectionFilters()`
   — every kind stays hittable.  Wire the held filter into the hit-test
   (`HitTester.hit(..., filters)`) so components/wires/groups can be
   excluded from selection as the legacy tool did.

2. **Wire fragment-flip.** `_flip_wire_fragment` is a status-bar stub.
   The whole-wire layer move (`_move_selected_wires_to_side`) works; the
   per-fragment flip (legacy 140 LoC of via-endpoint + alien-net + target-
   layer-overlap validation via `FlipWireFragmentCommand`) was out of the
   Phase-4 wire-edit op set.  Re-implement it against the `WireEditState`
   model and add it as a wire-edit op-table entry (alongside reroute /
   autoroute / delete) — `FlipWireFragmentCommand` takes
   `(wire_id, start_idx, end_idx, side)`, which `WireEditState` already
   carries.

## Parametric renderer for DIP / QIL / SIL packages

**Where:** `src/protoboard/gui/renderers/body_shapes.py` —
`SvgDIPBody`, `SvgSILBody`, `SvgQuadDIPBody`.

**Current state.** Three-or-more-pin packages still render through
hand-written SVG generators:

- `SvgSILBody` — used by `body_shape_for` for `SingleRow(n>=3)` when
  the class is not a transistor (headers, R networks, pots, slide
  switches).
- `SvgDIPBody` — used for every `DualRow` footprint (DIP-N ICs).
- `SvgQuadDIPBody` — used for `QuadRow` footprints (WeAct STM32G4
  CoreBoard).

The fourth class — `SvgBodyShape` — is unused dead code (no
`body_shape_for` branch returns it; no test or tool constructs it).
It can be removed independently of the larger refactor and would save
~150 LOC.

**Why this matters.** Two-pin packages now live in a clean parametric
pipeline:

```
TwoPinBodySpec (assets/body_types/*.yaml)
    ↓
TwoPinParametricBody (material palette + class flavour)
```

3+pin packages have no equivalent.  Their dimensions are hard-coded
into the SVG templates / generators (notch radius, slot size, body
margins, pin pitch).  Consequences:

- Material differentiation is impossible (a DIP-8 in plastic vs
  ceramic vs metal-lid looks identical).
- Industry standards (DIP-300/600, SOIC widths, QFP body sizes) are
  not expressed.  The renderer guesses geometry from `n_per_row` and
  `rows_spacing`.
- The package picker built in [B2/B3] only offers TwoPin packages —
  there is no analogous "ATmega328P → DIP-28W vs DIP-28N" dropdown.

**What to do.**

1. Introduce a `MultiPinBodyPackage` model alongside `TwoPinBodyPackage`
   in `src/protoboard/domain/components/`.  Likely fields:

   - `package_id`, `label`, `material`, `pin_count`
   - `body_length_mm`, `body_width_mm`, `body_height_mm`
   - `pin_pitch_mm`, `rows_spacing_mm`
   - shape-specific extras: notch (DIP), tab (TO-220), corner-pin
     marker (QFP)

   Package YAMLs go under `assets/body_types/` (existing flat list)
   or a sibling directory.

2. Map standard footprints to packages:

   - `DualRow(n_per_row=N, rows_spacing=S)` → DIP-{2N} packages
     parameterised by N and S (e.g. DIP-8 narrow vs DIP-8 wide).
   - `SingleRow(n=N)` → SIL-{N} header / R-network shells.
   - `QuadRow` → custom or module-class packages (WeAct CoreBoard).

3. Replace the SVG generators with a single
   `MultiPinParametricBody(spec, component_class, …)` analogous to
   `TwoPinParametricBody`:

   - Body silhouette (rectangle / rounded rectangle), corner radius
     from `edge`.
   - Class flavour: pin-1 notch (DIP), polarity marker if `polar`,
     metal tab (TO-220).
   - Material palette from `Theme.material_palette`.

4. Extend the picker UI (`LibraryPanel` + `PropertiesPanel`) so
   multi-pin parts also expose a body-variant dropdown.

5. Remove the SVG generators (`SvgBodyShape`, `SvgDIPBody`,
   `SvgSILBody`, `SvgQuadDIPBody`) and their tests.

**Quick win (independent of the larger refactor).** Remove
`SvgBodyShape` — it is fully unused.  ~150 LOC saved, no test
changes needed, no behaviour change in shipped renderings.
