<!-- SPDX-License-Identifier: MIT -->
# Footprint source-of-truth refactor — remove `defn.footprint` for placement geometry

**Status:** DONE (2026-06-28). All geometry consumers migrated; remaining
`defn.footprint` reads are topology-only or documented fallbacks (see end).

## The principle (decided 2026-06-28)

A **placed** component always resolves a concrete package/footprint through its
*instance*: `resolve_component_package(defn, instance, families)` (with a generic
fallback). Therefore **`ComponentDefinition.footprint` must never be the source
of placement geometry** — it is the *default variant's* pin cloud, which is the
wrong body whenever the instance chose another variant (e.g. R placed as
SMD-0805: default axial 3.2-hole pitch vs SMD 1-hole pitch). Every "components
drift / pin-pad highlight off / reach ring wrong / can't place" bug in this area
was the same root: a consumer read `defn.footprint` instead of the resolved
variant.

`defn.footprint` legitimately stays for **topology only**: `pin_count`,
`pin_names` validation, union-find membership, BOM type name — things identical
across all variants.

## The mechanism — the instance carries its definition (decided 2026-06-28)

Rather than thread a `families` catalogue through a dozen call signatures, the
**instance holds a live back-reference to the definition it was placed from and
the package catalogue**. A placed instance is never separated from its
definition: if an instance exists, its definition is always available. So the
instance can resolve its own variant geometry.

`ComponentInstance` (`domain/components/instance.py`) gains two
`compare=False, hash=False` fields, **never serialized** (the loader
reconstructs them from `definition_ref` + the session catalogue):

```python
definition: ComponentDefinition | None = None
families:   dict[str, PackageFamily] | None = None

def bind(self, definition, families) -> ComponentInstance:  # frozen-safe copy
def resolved_footprint(self) -> Footprint:                  # variant-aware
```

**Binding ritual** — mirrors `build_pin_identities`: whenever an instance enters
the project, `build_pin_identities_for` (command path) and
`rebuild_all_pin_identities` (bulk load) call `inst.bind(defn, families)` and
store the bound copy back. `package_families` is read best-effort via
`getattr(state, "package_families", None)` so it is *not* a required
`ComponentState` protocol member (bare test stubs stay valid; they degrade to
the definition default).

This unlocks **in-place footprint/package swaps**: with the definition bound, a
placed instance always knows the full variant option set, so a future
"change footprint" command needs no extra plumbing.

## The helpers

`domain/components/package/component_package.py`:
```python
resolved_footprint(defn, instance, families) -> Footprint   # graceful, never raises
```
`domain/layout/pad_conflict.py`:
```python
footprint_of(defn, inst) -> Footprint   # inst.resolved_footprint() else defn.footprint
```
`inst.resolved_footprint()` — the instance method; raises if unbound (use the
free functions when an instance may be unbound).

## Migrated (all committed)

Early call sites (pre-binding, families-threaded):
- `resolve_pin_world_position(families=…)`; `session.get_pin_positions`,
  `component_bbox`, `_build_comp_spatial_index`. (`d37c147`, `0eea557`)
- `component_renderer._render_one` + `render_groups`. (`d37c147`)
- `project_yaml` load: v1→v2 pin index, migration resolver, stale-path snap. (`8be4011`)
- `place_tool._resolved_footprint`. (`e2023dc`)

Binding model + remaining consumers (instance carries its definition):
- **Foundation** — `ComponentInstance.bind/resolved_footprint`, binding ritual in
  `build_pin_identities_for` + `rebuild_all_pin_identities`. (`94f4a59`)
- **PadIndex** `add_component(instance=…)` — the pin-pad highlight bug. (`4a8273d`)
- **Conflicts** — `pad_conflict.footprint_of`, `find_conflicts`/`find_wire_conflicts`
  (`incoming_instance` param), `placement_conflict.check_and_resolve`. The
  reach/can't-place bug. (`0470289`)
- **Group A** — `select_tool` flip + redrag overlay/feasibility, `route_tool`
  hints, `wire_renderer` anchors. (`a718391`)
- **Group B + extras** — `drag_detector`, `drc/engine`, `autorouter`,
  `cost_analyzer`, `unweave`, `builtin_optimizers`, `auto_placer` (+`spreader`
  3-tuple ripple), `group_tree`, `group_ownership`, `wire_utils`,
  `placement.move_blocked`, `wire_net_ops`, `properties_panel` (threaded the
  instance into `_update_geometry_table`), `resize_board_dialog`.

Services in domain/layout import `resolved_footprint` **locally** (the package
free function) to avoid a `pad_conflict → placement` module-load cycle; pure
services import `footprint_of` from `pad_conflict` at top.

## Deliberately left on `defn.footprint`

- **Topology** (variant-invariant): `incremental.py` union-find, `bom.py` type
  name, `board_renderer.py` strip groups, every `defn.footprint.pin_count()`
  loop bound and `pin_names` lookup.
- **Pre-placement / no instance**: the picker (`footprint_choice.py`,
  `properties_panel` body-variant combo via `offered_body_families`) lists
  *options* before any instance variant exists.
- **Documented fallbacks**: `pad_state.add_component` `else` branch (unbound
  instance), `make_ghost_item` default-when-no-variant, the `resolved_footprint`
  / `footprint_of` fallbacks themselves.

Regression guard: `TestPadIndexUsesVariantFootprint` (a placed SMD-0805 puts
pin-2 on the variant pad, not the default 3.2-hole position) +
`TestVariantFootprintOnLoad`.
