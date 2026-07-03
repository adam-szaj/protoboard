# TODO

Open follow-ups that don't fit into the closed plans archived under
[docs/archive/](archive/).  Each entry has enough context to pick up
the work cold.

## Generic-catalogue named packages removed — DONE (2026-06-26/27)

`assets/packages/generic.yaml` now carries **only the abstract base shapes**
(`cylinder`, `prism`, `cylinder_axial`, `cylinder_radial`, `prism_dil`, `disc`,
`disc_radial`, `prism_radial`, `prism_smd`).  Those are load-bearing: the
package-family catalogue (`assets/package_families/`) `extends` them and a
missing base fails family loading at startup.

All named (instantiable) packages were removed because every placed part
resolves its body through the family catalogue via `footprints: [ref]` — the
generic copies were dead in production (consumed only by tests):
- `TO-92`, `DO-35` — exact-name duplicates of families that had drifted (TO-92
  generic prism was bendable while the family cylinder was rigid; DO-35 dims
  differed).
- `R_Axial_DIN0207`, `CP_Elec_6.3x11`, `DIP-8` — twins of families under
  different names (`DIN-0207`, `Radial-Elko-D6.3`/`Axial-Elko-*`; DIP families
  are auto-generated).

The package-model tests (`test_package_reach`, `_adapter`, `_builtin_yaml`) were
repointed onto self-contained inline catalogues, so they no longer read named
packages from the file.

## Bendable leads for N-pin parts + per-pin re-drag (added 2026-06-26)

Full spec in [docs/bendable-leads-redrag.md](bendable-leads-redrag.md).
**Phase 1 DONE** `aebaf1e` (per-pin reach disc centred on each pin's default).
**Phase 2 DONE** `eacd3f5` (click-a-pin entry; clicked pin remembered; re-click
switches; pick-a-pin sub-step; `_redraggable_pin` auto-pick removed).
**Phase 3 DONE** `58d25cd` (rigid-grab body-translate + the shared
`redrag_feasibility` predicate in `reach.py`, called by both commit and overlay).
**Phase 4 DONE** (overlay: lit pads = feasibility ∩ board pads via the shared
predicate; reach-disc outline for a bendable grab; live green/red validity tint
under the cursor with an "X" non-colour cue for illegal). **All four phases of
the bendable-leads/per-pin re-drag work are complete.**

Behaviour change in Phase 3 (tracked per CLAUDE.md): clicking the **rigid**
anchor pin of an SMD chip now grabs that pin (a body-translate), not the free
terminal — the old single-movable convenience only applies to a *body* click
now. To re-drag the free terminal, click the terminal. SMD parity tests
repointed accordingly (`test_redrag_arms_for_smd_grabs_clicked_terminal`,
`test_redrag_smd_free_terminal_to_diagonal_pad`).

Dropped in Phase 2 (per CLAUDE.md, tracked here not as an inline comment): the
old `_redraggable_pin` had a special case forcing **pin 1** as the re-drag target
on a vertically-flipped 2-pin part. Under click-a-pin the user picks the pin
explicitly, so the heuristic is gone; no test asserted it. If a flipped 2-pin
needs a particular default grab, re-add it in `begin_redrag`'s pin-resolution,
not as an auto-pick.

Gap: both the reach model and the re-drag UI assume **one movable lead per
part**. The data model already reports every TO-92 pin as bendable
(`tests/unit/test_package_reach.py::test_to92_bendable`), but the reach is one
ring shared by all pins measured **from the component origin**
(`PinReach`/`PinBendSpec.is_reachable_from_origin`,
`reach.py:46` / `definition.py:75`), and `SelectTool._redraggable_pin`
(`select_tool.py:1829`) auto-picks a **single** pin (first movable, sorted) — the
user cannot choose which lead to bend, and the clicked pin is never remembered
(`hit_test.py` resolves a click to the component, not the pin).

Agreed model: reach is **per-pin**, a euclidean `(min, max)` **disc centred on
each pin's own default hole** (`min ≈ 0`). One feasibility rule: a re-drag is
legal iff every pin's final hole lies in its own disc and no rigid pin is forced
off its pad. Grab a **bendable** pin → pure single-lead bend (body fixed); grab a
**rigid** pin → body translates and the other leads absorb it within their discs
(this is why 2-pin rigid-end grab slides the body). Entry is **click-a-pin**
(re-click switches; pick-a-pin sub-step when the component wasn't selected via a
pin); the silent auto-pick is removed. Overlay shows **both** reachable-pad
highlight (∩ feasibility) and the disc outline + green/red validity tint.

Phases (see the doc): (1) per-pin disc reach model + tests; (2) remember clicked
pin + click-a-pin entry, remove auto-pick; (3) rigid-grab body-translate path +
unified feasibility predicate; (4) overlay. Riskiest: phase 3 (new
body-translation logic — today's `_build_redrag_command` only ever bends one
lead). Overlay legality must call the **same** predicate as commit (no silent
divergence). Deferred: a bendable grab that *also* translates the body (needs an
"extra range" knob in the package definition).

## Generic-package invariant — follow-ups (added 2026-06-25)

`ComponentDefinition.package` is now an invariant: the loader synthesizes a
NON-rigid generic bbox package (`kind="_generic"`, `synth_generic_package` in
`domain/components/package/kind.py`) for parts that declare none. Bend-spec
(`reach.effective_bend_specs`) and DRC (`drc/engine.py` ×3) exclude the
`_generic` tag so their behaviour is frozen. Two follow-ups remain:

1. **Body-mask bent-span difference.** `pad_state.add_component` now always uses
   `_package_body_pads(defn.package, …)` → `body_holes`, computed from the
   package's **default** (unbent) pin positions. The removed legacy path used
   `component_bbox(defn.footprint, placement, …)`, whose `pin_world_position`
   includes `placement.pin_offsets` (the bent-lead span). For a part placed at
   default pitch the two match exactly; for a **bent** lead the synth marks
   fewer body holes (only the default span + 0.5 margin). Routing impact is nil
   (these flexible parts have `body_blocks_routing=False`, so their `ComponentBody`
   is non-blocking), and the synth path gains side-awareness (plated-through) the
   legacy bbox lacked — but anything that *counts* `ComponentBody` items under a
   bent lead sees a difference. Revisit if a consumer needs the bent-span body.

2. **Consolidate the `_generic` exclusion.** The `is_generic_package(pkg)` guard
   now appears in `reach.py` (×2) and `engine.py` (×3) — a `.kind == "_generic"`
   tag-branch repeated in 5 sites. Once authored `bendable_pins` are migrated into
   real non-rigid packages (a data migration), the whole generic fallback +
   `bendable_pins` field can retire; until then, the shared `is_generic_package`
   predicate already centralises the check — keep it the single source.

3. **The ~7 `component_bbox` collision call sites** (placement/pad_conflict/
   placement_conflict/drag_detector/auto_placer) and routing-cost ones still use
   the footprint bbox unconditionally (they never branched on `defn.package`).
   They could now read the package body mask instead, but that is a separate
   collision-semantics change — out of scope for the invariant.

## Place-tool ghost / king-move overlay use defn.footprint, not the chosen variant (added 2026-06-25) — DONE (2026-06-25)

**Done** (`a15d56e`, `d572052`, `e2023dc`, `7bfda89`, `d906630`). `PlaceTool`
now carries the picker's `package_variant` and resolves it through
`_resolved_footprint(defn)` → `resolve_component_package` + `PackageFootprint`
(falling back to `defn.footprint` only when no variant resolves). The ghost,
king-move candidate positions, drag baseline, snap and commit all read the
chosen variant's footprint, so an SMD-placed `R` shows the horizontal SMD body
with pin 1 centred. Regression tests: `test_ghost_body_matches_chosen_variant`,
`test_drag_baseline_uses_chosen_variant_geometry`,
`test_place_library_resistor_as_smd_variant_activates`,
`test_place_library_resistor_as_axial_variant_stays_free`,
`test_smd_enters_drag_pin_phase` in `tests/gui/test_place_tool.py`.

Original report (kept for context):

Placing `R` as **SMD-0805**, the 8 king-move dots are present but mis-centred:
pin 1 sits on a corner dot instead of in the empty centre of the 3×3 grid, and
the ghost draws as the vertical *axial* rectangle, not the horizontal SMD body.

Root cause — the same variant-vs-definition asymmetry the bend-spec fix
(`effective_bend_specs(defn, instance, families, package_variant)`) solved, but
now in the **drawing** layer. The bend-spec *offsets* are computed against the
**chosen variant's package** (SMD-0805: `pin_positions [(0,0),(1,0)]`, pin 2 to
the **right**, +x). But `place_tool._discrete_candidate_world_positions` and the
ghost build apply those offsets to **`defn.footprint`** — the resistor's THT
`GenericFootprint` (`[(0,0),(0,1)]`, pin 2 **down**, +y). Offsets correct for
one axis, footprint on the other → the dot grid is shifted by
`pin2_SMD − pin2_axial = (1,−1)`, so pin 1 lands in a corner. (Verified: feeding
`PackageFootprint(SMD-0805)` to the same math re-centres the grid on pin 1.)

Fix direction: the place tool must draw (ghost, overlay, snap, commit geometry)
from the **footprint of the chosen variant** — i.e. resolve the variant package
(as `effective_bend_specs` now does) and use `PackageFootprint(pkg)` wherever it
currently reads `defn.footprint` during placement. That also fixes the ghost's
wrong (axial) silhouette for a variant-placed SMD. Audit every `defn.footprint`
use in `gui/tools/place_tool.py` (ghost build, `_discrete_candidate_world_positions`,
`_compute_drag_offset`, pin-count for the drag queue, commit). Pairs with the
deferred `component_bbox` item above (same "use the chosen variant's footprint,
not the definition's" theme).

## component_bbox margin should follow package rigidity, not class (added 2026-06-25) — DONE (2026-06-29)

**Done** (`3676256`). `component_bbox` now selects the margin via
`_is_flexible_body(footprint, component_class)`: when the footprint is a
`PackageFootprint` it reads `not is_rigid_package(fp.package)`, otherwise it
falls back to the class test for bare footprints (`drag_detector` / `auto_placer`
call sites, unchanged). SMD/DIP-as-rigid now get `_BODY_MARGIN`; axial `R`
(`BOARD_SOLVE`) and the synth `_generic` package stay `_PAD_MARGIN`. Guarded by
`tests/unit/test_component_bbox_rigidity.py` (SMD→body, axial→pad, DIP→body,
bare-footprint→class). Closes "Generic-package invariant follow-up #3" too.

Original report (kept for context):

`domain/layout/placement.py::component_bbox` picks its margin by **electrical
class**: `m = _PAD_MARGIN if component_class in _FLEXIBLE_LEAD_CLASSES else
_BODY_MARGIN`. So an SMD resistor (class `resistor`, but a *rigid* body lying
flat on pads) is given the flexible-lead pad-margin as if its body floated with
bendable leads — the geometric twin of the bend-spec bug just fixed (where the
place tool treated SMD `R` as THT). This bbox drives hit-test, placement-conflict
detection and the selection outline (`board_view.py`, `placement_conflict.py`,
`pad_conflict.py`, routing).

The model already has the right signal: `shapes.is_rigid_package(package)` — the
same function `loader.py:613` uses for "the rigid body fixes the spacing". The
clean fix is to choose the margin from `is_rigid_package(resolved_pkg)` instead
of the class.

**Why deferred (2026-06-25) — original blocker now LIFTED (verified 2026-06-29):**
the original reason was that every `component_bbox` call site passed only
`defn.footprint` (the *default* variant) + `component_class`, so none could see
the chosen variant to read `is_rigid_package(...)`. That is no longer true: the
instance-carrying sites now thread the **chosen variant's** footprint —
`session.component_bbox` via `resolved_footprint(defn, inst, families)`
(`gui/session.py:786`), and `pad_conflict` + the routing-cost sites via
`footprint_of(defn, inst)` (`pad_conflict.py:673`, `cost_analyzer.py:196`,
`autorouter.py:2315`). So `is_rigid_package(fp.package)` off the passed
`PackageFootprint` would now correctly tell SMD-`R` from THT-`R` at those sites.

**Remaining work (now actionable):** change the margin selection in
`placement.py:144` from `component_class in _FLEXIBLE_LEAD_CLASSES` to
`is_rigid_package(fp.package)` when `fp` is a `PackageFootprint` (fall back to the
class test for bare `Footprint`s). Two sites still cannot resolve the variant
(`drag_detector`, `auto_placer` — no instance/catalogue), so they keep the class
fallback. Do it as its own change with regression tests (THT axial keeps
`_PAD_MARGIN`; SMD/DIP get `_BODY_MARGIN`). Same item as "Generic-package
invariant follow-up #3" above.

## Footprint→body cleanup: two deferred parities (added 2026-06-25)

The geometry/footprint rework (commits `03d432e`/`028259b`/`a28641f` +
follow-up on this branch) collapsed the concrete footprint types
(`DualRow`/`SingleRow`/`TwoPin`/…) into a single `GenericFootprint(offsets)`
— a footprint is now **only** a pin cloud (count + spacing). A footprint no
longer determines a body (the same SIL-3 fits a TO-92, a header, a trimmer).
The `isinstance(fp, …)` body fallback in `gui/renderers/body_shapes.py` and the
`prism_for_footprint` synthesizer were removed accordingly. Two user-visible
behaviours were deferred as stubs (user decision 2026-06-25 "tylko odblokuj
import (stub)") and are marked `xfail` so the suite stays green:

1. **Body-variant picker offers nothing.**
   `domain/components/package/generated_families.offered_body_families` now
   returns `[]` unconditionally — it used to branch on the footprint *type* to
   slice the catalogue, and that type is gone. The offer must be re-derived from
   the component's **explicit** body (`package` / `package_family_ref` /
   `footprints[]`), not from the footprint. Until then the library/properties
   body picker shows no alternatives.
   xfail: `tests/gui/test_panels.py::TestLibraryPanelBodyPicker` (5) and
   `::TestBodyVariantDropdown` (4).

2. **Generated DIP family body is a uniform-margin bbox, not JEDEC-sized.**
   `package/kind._prism_from_footprint` wraps the pin cloud with a fixed
   0.5-hole margin for every kind. The old `prism_for_footprint` had a DualRow
   branch sizing the DIP body to the measured fit `2.54·R − 1.35` mm and
   centring it *between* the rows (legs showing). Restore DIP body dims in the
   prism path (or as a `Prism` feature, like the TO-220 tab above).
   xfail: `tests/unit/test_generated_families.py::TestDipFamily::`
   `test_body_width_matches_measured_mm` and `::test_body_sits_between_rows_not_over_pins`.

## TO-220 metal tab render (added 2026-06-22)

TO-220 became a fully-specified prism package family
(`assets/package_families/to220.yaml`); regulators (`_TO220_3`) and power
MOSFETs (`_MOSFET_TO220_GDS`) declare `footprints: [TO-220]`, so preview and
placed resolve the same prism (commit on this branch; guarded by
`TestTO220PackageParity`).  The body renders as a **plain radial-prism
rectangle** — the characteristic light metal mounting tab the legacy
`TO220Body` drew (using `theme.to220_tab_height_mm`) was consciously dropped
(user decision 2026-06-22: "goły prostokąt na razie").

**DONE (2026-07-03) — restored as a renderer plugin.** With the modular
renderer architecture (lineage-dispatched `PackageRenderer` hierarchy +
`renderers/` plugin discovery, see docs/rendering.md §4.1-4.2), the tab is
drawn by `library/renderers/to220.py` — `TO220Renderer(PrismRenderer)` with
`renders = "TO-220"` extends `super().render_body()` with the metal tab strip
(guarded by `tests/gui/test_renderer_registry.py::TestTO220Plugin`).  This
supersedes the earlier "fold the tab into `Prism` + `_draw_radial`" plan: the
per-*family* renderer is now the sanctioned extension mechanism (user
decision 2026-07-03), and TO-247 / TO-126 get their tabs by registering for
their own family names (or by a shared renderer registered for several keys).

Remaining follow-up: `TO220Body` / `TO92Body` are **fully dead code**
(parity proven by `TestTO220PackageParity` / `TestTO92PackageParity`;
production dispatch never reaches them; the tab capability now lives in the
plugin).  Repoint their remaining direct unit tests at the package renderers,
then delete both classes.  `GenericBody` stays (the live no-package fallback).

## PNG export crashed on save — FIXED (2026-07-03)

`export_png_from_snapshot` called `img.save(str(path), b"PNG")`.  This PySide6's
`QImage.save` runtime rejects a `bytes` format arg (`ValueError: called with
wrong argument values`) even though the type stub asks for `bytes` — a
stub-vs-runtime mismatch — so **every File → Export PNG raised** and the feature
was completely broken.  There was **zero** test coverage, which is how it
shipped.  Fixed by dropping the explicit format (Qt infers `PNG` from the `.png`
extension) and checking the boolean return (`save` returns False, does not raise,
on a bad path).  Guarded by `tests/gui/test_export_paint_smoke.py::TestExportPng`
(writes a real file, verifies PNG magic bytes + reloads a non-null QImage; covers
both `export_png_from_snapshot` and the top-level `export_png`).  SVG/PDF export
were checked — no sibling `.save(..., b"...")` calls (svg uses `QSvgGenerator`).

## Package body rendering — remaining open threads (surveyed 2026-07-03)

A full render survey (this session) inventoried the still-open package-render
threads.  Only the PNG-export crash above was fixed now (user scope decision);
the rest are tracked here / in their existing entries.  Ranked:
- **TO-220 metal tab + delete dead legacy bodies** — see the TO-220 section
  above (tab as a `Prism` feature, then remove the now-dead `TO220Body`/`TO92Body`).
- **Body-variant picker returns `[]`** — see "Footprint→body cleanup" item 1
  (xfail `TestLibraryPanelBodyPicker` = 7 methods, `TestBodyVariantDropdown` = 10
  methods; the "(5)"/"(4)" counts in that entry are stale).
- **Generated DIP body not JEDEC-sized** — see "Footprint→body cleanup" item 2
  (xfail `test_body_width_matches_measured_mm`, `test_body_sits_between_rows_not_over_pins`).
- **Appearance override on `GenericBody` + per-corner corner-radius** — see the
  "Appearance override" section gaps 0 and 1.  `TO92Body`/`TO220Body` recolor is
  moot once they are deleted (dead code); `GenericBody` is the live gap.
- **Pin-name labels never rotate / overlap on rotated dense parts** — labels use
  `ItemIgnoresTransformations` (`component_renderer.py:78,149,164`) so they draw
  screen-axis-aligned regardless of placement rotation; a rotated devboard's pin
  names smear.  DEFERRED by user ("later") — do not start unprompted.

## Component-declaration rework — IC repoint deferred (added 2026-06-21) — IC repoint DONE (2026-06-21)

The headline IC repoint is done (see below, guarded by `TestMigratedICs`). Only
the named-standard-sizes sub-item (last paragraph) is still deferred.

Phases A/B/C of the declaration rework shipped: `extends` (cross-file),
`footprints:` lists, `symbol`, `kind` packages, the forced-choice picker +
recently-used, declared body-kind render, and the passive library shrunk to
generic `R` / `C` / `C_POL` (each offering its bodies as a `footprints:` list).

**DONE (2026-06-21) — ICs repointed.** The DIP ICs under `library/ICs/` moved
off `footprint: {type: DIP, pins: N}` onto `pins: {count: N}` +
`package: {kind: dip_ic}` (DIP-28 → `wdip_ic`). Footprint-only base templates
(`_74_DIP14/16/20`, `_DIP14_IC/16`, `_SINGLE_OPAMP_741`) were dropped; their
override children carry their own pin_names, the 741 children got the pinout
inlined. Genuine shared pinouts kept as templates (`_DUAL_OPAMP_SS/DS`,
`_QUAD_OPAMP` — the standard dual/quad-opamp DIP-8/14 pinouts). TO92/TO220
regulators left untouched. Guarded by `TestMigratedICs`.

Also deferred (per the passive work): predefined standard sizes as
`kind`-with-dimensions (e.g. `kind: tht-din`, size `0207/typ`) sourced from
norms/catalogues — the generator already builds cylinder/prism bodies from
`dims`, so this is adding named size presets, not new geometry.

## Appearance override — deferred coverage (added 2026-06-20)

The per-component / per-instance visual override (fill `color` + corner radius)
landed for **package-backed bodies** (`PackageBody`) — see
`docs/footprints/authoring.md` §8 and `domain/components/appearance.py`. It is now
authored as a single CSS-like **`style:`** string (`"color: #…; corner-radius: …"`,
2026-06-21); the legacy `appearance: {color, corner_radius}` mapping is no longer
read. Three gaps were consciously deferred:

0. **Per-corner corner radius is parsed but not rendered (added 2026-06-21).**
   `style: corner-radius` accepts 1–4 values in CSS `border-radius` order and
   `Appearance.corner_radius_mm` stores them as a tuple, but the render path uses
   only the **first** value (`Appearance.corner_radius_mm_scalar`, read by
   `PackageBody._effective_corner_radius_mm` and the silhouette `corner_r` path).
   To finish: thread the full tuple into the rounded-rect / silhouette paths so
   each corner gets its own radius.

1. **Non-package bodies ignore the override.** `TO92Body`, `TO220Body` and
   `GenericBody` (`gui/renderers/body_shapes.py`) do not take or honour the
   `Appearance` override yet — only `PackageBody` does (the catalogue/inline-package
   and synthesized-prism arms of `body_shape_for`). A 3-pin TO-92 transistor or a
   bare-footprint generic part therefore can't be recoloured/re-rounded via
   `style:`. To finish: thread the resolved `Appearance` into those three
   bodies' `draw()` and apply colour/corner-radius the same way `PackageBody` does.

2. **No GUI editor for per-instance style.** The override is YAML-driven only
   (component definition or `instances.yaml`). There is no Properties-panel control
   nor a `SetAppearanceCommand` (cf. the existing `SetPackageVariantCommand`).

   **Planned (decided 2026-06-20, deferred — pick up cold):** edit a selected
   instance's body colour + corner radius in the Properties panel.
   - **UX (decided):** two **text fields** — colour as `#RRGGBBAA`, radius as float
     mm (NOT a QColorDialog swatch). Commit via the **existing Apply button** (like
     refdes/value), not live-on-pick. **Empty field = no override** (clear the field
     → falls back to the package colour/radius: empty colour → `color=None`, empty
     radius → `corner_radius_mm=None`).
   - **(1) Command** — add `SetComponentAppearanceCommand` to
     `domain/commands/component_cmds.py`, mirroring `SetComponentPackageVariantCommand`
     (`:1221-1252`): `dataclasses.replace(inst, appearance=…)` into
     `state.instances[id]`, `_old` saved in apply / restored in revert. Match the
     base method names (`apply`/`revert`) of that command.
   - **(2) Recording** — register `"set_appearance"` in
     `services/recording/serialization.py` (mirror the `set_package_variant`
     registration `:503-506`): `to` emits `{component_id, style}` where `style` is
     `Appearance.to_style_string()` (or null when the override is `None`), `from`
     rebuilds via `parse_style(...)` or `None`. Reuse `_uuid_to/_from`, `_register`.
   - **(3) Properties panel** (`gui/panels/properties_panel.py`, **signal-out**, holds
     no session): add `appearance_changed = Signal(uuid.UUID, object)` (`:44-54`); two
     `QLineEdit`s as `QFormLayout` rows near the Body row (`:114`); populate them in
     `show_component` (`:177`) from `inst.appearance`, blank in `clear_component`
     (`:203`); in `_on_apply` (`:487-503`) build an `Appearance` from the fields and
     emit `appearance_changed` (validate via the domain — reject a bad colour, do not
     emit; only emit when changed, to avoid no-op undo entries). Emit it as a sibling
     of the existing `pin_offsets_changed` from Apply.
   - **(4) MainWindow** — connect near `:336-340`; add `_on_appearance_changed`
     mirroring `_on_package_variant_changed` (`:2692-2706`):
     `stack.execute(cmd, cast(ComponentState, self._session))` + `mark_dirty()` +
     `board_view.refresh()`. **No renderer change** — `instance.appearance` already
     flows `body_shape_for` → `PackageBody`; refresh re-renders.
   - **Tests:** command apply/revert (incl. None→set→None); serializer round-trip
     (`test_command_recording.py`); panel populate + emit + bad-colour rejection.
     Drive panel signals/slots directly (heavy Qt event loops hang under pytest-qt).
     Then mark this item done + note GUI editing in `authoring.md` §8.

## SelectTool — deferred parity items (after the rewrite landed) — DONE (both items)

The SelectTool rewrite (`gui/tools/select_tool.py` — the service-based tool
that replaced the 9.5k-line monolith) reached full integration parity. Both
consciously-deferred gaps are now closed (verified in code 2026-06-29):

1. **Selection-filter enforcement.** ✅ DONE (2026-06-20).  `SelectTool.
   _filters()` now translates the held `SelectionFilter` into the hit-test's
   per-kind `SelectionFilters` gate, so components/wires/groups can be excluded
   from selection; the Alt+click relation-expansion
   (`_relation_correlated_selectables`) also honours the filter (rule 5: wire
   filter → net's wires + their pads; component filter → net's component pads).

2. **Wire fragment-flip.** ✅ DONE (2026-06-19).  `_flip_wire_fragment` is now
   a wire-edit op-table entry (`[F]`) backed by
   `wire_edit.build_flip_fragment_command` → `FlipWireFragmentCommand`.
   Same-wire only; cross-wire falls back to the whole-wire layer move.  See
   `docs/armed-rotate-and-subtool-delegation.md` §4 stage 4.

## SelectTool v2 — silent-regression audit (2026-06-20)

A full legacy-vs-v2 behaviour audit (parallel finders + adversarial verify)
surfaced silent user-visible regressions from the swap.  Already fixed in this
branch: group-protection lock, relation status label, keyboard relation-cycle,
reroute anchor, bright wire+pad highlight, Alt+LMB net expansion, net-pad discs
following the real selection.  All confirmed items are now resolved:

- **Rubber-band only catches components.**  ✅ DONE.  `_band_select` now
  extends the picked set with `hit.extras` (wires + groups caught by the
  controller's LTR all-inside / RTL any-inside test) and decodes the modifier
  into SET / ADD / REMOVE (`set_many` after a clear, relation-agnostic), not a
  bare component-only SET.
- **Drag conflict feedback.**  ✅ DONE (2026-06-20).  Body-outline recolour
  red (HARD) / amber (SOFT) via `BoardView.set_selection_outline_override`
  driven from `SelectTool._show_conflict_corner`; wire ghost-dimming via
  `_update_wire_ghosting` → `set_wire_drag_state` (drop 0.3 / disconnect 0.5),
  reset in `_clear_drag_corner`.  (Legacy dashed bbox overlay intentionally
  not restored — the outline recolour replaces it.)
- **Free-pin click hint.**  ✅ DONE (2026-06-20).  `_hint_free_pin_under`
  fires from `on_press` when a press lands on an unassigned pin:
  "Pin N (name) — no net (right-click → Assign to net)".
- **Properties panel selection sync.**  ✅ DONE (2026-06-20).  Broader than the
  audit framed it: the panel was never told what was selected
  (`show_component` had no caller), so it stayed blank.  Originally re-wired via
  `MainWindow._sync_properties_panel`; since the session-hub rework (2026-06-29)
  the panel subscribes to `session.selection_changed` itself
  (`PropertiesPanel.set_session` → `_on_session_selection`) and populates /
  clears on each broadcast, and rotate/flip `_notify_selection()` so the combo
  refreshes.  See [docs/gui.md](gui.md) "Selection sync (session hub)".
- **Ctrl+Alt+LMB highlight union.**  ✅ DONE (2026-06-20).  `_apply_selection`
  ADD now rebuilds a relation-agnostic union (clear→set_many), so successive
  Ctrl+Alt clicks accumulate across different nets instead of the FSM add()
  dropping the prior one.
- **Group-enter affordance.**  ✅ DONE (2026-06-20).  Restored as a
  double-click (more discoverable than the legacy 1 s long-press):
  `on_double_press` → `_group_under` → `enter_group`.
- **Wire-edit pad-node enrichment.**  ✅ DONE (2026-06-20).
  `_ensure_wire_ready_for_edit` (called from `arm_wire_edit`) re-inserts board
  pads on the wire's path as vertices via `insert_pad_nodes` +
  `replace_wire_path_cmd`, so simplified wires' intermediate pads are
  clickable again.

NOT a regression (audit false positive): "selection-filter enforcement (GROUPS)
not wired" — that was the consciously-deferred item 1 above (now ✅ done), not a
swap loss.

All confirmed audit regressions are now resolved.

## Parametric renderer for DIP / SIL / dev-board packages

**Done (2026-06-20).** Multi-pin in-line bodies now render through the
same parametric `PackageBody` as catalogued packages — the hand-written
SVG generators are gone.

**DIP body sizing — by mm, centred on the pins (fixed 2026-06-20).**  Generated
`dip-N` / `wdip-N` bodies were too wide (pin-cloud + uniform 0.5-hole margin),
covering the pin columns.  Now a DIP body is sized by explicit mm and **centred
on the pin cloud**:

- `Prism.bbox()` (`package/shapes.py`) with explicit `width_mm`/`length_mm`
  returns a rect centred on the pin-cloud centre (+ optional `body_offset_mm`),
  not anchored at pin 1.  All consumers read bbox in the same pin-1-relative
  frame, so the collision mask now marks the *interior* holes (between rows),
  not the pin columns — physically correct.
- `prism_for_footprint` (`package/from_footprint.py`): a `DualRow` gets JEDEC
  dims — `width = rows_mm − 2·1.27` (body narrower than the row spacing, legs
  show), `length = pin_span + 2·1.27` (runs past the end pins).  SIL / dev-board
  keep the margin path.  Named `DIP-8` in `generic.yaml` re-derived to match.

---

- `src/protoboard/gui/renderers/prism_silhouette.py` — a `PrismSilhouette`
  strategy family (SIL housing, DIP notched body), selected by clustering
  a prism's pin cloud into columns (`silhouette_for`).  1 col of ≥3 pins →
  SIL, 2 cols of ≥2 pins each → DIP; a two-pin prism (SMD chip) and anything
  else → plain bbox / SMD body (no silhouette).
- SMD chips (`SMD-0805` / `SMD-0603`, `assets/package_families/smd_chip.yaml`,
  base `prism_smd`) are two-pin axial prisms.  `PackageBody._draw_smd_chip`
  renders them as a flat rounded body centred on the pad pair and aligned to
  the pad axis (ortho + diagonal), spanning both pads — no leads, no marker.
  The body lies on adjacent pads without covering a hole (`body_blocks`
  stays unset, so the collision mask treats it as a low body, not a hole
  blocker).
- `src/protoboard/domain/components/package/from_footprint.py` —
  `prism_for_footprint` synthesizes a `Prism` from a `DualRow` / `SingleRow`
  / `QuadRow` footprint (pins derived as consecutive differences of the
  footprint's own pin cloud, body = bbox + 0.5-hole margin).  Memoised by
  footprint identity (`@lru_cache`) so a given DIP-16 is built once.
- `body_shape_for` now routes those footprints through the synthesized
  prism + `PackageBody` when no catalogue package resolves (catalogue /
  `package_family_ref` / inline `package` still win first).
- Deleted: `SvgDIPBody`, `SvgSILBody`, `SvgQuadDIPBody`, the legacy
  `DIPBody` / `SILBody`.  (`SvgBodyShape` no longer existed.)  Their
  behaviour tests were repointed at `PackageBody` before deletion
  (`tests/gui/test_body_shapes.py`) — no silent regression.

Appearance is data-driven: the body fill is the package `color:` if set,
else the component / theme fill the renderer passes in (no hard-coded
DIP blue any more).

**SMD chip bodies + rigid-package geometry (done 2026-06-20).**
`SMD-0805` / `SMD-0603` (`assets/package_families/smd_chip.yaml`, base
`prism_smd`) are two-pin axial prisms; dedicated parts `R_SMD_0805 /
R_SMD_0603 / C_SMD_0805 / C_SMD_0603` reference them.  Key fix: a **rigid**
package (`is_rigid_package` — fixed `board_offset`, zero bend, vs the axial
solver/bend of a resistor) referenced via `package_family_ref` now drives
the footprint.  `load_library(dirs, families)` resolves such a ref to a
`PackageFootprint` and drops `bendable_pins`, so the body fixes the 1-hole
pin spacing instead of inheriting the resistor's bendable 4-hole lead (which
made the chip render as a long stretched rectangle).  `load_full_package_
catalogue()` is the single source of the merged generated+authored catalogue
(session picker + loader).  `PackageBody._draw_smd_chip` renders the chip
centred on the pad pair, aligned to the pad axis, spanning the gap + a small
overhang.

**SMD mounting mechanic (ortho + diagonal) — done 2026-06-20.**  The SMD body
is rigid, but its mounting is pad-bound, so a chip may rest on two
orthogonally- **or** diagonally-adjacent pads.  Implemented as a *discrete
mount* on the package, reusing the existing bendable-pin discrete-snap — no new
FSM, no 45° rotation:

- `MountStyle` (`package/body.py`): `THROUGH_HOLE` / `SURFACE`.  `Prism` gains
  `mount` + `mount_points`; `prism_smd` base declares `mount: surface`.
- `surface_mount_offsets` (`package/shapes.py`): pin-2 offsets that land it on
  each king-move neighbour pad of pin 1 (the package's `mount_points` neighbour
  set, default the 8 king-move neighbours), excluding pin 1's own pad.
- `package_to_bend_specs` (`package/reach.py`): a surface-mount 2-pin package
  → `{2: PinBendSpec(discrete=<mount offsets>)}`; `is_pin_bendable` reports pin
  2 movable.  Through-hole packages unchanged.
- `effective_bend_specs(defn)` (`package/reach.py`): the single resolver
  (package-driven → else `bendable_pins`) shared by `PlaceTool._bend_specs`
  **and** the properties panel — the panel previously read `bendable_pins`
  directly and so showed nothing for an SMD part.
- The loader records the resolved rigid package on `defn.package` (not just the
  footprint) so `effective_bend_specs` sees it.
- `place_tool` overlay now highlights the discrete mount-candidate pads (was
  free-reach only).  The properties-panel `_DiscreteBendWidget` combo edits the
  mount post-placement, free.  The choice is just `pin_offsets[2]` — persisted
  and recorded by the existing placement machinery.
- **Rotating / re-orienting a placed SMD**: `Ctrl+R` ("Re-drag Pins") now works
  for SMD — `SelectTool._redraggable_pin` accepts a discrete-mount pin and
  `_build_redrag_command` snaps the clicked pad to the nearest of the 8 mount
  offsets, so a placed chip re-orients (ortho + diagonal) in one gesture.
  Plain `R` still gives the 4 orthogonal rotations; the diagonal mounts come
  from the mount pick (drag at place-time, `Ctrl+R` after, or the Properties
  combo).

DRC/collision already treat the diagonal mount correctly (pins checked
independently; body collision masks snap to integer holes; body `body_blocks`
unset → occupies space, not a hole).  Renderer already aligns the body to the
pad axis.

Not done: friendlier combo labels (compass arrows) — kept `(dx, dy)` because an
arrow needs pin-1 context the widget lacks; sub-hole "floating" positions.

**Named DIP/SIL variants — generated + picker (done 2026-06-20).**
The whole multi-pin body set is *generated* by name (no static YAML
catalogue):

- `dip-N`  → narrow body (0.3", `rows_spacing` 3 holes).
- `wdip-N` → wide body (0.6", `rows_spacing` 6 holes).
- `sil-N`  → single in-line header shell.

`domain/components/package/generated_families.py` builds these as
`PackageFamily` objects (one `std` variant each) via the same
`prism_for_footprint` machinery; `EditorSession` merges them into
`package_families` (authored two-pin families win on an id clash).  A part
selects one through the existing `instance.package_variant` →
`resolve_component_package` path — `"wdip-16/std"` renders the wide body.

The body-variant picker (properties + library panels) is opened to
multi-pin in-line footprints via `offered_body_families`, which offers only
the pin-count-matching families (a DIP-16 sees `dip-16` / `wdip-16`, never
`dip-8` or a resistor body; a two-pin part still sees only two-pin
families).

Follow-up (not scheduled): library YAML could set a multi-pin part's
`package_family_ref` (e.g. an ATmega328P defaulting to `wdip-28`) so the
"Default" picker entry resolves to a specific width; today an unset default
falls through to the synthesized narrow body.

**Package parameter schema — Phase 1 done, follow-ups (2026-06-27).**
Field declarations now live under `schema:` on abstract bases (path-mirrored to
the value tree: `schema.dims.width` describes `dims.width`), parsed by
`domain/components/package/schema.py` into a `ParamSchema`. The loader applies it
before building a shape: fill `default` → check `required` → type-check declared
fields. Validation is **loose** — a value with no schema leaf (`flat_radius`,
`flat_normal`, `margin`, `edge_radius`, `corner_radius`) passes through. The
`auto` sentinel satisfies `required` and is skipped by the type check (a kind
generator is meant to resolve it). `leads.direction` is deliberately *not*
schema-declared (its three YAML forms are a richer grammar than a single type
shape; `expand_direction` validates it). See `docs/footprints/package-model.md` §5.

Phase 2 landed in **limited scope**: the generator API moved to
`build_kind_package(kind, ctx, schema) -> GeneratorContext` (frozen
`GeneratorContext{pin_count, values, package}`), so a generator can read schema +
current/earlier-computed values and so a chain is *possible*. The procedural
DIP/SIL/connector builders are unchanged in behaviour.

Deferred (not scheduled):
- **Generator chain driver** — running `generator_X → generator_Y` so one
  generator sees another's output. The API supports it; the driver is unbuilt.
- **`auto` resolution *through* generators** — today the procedural builders do
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

## Eliminate `defn.footprint` for placed components — RULE + audit (added 2026-06-30, DONE 2026-06-30)

**HARD RULE (user, 5th time):** any `defn.footprint` used for a component that is
*placed on the board* is a **critical bug**. A placed component always has a
bound instance, and its real package/footprint is the chosen variant
(`inst.resolved_footprint()` / `pad_conflict.footprint_of(defn, inst)`), NOT the
definition default. When the instance picked a non-default variant the pin
*positions* differ (e.g. `IRF9540_P` as `package_variant: sil-3/std`: TO-220
default pins along X `[(0,0),(1,0),(2,0)]` vs sil-3 along Y `[(0,0),(0,1),(0,2)]`),
so after rotation pins 2/3 land on the wrong pads. Pin 1 (anchor, offset 0,0)
usually still works, so the bug looks path-dependent / partial.

**New shared helper:** `pad_conflict.resolved_footprint_or_default(defn, inst)` —
returns the instance's resolved variant footprint when bound, falls back to
`defn.footprint` when unbound. Use it in build paths that can legitimately run
before the session binds instances lazily (`load_project` returns unbound). Where
the instance is *guaranteed* bound, prefer the strict `footprint_of(defn, inst)`
(raises on unbound, surfacing the bug). Both live in `domain/layout/pad_conflict.py`.

**Key invariant (verified across all 169 builtin definitions, 0 mismatches):**
**pin _count_ is variant-invariant** — every variant of one definition has the
same number of pins (a 3-pin MOSFET is 3-pin as TO-220 or SIL-3). Only pin
*world positions* diverge. So `pin_count()` reads were never the bug; only
geometry was. The fix still routes count through the resolved footprint for
single-source-of-truth (one resolve per loop, used for both bound and geometry).

### DONE — CRITICAL: pin *world positions* (real bug — wrong pads on a variant)
- `domain/connectivity/incremental.py` `_build_same_name_pin_merges` /
  `_build_component_pin_map` — **the IRF9540 MISSING_NET bug**, fixed in `5d00c8c`
  with regression test `tests/unit/test_connectivity_variant_footprint.py` (green).
- `gui/renderers/board_renderer.py` `pin_net_map` — pad-net overlay (`5d00c8c`).
- `gui/renderers/component_renderer.py:677` — **not a bug**: ghost-preview path,
  no placed instance; already resolves the variant when `package_variant` is set.

### DONE — placed-component geometry/iteration repointed to resolved footprint
Hoisted the resolved footprint before the loop (`fp = footprint_of(defn, inst)` or
`resolved_footprint_or_default` where unbound is possible) and used `fp.pin_count()`
as the bound: `services/drc/engine.py`, `services/routing/autorouter.py`,
`services/routing/unweave.py`, `services/routing/builtin_optimizers.py` (×2),
`services/placement/auto_placer.py`, `services/drag_detector.py` (repointed bounds
to the already-hoisted `incoming_fp`/`other_fp`), `domain/layout/group_tree.py`
(×2), `gui/session.py`, `gui/main_window.py`, `gui/tools/select_tool.py` (flip
gates ×2 + export-pins picker). All variant-invariant pin-count reads, but now
single-source-of-truth. Zero new `mypy --strict` errors.

### Legitimately left on `defn.footprint` (no placed-component geometry)
- `instance.py:140` (the documented no-package default return — the ONE correct
  fallback), `domain/board/pad_state.py:874` (explicit `else:` branch for an
  *unbound* instance; the `if instance is not None` branch already resolves the
  variant — the model implementation), `component_package.py` / `pad_conflict.py`
  docstrings, `place_tool.py:488` (`_resolved_footprint`'s documented no-variant
  fallback), `loader.py` / `footprint_choice.py` / `properties_panel.py:580`
  (picker over the `defn.footprints` *list*), `bom.py:33` (type name for the BOM).

## Group scene-graph migration — transitional debts (added 2026-07-03)

The FLATTEN→scene-graph group migration shipped in phases 0–6 (commits
`55d488a`→Phase 6): a `ComponentGroup` now carries its own `Transform2D`; a
member's world pose is composed from the ancestor group transforms at read time
through the `PlacementCollection` seam (`domain/layout/placement.py`,
`GroupTree.world_transform`); group rotate/flip fold a matrix into
`group.transform` instead of baking member coords, so their undo snapshots hold
no per-member placements. Two deliberate transitional debts remain — both work
correctly today, neither is a bug, and each is its own future change.

### 1. `group.rotation` / `group.origin` are still authoritative, not derived views

`group.transform` is the scene-graph source of truth, but the legacy
`rotation:int` + `origin:Point` fields are still **written** (kept in sync by the
group commands and the v2→v3 load migration) and **read**. Making them derived
views (`rotation` = the transform's rotation steps; `origin` = a pure
bbox-top-left read) is the end-state of the linear-algebra unification
(`feedback_prefer_linear_algebra_math_physics`), deferred so this migration
could ship incrementally.

**Important:** geometry render / hit-test do NOT read these fields — they read
`group.transform` via `GroupTree.world_transform` → the compose seam
(`layout.py` `_compose_placement`). So the conversion touches only two
subsystems plus one GUI dirty-check:

- **Persistence (save):** `services/persistence/project_yaml.py:363-364`
  (write rotation/origin), `services/recording/serialization.py:225-228`
  (command-recorder group serialization).
- **Persistence (v2→v3 load migration):** `project_yaml.py:499` (trigger:
  `rotation != 0 and transform == identity`), `:509` (`origin is None` skip
  guard), `:512` (`_rotation_about(origin, rotation)` derives the transform from
  the baked pair). This path *must* keep reading the legacy pair to migrate old
  files even after the live model stops writing them.
- **Origin-recompute (bbox-top-left invariant):** `domain/layout/group_tree.py`
  `recompute_group_origin` (~:477-498, mirrored by `_recompute_group_origin` in
  `group_cmds.py`); also the rotate-pivot lookup `group.origin or
  GroupTree.group_origin(...)` in `RotateGroupCommand.apply`.
- **GUI dirty-check:** `gui/selection/float_transform.py:418` reads `.rotation`
  in a transform-diff check.

Constraint to preserve (riskiest-surface #3 in the plan): `origin` means "bbox
top-left" for hit-test/render — do NOT conflate it with the transform pivot when
converting. Do the conversion as its own change with the parity corpus green
(`test_pin_world_parity`, `test_project_01/02`, `gold_performance_test`,
`tangled_*`).

### 2. The float-transform live *preview* never routes through the group transform

`gui/selection/float_transform.py` `_rotate` / `_move` / `_flip` (~:318/:340/:366)
mutate member placements directly off-stack to draw the **drag preview**; they do
not fold into `group.transform`. This is harmless for a single flat group — the
committed result goes through the snapshot-based `TransformDragSetCommand` /
`MoveDragSetCommand` or the group commands, which are correct — but a
**nested-group** preview can diverge from what the commit produces, because the
preview bakes world coords while the commit composes. The plan flagged this as
"audit in Phase 4"; Phase 4 was folded into Phase 3, which rewrote only
`RotateGroupCommand`, so this preview path still thinks in world coordinates.

Follow-up: make the preview compose through the group transform (or explicitly
document + gate the flat-only limitation). Coverage here is thin, so add a
preview-vs-commit parity test (drive a nested-group drag preview, then commit,
and assert the two produce identical world pins) BEFORE changing the preview
math.

### 3. `FlipGroupCommand` has no GUI entry point (unwired)

`FlipGroupCommand` (`group_cmds.py`, shipped Phase 5, serialization registered
2026-07-03) is fully implemented and replay-ready, but **nothing constructs it
from the GUI** — no menu action, no keybinding, no `select_tool` dispatch. Flip
is asymmetric with rotate: `select_tool._rotate_multi` routes a selected group to
`RotateGroupCommand` (`select_tool.py:3238-3239`), but `_flip_multi`
(`select_tool.py:3246`) only flips 2-pin **components** individually via
`MoveComponentCommand` + `flip_component` — a selected group is not flipped as a
rigid body.

Wiring it is a feature, not a mechanical entry-add, because the command needs an
`axis` (`'v'`/`'h'`) and an optional `pivot`, and the intended UX is the armed
**FlipMode** (sibling to armed Rotate/Move) with `[v]`/`[h]` axis choice + a
pickable axis position — which is designed but not built (see the group
live-compose plan, Phase 5 notes). Minimum viable wiring without FlipMode: in
`_flip_multi`, when the selection is a single group, emit
`FlipGroupCommand(group_id, axis=…)` with a default axis (the plan's default is
the arithmetic pin centroid, already the command's `pivot=None` behaviour). Do it
with the armed-FlipMode UX or as a deliberate minimal default — either way it is
its own change with its own tests. Until then, group flip is reachable only
programmatically / via replay, not by the user.

## Command serialization coverage — gap closed + totality guard (added 2026-07-03)

Audit of "every Command must be serializable" found **6 concrete commands with no
serializer** — the recorder *silently skips* unregistered commands
(`test_unknown_command_is_skipped`), so any recorded session containing them
replayed **divergently**. Registered them in
`services/recording/serialization.py` with round-trip tests
(`test_command_recording.py::TestNewlyRegisteredRoundtrip`):
`MoveDragSetCommand`, `TransformDragSetCommand` (multi-select drag / float
transform — the biggest blind spot, common ops), `DropWiresCommand` (macro
companion of the transform commit), `FuseTouchingWiresCommand` (wire-fuse macro
member), `ConvertEndpointToWirePinCommand` / `ConvertWirePinToEndpointCommand`
(wire-pin conversion via main_window). Also registered `FlipGroupCommand` so it is
replay-ready the moment it gets a GUI entry (see item 3 above).

New guard: `test_command_recording.py::TestSerializationTotality` iterates every
concrete `Command` subclass and asserts each is registered **or** listed in
`_INTENTIONALLY_UNREGISTERED` with a reason (discovery is scoped to the
`protoboard.domain.commands` package, so throwaway `Command` subclasses defined in
other test modules and the deserialize-only `_DeferredSplitStrip` wrapper in
`serialization.py` are out of scope by construction). Currently excused:
`ConditionalCommand` / `GroupMembershipCommand` (abstract bases) and
`TranslateAnchoredWiresCommand` (**dead code** — superseded by the drag pipeline,
no constructor anywhere in `src/`; a candidate for deletion in a separate
cleanup). A new command that reaches `CommandStack.execute` now fails the suite
unless registered or explicitly excused.

Note on the keybinding half of the audit: the request "every command has a
keybinding-panel entry" is not 1:1 achievable by design — the panel binds
**actions** (`ALL_ACTIONS` ⟷ `KB.DEFAULTS`, 103 specs, totality already enforced
by `test_action_registry.py`), not commands (~33). One action fires ≥1 command and
many commands are mouse/dialog-only (drag, place-tool click) with no meaningful
shortcut. The only command lacking *any* user-facing entry is `FlipGroupCommand`
(item 3); all others are reachable via an action, menu, PPM, or direct interaction.
