# TODO

Open follow-ups that don't fit into the closed plans archived under
[docs/archive/](archive/).  Each entry has enough context to pick up
the work cold.

## SelectTool — deferred parity items (after the rewrite landed)

The SelectTool rewrite (`gui/tools/select_tool.py` — the service-based tool
that replaced the 9.5k-line monolith) reached full integration parity, with
two consciously-deferred gaps:

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
following the real selection.  Remaining confirmed items (all medium), being
worked through:

- **Rubber-band only catches components.**  `_arm_band` passes only
  `comp_ids`; `_band_select` builds only `ComponentSelectable` and always
  SETs.  Lost vs legacy: wire catching (LTR all-inside / RTL any-inside or
  segment-crosses), group catching (member-bbox coverage), and Ctrl/Shift
  ADD/REMOVE ops (always replaces).
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
  (`show_component` had no caller), so it stayed blank.  `MainWindow.
  _sync_properties_panel` (from `_on_selection_changed`) populates it on select
  / clears on deselect, and rotate/flip now `_notify_selection()` so the combo
  refreshes.
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
