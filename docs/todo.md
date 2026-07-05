# TODO

Open follow-ups that don't fit into the closed plans archived under
[docs/archive/](archive/).  Each entry has enough context to pick up
the work cold.  Completed entries are pruned (2026-07-04) — recover
their full write-ups from this file's git history.

## Connector pin-name presets → generator config (added 2026-07-06)

The parametric-connector rewrite (variable pin count chosen at placement;
`library/Connectors/headers.yaml`) deliberately DROPPED the authored
`variants:` pin-name presets the fixed-count headers carried (UART/I2C/SPI/SWD
labellings on `PinHeader_1x03` / `_1x04` — recover the data from this file's
git history at the rewrite commit). Decision: presets belong to the
**generator** — equip `PackageGenerator` (pin_header first) with a per-pin-count
preset configuration the concretization copies into the synthesized
definition's `variants`, so the pin-variant picker works again. Related: the
planned `idc` kind should join `pin_header`/`jst` when added.

## Kind generators — remaining edge (added 2026-07-04)

The pluggable generator registry is feature-complete (partial kinds in
`footprints:` options, the multi-step chain driver, per-kind recent-variant
history — all shipped 2026-07-04; see package-model.md §6). One edge left:

- **`auto` resolution inside the *builtin* procedural builders** — the DIP/SIL
  /connector generators do not consult `ctx.schema` yet; an unresolved `auto`
  on the YAML families path is still rejected downstream by `_require_float`
  (the intended failure). The chain machinery supports it — wiring a builtin
  compute step (e.g. DIP `length` from `pins_per_row`) is the remaining piece
  (also listed under "Package parameter schema" below).

## Bendable leads — deferred extension (added 2026-06-26)

The four-phase bendable-leads / per-pin re-drag work is complete (full spec in
[docs/bendable-leads-redrag.md](bendable-leads-redrag.md)). One extension was
deferred: a **bendable grab that also translates the body** — today grabbing a
bendable pin is a pure single-lead bend (body fixed); letting the body absorb
part of the motion needs an "extra range" knob in the package definition.

Also tracked from that work (dropped heuristic, per CLAUDE.md): the old
auto-pick forced **pin 1** as the re-drag target on a vertically-flipped 2-pin
part. Under click-a-pin the user picks explicitly; if a flipped 2-pin ever
needs a particular default grab, re-add it in `begin_redrag`'s pin-resolution,
not as an auto-pick.

## Generic-package invariant — follow-ups (added 2026-06-25)

`ComponentDefinition.package` is an invariant: the loader synthesizes a
NON-rigid generic bbox package (`kind="_generic"`, `synth_generic_package` in
`domain/components/package/kind.py`) for parts that declare none. Bend-spec
(`reach.effective_bend_specs`), DRC (`drc/engine.py` ×3) and the footprint
picker (`gui/footprint_choice.py`, `generated_families._explicit_body`) exclude
the `_generic` tag so their behaviour is frozen. Two follow-ups remain:

1. **Body-mask bent-span difference.** `pad_state.add_component` always uses
   `_package_body_pads(defn.package, …)` → `body_holes`, computed from the
   package's **default** (unbent) pin positions. The removed legacy path used
   `component_bbox(defn.footprint, placement, …)`, whose `pin_world_position`
   includes `placement.pin_offsets` (the bent-lead span). For a part placed at
   default pitch the two match exactly; for a **bent** lead the synth marks
   fewer body holes (only the default span + 0.5 margin). Routing impact is nil
   (these flexible parts have `body_blocks_routing=False`, so their `ComponentBody`
   is non-blocking) — but anything that *counts* `ComponentBody` items under a
   bent lead sees a difference. Revisit if a consumer needs the bent-span body.

2. **Consolidate the `_generic` exclusion.** `is_generic_package(pkg)` now
   appears in ~6 sites (reach ×2, DRC ×3, picker). Once authored
   `bendable_pins` are migrated into real non-rigid packages (a data
   migration), the whole generic fallback + `bendable_pins` field can retire;
   until then, the shared `is_generic_package` predicate stays the single
   source of the check.

## Footprint→body cleanup — remaining parity (added 2026-06-25)

**Generated DIP family body is a uniform-margin bbox, not JEDEC-sized.**
`package/kind._prism_from_footprint` wraps the pin cloud with a fixed
0.5-hole margin for every kind. The old `prism_for_footprint` had a DualRow
branch sizing the DIP body to the measured fit `2.54·R − 1.35` mm and
centring it *between* the rows (legs showing). Restore DIP body dims in the
prism path (or as a `Prism` feature, like the TO-220 tab renderer plugin).
xfail: `tests/unit/test_generated_families.py::TestDipFamily::`
`test_body_width_matches_measured_mm` and `::test_body_sits_between_rows_not_over_pins`.

## Pin-name labels never rotate (surveyed 2026-07-03) — user-deferred

Labels use `ItemIgnoresTransformations` (`component_renderer.py`) so they draw
screen-axis-aligned regardless of placement rotation; a rotated devboard's pin
names overlap.  DEFERRED by user decision ("later") — do not start unprompted.

## Named standard sizes as kind presets (added 2026-06-21)

Predefined standard sizes as `kind`-with-dimensions (e.g. `kind: tht-din`,
size `0207/typ`) sourced from norms/catalogues — the generator already builds
cylinder/prism bodies from `dims`, so this is adding named size presets, not
new geometry.

## Package parameter schema — deferred edges (2026-06-27)

Phase 1 (declarations under `schema:` + loose validation) and a limited
Phase 2 (`build_kind_package(kind, ctx, schema) -> GeneratorContext`) shipped.
Deferred (chain driver is Kind-generators item 2 above):

- **`auto` resolution *through* generators** — the procedural builders do
  not consult `schema` or resolve `auto`; an unresolved `auto` on the YAML path
  is rejected downstream by `_require_float` (the intended failure). Wiring a
  kind generator to compute `auto` dims (e.g. DIP `length` from `pins_per_row`)
  is the next step.
- **Strict schema mode** — an unknown field (no schema leaf) is currently *not*
  an error (loose). A strict mode (unknown ⇒ `PackageDefError`) would need every
  optional field declared in every base first.
- **Place-tool picker** — the Properties panel already consumes the schema
  (`properties_panel.py`, one row per `schema` leaf via `fields_view`;
  generator-derived fields read-only). The remaining piece is the place-tool
  package picker sharing the same field-list widget (editing a *pending* package
  spec, not a committed instance). See `docs/footprints/package-model.md` §5.6.

## Multi-pin default body + SMD mount labels (added 2026-06-20)

- Library YAML could set a multi-pin part's `package_family_ref` (e.g. an
  ATmega328P defaulting to `wdip-28`) so the picker's "Default" entry resolves
  to a specific width; today an unset default falls through to the synthesized
  narrow body.
- Friendlier SMD mount-combo labels (compass arrows) — kept `(dx, dy)` because
  an arrow needs pin-1 context the widget lacks; sub-hole "floating" positions.

## Selection sync — single-pin precision deferred (2026-06-29)

Bidirectional board↔panel selection sync ships through the `EditorSession` hub
(see [docs/gui.md](gui.md) "Selection sync (session hub)"). The
`selection_changed` / `selection_requested` triple is
`(comp_ids, wire_ids, net_id)` — it carries **no pin number**.

Consequence: a pin click on the board, or a Component-Pin row click in the Nets
panel, selects/lights **all pin rows of that component**, not the single clicked
pin. The component + its net are correct; only per-pin row precision is missing.

To add it later: extend the signal triple with a 4th `pin_number: int | None`
arg (or a `frozenset[PinRef]`), thread it through
`session.request_select_pin` / `notify_selection`, and have
`NetsEditorPanel.sync_selection` select just the matching `f"{cid}:{pin}"` row.
Keep the existing 3-arg slots working (default the pin arg to None).

## HARD RULE — no definition-default geometry for placed components (2026-06-30)

Any definition-default geometry used for a component that is *placed on the
board* is a **critical bug**: a placed component always has a bound instance,
and its real package/footprint is the chosen variant
(`inst.resolved_footprint()` / `pad_conflict.footprint_of(defn, inst)` —
strict; or `resolved_footprint_or_default(defn, inst)` where unbound is
legitimate). Pin *count* is variant-invariant (verified across all builtin
definitions); only pin *world positions* diverge, so geometry reads are the
danger. The 2026-06-30 audit repointed every placed-component site.
**2026-07-04: the `defn.footprint` field was removed entirely** — the default
geometry is `defn.package` (required), and the few legitimate instance-less
consumers call `defn.default_footprint()`, whose name makes the intent
auditable. New code must never call `default_footprint()` with an instance in
scope.

## Group flip — armed FlipMode UX (added 2026-07-04)

`FlipGroupCommand` is wired minimally: Flip on a selected group mirrors it
about the pin centroid with the default `'v'` axis (`_flip_multi`). The
designed full UX is the armed **FlipMode** (sibling to armed Rotate/Move)
with `[v]`/`[h]` axis choice + a pickable axis position — see the group
live-compose plan, Phase 5 notes.

## Dead code — `TranslateAnchoredWiresCommand` (added 2026-07-03)

Superseded by the drag pipeline; no constructor anywhere in `src/`. Listed in
`_INTENTIONALLY_UNREGISTERED` (serialization totality guard) with a reason —
a candidate for deletion in a separate cleanup.
