# TODO

Open follow-ups that don't fit into the closed plans archived under
[docs/archive/](archive/).  Each entry has enough context to pick up
the work cold.  Completed entries are pruned (2026-07-04) — recover
their full write-ups from this file's git history.

## Qt ports — deferred pieces (added 2026-07-09)

The Qt-ports analysis ([qt-ports.md](qt-ports.md)) is a decision gate; even
if the ports-only refactor proceeds, these pieces are deliberately deferred:


- **`ExecutorPort` residue** (the port itself SHIPPED 2026-07-09:
  `ports/executor.py` + `gui/qt_executor.py` + seam attr + `FakeExecutor`;
  SelectTool `_simplify_wires`/`_unweave_wires` converted, `_view_widget()`
  deleted).  Deliberately left Qt-side: MainWindow's 3 direct
  `run_with_progress` calls (Qt consumer); async AutorouteWorker/DrcWorker;
  SelectTool's `_menu_autoroute_group` reaching `window()._group_autoroute`
  (async launch from behind the seam — separate design: completion-callback
  job or a structural notification to MainWindow).
- ~~**Semantic per-tool preview objects**~~ SHIPPED 2026-07-09 for all four
  tools (`route_preview.py`, `place_preview.py`, `simplify_preview.py`,
  `select_preview.py`; qt-ports.md §3 updated). Only `DragCornerText`'s two
  scene-handle pass-throughs remain in SelectTool (already the pattern).
- ~~**`selectable.draw_highlight -> list[QGraphicsItem]`**~~ REMOVED
  2026-07-09 (user decision): the member never gained an implementation or a
  caller — highlight rendering went the ``HighlightStack`` dimming route
  instead — so it was deleted from the protocol and adapters rather than
  tightened to an opaque handle.

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

## Kind generators — declarative-node follow-ups (updated 2026-07-06)

The pluggable generator registry is feature-complete (partial kinds in
`footprints:` options, the multi-step chain driver, per-kind recent-variant
history — all shipped 2026-07-04; see package-model.md §6). The remaining
edge — **`auto` resolution** — shipped 2026-07-06 as the declarative kind
nodes pilot (`assets/packages/kinds.yaml` + `kind_nodes.py` +
`PackageGenerator.resolve_auto`; DIP kinds only). Follow-ups from that pilot:

- **Migrate the remaining builtin kinds to nodes** — `sil`, `pin_header`
  (its `rows` schema moves to the node's own `schema:` block, becoming the
  first `KIND_DECLARED_SCHEMAS` user in production) and `jst` still build
  through the legacy pure-code path.
- **Rewire the generated `dip-N`/`wdip-N` picker families through
  `build_kind_package`** — `generated_families.dip_family` still builds via
  `prism_from_footprint` directly (geometry identical through the shared
  `dip_body_dims_mm`, but no schema/raw_values/full lineage/color on those
  bodies). Deferred from the pilot 2026-07-06.
- **Delete `DipGenerator.generate` (the node-less fallback)** once the
  generated families above stop calling the legacy path and test registries
  bind nodes everywhere.

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

~~Generated DIP family body is a uniform-margin bbox, not JEDEC-sized.~~
RESTORED 2026-07-06: `dip_body_dims_mm` (generators/dip.py) applies the
measured fit `width = 2.54·R − 1.35` mm / `length = pin span + 2×1.27` mm in
both DIP build paths (`DipGenerator` and the generated `dip-N`/`wdip-N`
families); the two xfails in test_generated_families.py are live asserts
again. Follow-up still open: per-component width override (e.g. ATmega328
at 7.20 mm).

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
`auto` resolution *through* generators shipped 2026-07-06 (declarative kind
nodes — package-model.md §5.4 and §6; an unresolved `auto` is now a loud
`PackageDefError` naming the dotted paths). Still deferred:

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

## MVC verb-layer — deferred follow-ups (added 2026-07-11)

The verb-layer seam shipped step by step (docs/mvc-verb-layer.md, commits
3fcf3c9 → 6a). Deliberately deferred, in order of likely value:

- **State-driven KeyMap-layer swap (planned step 6b, dropped for 6a).**
  Step 6 landed as *emit + record* `controller_state_changed`. The originally
  planned KeyMap-layer swap was rejected after a code audit: armed keys
  dispatch through `SelectTool.on_key` gated on `self._armed` (not a KeyMap
  `lookup`), so a swap is cosmetic for dispatch; and the armed set includes
  `R`/`Alt+R`/`Esc`, so `wants_key`/`key_claims` must stay to veto those global
  shortcuts anyway. Only pursue a state-driven layer where dispatch actually
  flows through the KeyMap (not the armed sub-tool path).
- **Hover-highlight visual.** `hover_published` fires with the hovered ids
  (HoverEvent) but no consumer paints a hover highlight yet; the highlight
  renderer is the natural subscriber.
- **`select.pick` / `select.wire_edit` states.** `ToolController.state`
  currently reports only idle / armed-rotate / armed-move; the pick and
  wire-edit sub-states are not yet surfaced.
- **Inline the LMB logic out of `on_press`.** `default_action` delegates to
  `on_press`, whose `if inp.button != LEFT_BUTTON: return False` guard is now
  dead for the verb path; a later pass can inline the body and drop the guard.
- **Migrate other tools' internals into verbs.** Place/Route/Cut/Simplify still
  receive `on_press`/`on_move` via the default `_Tool` adapters; only SelectTool
  overrides the verbs so far.
