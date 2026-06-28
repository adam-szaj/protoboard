<!-- SPDX-License-Identifier: MIT -->
# Footprint source-of-truth refactor — remove `defn.footprint` for placement geometry

**Status:** plan / in progress (2026-06-28). Several call sites already migrated
(see "Done"); the rest is staged below.

## The principle (decided 2026-06-28)

A **placed** component always resolves a concrete package/footprint through its
*instance*: `resolve_component_package(defn, instance, families)` (with a generic
fallback). Therefore **`ComponentDefinition.footprint` must never be the source
of placement geometry** — it is the *default variant's* pin cloud, which is the
wrong body whenever the instance chose another variant (e.g. R placed as
SMD-0805: default axial 3.2-hole pitch vs SMD 1-hole pitch). Every "components
drift / pin-pad highlight off / reach ring wrong / can't place" bug in this area
is the same root: a consumer read `defn.footprint` instead of the resolved
variant.

`defn.footprint` legitimately stays for **topology only**: `pin_count`,
`pin_names` validation, union-find membership, BOM type name — things identical
across all variants.

## The helper (exists)

`domain/components/package/component_package.py`:
```python
resolved_footprint(defn, instance, families) -> Footprint
# resolve_component_package(...) → PackageFootprint, else defn.footprint
```
`resolve_pin_world_position(..., families=...)` already routes through it.

## Done (committed)

- `resolve_pin_world_position` gained `families`; routes through `resolved_footprint`. (`d37c147`)
- `session.get_pin_positions`, `session.component_bbox`, `_build_comp_spatial_index`. (`d37c147`, `0eea557`)
- `component_renderer._render_one` + `render_groups`. (`d37c147`)
- `project_yaml` load: v1→v2 pin index, migration resolver, stale-path snap. (`8be4011`)
- `place_tool._resolved_footprint` (baseline / flip / pin-count). (`e2023dc`)

## Group A — easy (instance + families in scope; local wrap, no signature change)

| File:line | Function | Note |
|---|---|---|
| `gui/placement_conflict.py:73,84,91-94` | `check_and_resolve` | `session.package_families` in scope → pass to `find_conflicts` |
| `gui/tools/select_tool.py:2128,3093,3098,3207,3212,3508` | flip / centre / pin-count | wrap with `resolved_footprint(defn, inst, self._session.package_families)`; 3093/3207/3508 pin_count are TOPOLOGY (leave) |
| `gui/tools/route_tool.py:223,242` | routing pin pos | `self._session.package_families` |
| `gui/renderers/wire_renderer.py:726,870` | wire anchor/target pin pos | `theme.package_families` + instance lookup |

## Group B — needs plumbing (families/instance not in signature)

Each needs `families` threaded through. Caller chain always bottoms out at
`EditorSession.package_families`.

- **`domain/board/pad_state.py:861` (`PadIndex.add_component`)** — CRITICAL (this
  is the pin-pad highlight bug). Plumb `families` into `PadIndex.__init__` →
  `add_component`; resolve with `resolved_footprint(defn, instances[comp_id],
  families)`. Callers: `pad_state.rebuild`, `drag_controller`,
  `placement_conflict._build_drag_pad_index` — all have `session.package_families`.
- **`domain/layout/pad_conflict.py` (`find_conflicts`, `find_wire_conflicts`)** —
  add `families` param; resolve per-pin for incoming + existing components.
- **`services/drag_detector.py` (`DragSnapshot`)** — add `families` field, used in
  classify loops.
- **`domain/layout/group_tree.py`, `group_ownership.py`** — geometry methods need
  `families` (verify which are GEOMETRY vs TOPOLOGY first).
- **`services/drc/engine.py`, `services/routing/autorouter.py`,
  `builtin_optimizers.py`, `auto_placer.py`, `cost_analyzer.py`, `unweave.py`** —
  service entry points get a `families` param, passed from `gui/workers/*` which
  hold `session.package_families`. Largest, most systematic block.
- **`gui/dialogs/resize_board_dialog.py:91-92`** — has `session`.

## Group C — topology, leave as-is

`definition.py:259,262,274` (pin_count / pin_names validation),
`incremental.py:576,658` (union-find), `board_renderer.py:483` (strip groups
topology), `bom.py:33` (type name).

## Phasing

1. **Group A** + `pad_state.PadIndex` (the pin-pad highlight + conflict bugs the
   user is hitting now). Highest user-visible value.
2. `pad_conflict`, `drag_detector`, group tree.
3. Services (DRC, autorouter, optimizers, auto_placer) — thread `families` from workers.
4. Renderers / wire_utils — defer; `defn.footprint` fallback is visually safe.

Test per area: a placed variant (SMD-0805) whose pin-2 lands on the variant pad,
not the default 3.2-hole position.
