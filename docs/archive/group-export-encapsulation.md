# Group Export Encapsulation Refactor

Status: design plan, 2026-06-04. Decisions confirmed by user; implementation not started.

## TL;DR

`ComponentGroup.exported_pins` will become **encapsulated**: a pin can appear in `A.exported_pins` only when its component is either:
- a direct member of A (`A.component_ids`), **or**
- already exported by a direct child of A (`ep ∈ child.exported_pins` for some `child.id ∈ A.child_group_ids`).

This makes groups behave as proper black boxes: in `A[B[C]]`, exposing `C.c1` to the world requires three explicit re-exports — `C.exported_pins ∋ c1`, `B.exported_pins ∋ c1`, `A.exported_pins ∋ c1`. No transitive auto-promotion.

User decisions (2026-06-04):
1. **Schema unchanged** — `ExportedPin` stays a `(component_id, pin_number)` tuple. The new constraint is enforced by a validator that walks the parent→child re-export chain.
2. **Existing projects auto-migrate silently on load** — the loader strips any flat export whose component isn't a direct member of the group and isn't covered by a direct child's exports, in a topological pass from leaves to root.
3. **No `re_export_descendants` opt-in** — every re-export is explicit.

## 1. Current behaviour — what's actually broken

Two code paths disagree on what "exported" means in a nested tree:

- **`external_pins_for_components` (group.py:102)** treats the group's *recursive component set* as the input. For "Export Auto" on A in `A[B[C]]`, it walks every net and collects every pin on any descendant of A whose net touches the outside world. The result is a flat `frozenset[ExportedPin]` that mixes pins of A's direct members with pins of components living deep inside C. Transparent.
- **`board_view.snap` and `is_locked_pin` (board_view.py:1156, 1230)** look only at `active_group.child_group_ids` and read **each child's own `exported_pins`** verbatim. From A, only `B.exported_pins` is observed; whatever B does or doesn't re-export from C is the final word. Encapsulated.
- **`net_is_internal_to_group` (group_ownership.py:124)** uses **direct** members (`group.component_ids`) — neither recursive nor encapsulated. Inconsistent with both above.
- **DRC `_check_group_exports` (engine.py:835)** uses **recursive** members. Flags MISSING/BOGUS based on the flat exported set against the recursive comp set. Transparent.

Net effect: the user can "Export Auto" on A and get pins from C in A.exported_pins (transparent), but the snap/render layer treats those pins as invisible from A (encapsulated). Inconsistency leads to wires that look connected in the model but unreachable in the editor, and to ownership classifications that diverge from rendering.

## 2. Target semantics — encapsulated re-export chain

### 2.1 Schema (no change to `ComponentGroup`)

```python
@dataclass(frozen=True)
class ExportedPin:
    component_id: uuid.UUID
    pin_number: int
```

Still a plain `(comp_id, pin_num)` tuple. **The change is in what's allowed to land in `exported_pins`**.

### 2.2 New invariant

For every non-root group G:

> Every `ep ∈ G.exported_pins` must satisfy at least one of:
> 1. `ep.component_id ∈ G.component_ids` (direct ownership), or
> 2. `∃ child ∈ G.child_group_ids: ep ∈ child.exported_pins` (re-export through a direct child's interface).

Equivalently: the *exposable surface* of G is

```python
exposable(G) = {ExportedPin(c, p) for c in G.component_ids for p in pins_of(c)} \
              ∪ ⋃{child.exported_pins for child ∈ G.child_group_ids}
```

and `G.exported_pins ⊆ exposable(G)`.

### 2.3 Consequences in plain language

- C exports `c1` → c1 is part of C's interface, visible to B (because B contains C as a direct child).
- For c1 to be visible to A as part of B's interface, **B must re-export c1**: `B.exported_pins ⊇ {c1}`. Without that, A treats B as a black box that didn't expose c1.
- For c1 to be visible to the world: A must re-export c1 too.
- The pin "physically lives on a component in C", but each level along the chain decides whether to expose it. Encapsulation.

### 2.4 What does *not* change

- Wires routed entirely between exposed-down-the-chain pins remain physical wires owned by the deepest group that fully contains them. Rendering Z-order, drag classification, autoroute group projections — the algorithms keep working on the same `all_component_ids(G)` recursive sets they use today.
- Global nets (VCC/GND) stay global: their pins are *automatically* part of every group's external interface, never owned, never needing manual export.

## 3. Plan — touched files and surgical changes

### 3.1 Domain model (`src/protoboard/domain/layout/group.py`)

**New helper:** `exposable_surface(groups, group_id) -> frozenset[ExportedPin]` returning the union described in §2.2. Pure function, no Qt.

**New validator:** `validate_exported_pins(groups, group_id) -> list[InvalidExport]` — returns the subset of `G.exported_pins` that violates the invariant (component neither direct member nor child re-export). Used by both DRC and the auto-migration loader.

**Rewrite `external_pins_for_components`** — the current signature `(component_ids, schematic)` is too coarse. Replace (or add a wrapper) with `external_pins_for_group(groups, group_id, schematic)` that:
1. Builds `direct_comp_ids = G.component_ids`,
2. For each direct comp, for each net pin on that comp: if the net has any pin outside `all_component_ids(G)` or is global, that pin is a candidate,
3. Plus the union of `child.exported_pins` for direct children (their re-exportable surface),
4. Returns the subset that should be re-exported because the net continues to reach outside G.

Auto-export (the user-facing "Export Auto") becomes: *"the minimum re-export set such that every net reaching outside G has at least one exposed pin"*. Matches the DRC's `GROUP_MISSING_EXPORT` rule.

**Rewrite `net_is_internal_to_group`** — should use `all_component_ids(groups, group.id)` (recursive), not `group.component_ids`. This is an existing bug; the rewrite for encapsulation gives us a clean chance to fix it. Group ownership classification is unaffected (caller already passes the recursive set as `group_comp_ids`), so this only fixes the standalone callers.

### 3.2 DRC engine (`src/protoboard/services/drc/engine.py:806-` `_check_group_exports`)

Add a third issue class:

- `GROUP_INVALID_EXPORT` (error): `ep ∈ G.exported_pins` but `ep.component_id ∉ G.component_ids` and `ep ∉ ⋃ child.exported_pins`. The pin can't be at G's interface because the chain isn't honoured.

The existing `GROUP_MISSING_EXPORT` (external net with zero exposure) and `GROUP_BOGUS_EXPORT` (export of a non-external net) remain. Run the new check before MISSING — invalid exports could mask MISSING violations.

### 3.3 Persistence loader (`src/protoboard/services/persistence/project_yaml.py:697-`)

After loading every group, run a topological pass:
1. Order groups by depth (root → leaves first since the validator works bottom-up: a group's exposable surface depends on its children's exports).
2. Actually: leaves → root. Each leaf has no children; its exposable surface is just its own direct pins. Then parent groups can compute their surface knowing children's `exported_pins` are already legal.
3. For each group in that order, drop every `ep` not in `exposable_surface(...)`.
4. Log how many entries were stripped per group at INFO level so the user notices.

No dialog. The loader silently makes the loaded layout conform to the invariant; the user finds out by seeing fewer dots on the outline.

### 3.4 GUI: `_group_export_auto` (`main_window.py`)

Switch from `external_pins_for_components(all_component_ids(G), schematic)` to the new `external_pins_for_group(groups, gid, schematic)` so the auto-export respects encapsulation.

### 3.5 GUI: PPM `_group_for_selection` (`select_tool.py:7320+`)

Same change: when creating a new group with `auto_export_external=True`, compute the exposable surface using the new helper.

### 3.6 GUI: PPM per-pin export submenu (`select_tool.py:2990-3004`)

Currently lists `pin_num in range(1, footprint.pin_count() + 1)` of the clicked component (a direct member of active group). The encapsulated model needs to also surface re-export options for pins of direct child groups whose `exported_pins` contains them but `active_grp.exported_pins` does not.

Two sub-menus:
1. **"This component"** — pins of the clicked component (if direct member of active group). Same as today.
2. **"Re-export from sub-group X"** — for each direct child X of active group, list `X.exported_pins`. Each item toggles whether that ep also lives in `active_grp.exported_pins`. Disabled item for "already in active.exported_pins → check".

### 3.7 GUI: `board_view.snap` and `is_locked_pin` (`board_view.py:1148+`, `1220+`)

Already encapsulated (reads only direct children's `exported_pins`). No change needed.

### 3.8 GUI: component renderer (`renderers/component_renderer.py:779+`)

Already encapsulated (renders `group.exported_pins` of each visible child). No change needed.

### 3.9 GUI: groups panel display (`panels/groups_panel.py:110`)

The label shows `len(grp.exported_pins)`. Keep — number is still meaningful, just constrained.

### 3.10 Net-aware classifier (`drag_classification.py`, `group_ownership.py`)

`wire_is_owned_by_group` keeps using the recursive component set and reads `group.exported_pins` directly. Since each level's `exported_pins` is now legal-by-construction, classification behaviour for wires *inside the group's territory* is unchanged. But: when classifying for a parent group A whose child B exposes c1, the wire from c1 to outside-of-A pin is owned by neither C nor B nor A unless A re-exports c1 — which is the correct behaviour. No code change, semantics fall out naturally.

## 4. Data migration — concrete example

Before refactor: `micek2/layout.yaml`'s "Group5" exports pin c1 of a component that's actually inside a sub-group (call it Group5a) but Group5a isn't part of the tree.

Likely current state: many groups have flat exports that don't honour the chain. Auto-migration (§3.3) strips them on load. The user opens `micek2`, the DRC panel shows the same nets that lost their exposed pins as `GROUP_MISSING_EXPORT` issues, and the user re-runs Export Auto on each group from leaves to root.

Alternative (gentler): keep stripped entries in a session-only `pending_exports_to_review` map; surface a "X exports were dropped on load, review" toast. Out of scope for this refactor; add later if migration churn proves painful.

## 5. Test plan

New tests in `tests/unit/test_groups.py`:

- `test_exposable_surface_direct_pins_only` — leaf group, surface = pins of its direct components.
- `test_exposable_surface_includes_child_reexports` — A with child B, B exports c1; surface(A) includes c1.
- `test_exposable_surface_does_not_include_grandchild` — A[B[C]], C exports c1 but B doesn't re-export; surface(A) excludes c1.
- `test_validate_exported_pins_rejects_grandchild_flat_export` — A.exported_pins contains c1 (pin of C), B doesn't re-export → validator returns c1 as invalid.
- `test_validate_exported_pins_accepts_chain` — same setup but B also exports c1 → A's c1 is valid.
- `test_external_pins_for_group_respects_child_interface` — auto-export at A produces re-exports only of pins that B already exposes (not bare descendants).
- `test_load_migration_strips_orphan_export` — synthetic project YAML with flat export from grandchild; after load, A.exported_pins drops it; INFO log emitted.

DRC tests:

- `test_drc_group_invalid_export_grandchild_flat` — A flat-exports c1 without B re-exporting → `GROUP_INVALID_EXPORT`.
- `test_drc_group_invalid_export_resolved_by_chain` — same setup but B re-exports → no issue.

Persistence:

- `test_round_trip_preserves_encapsulated_export_chain` — save & load, every `exported_pins` survives unchanged.
- `test_round_trip_strips_legacy_flat_export` — load YAML with flat export, save, the stripped pin doesn't reappear.

## 6. Rollout order

1. Add `exposable_surface` and `validate_exported_pins` helpers + tests. Pure addition, no behaviour change.
2. Add `external_pins_for_group` helper + tests. Old `external_pins_for_components` stays in place initially.
3. Switch `_group_export_auto` and `_group_for_selection` to the new helper. User-visible change: Auto-export from A in `A[B[C]]` now produces fewer pins (only direct + already-re-exported-by-B). Compare against expectation on a tree project before committing.
4. Add DRC `GROUP_INVALID_EXPORT` check + tests.
5. Add loader auto-migration + tests.
6. Rewrite per-pin PPM (§3.6) so the user can build chains by clicking re-export rows on each level.
7. Fix `net_is_internal_to_group` recursion bug (out-of-scope but folded in for cleanliness).
8. Drop the old `external_pins_for_components` once no caller remains. (Or keep as a low-level utility, callers explicit about "I want flat across recursive set" — depends on whether autoroute group projection still wants flat.)

## 7. Open questions

- **Group disband (`DisbandGroupCommand`)**: when B is disbanded, its `exported_pins` lose their carrier. Should A's `exported_pins` entries that depended on B's chain be auto-stripped, or should disband refuse with a warning that A still references B's exports? Probably auto-strip with an INFO log — same idiom as load migration. Decide before implementing.
- **Re-grouping (Ctrl+G inside A that pulls B and some loose comps into a new C)**: does the auto-export on C include the chain from B (since B is a direct child of C now)? Yes, by §3.4 — but verify with a unit test once the helper exists.
- **Per-pin "Export pin" PPM on a member of A in the new model**: enabling export means adding ep to A.exported_pins. Validator accepts because the pin's component is in `A.component_ids`. Fine. But the same pin in A is also in `all_component_ids(A)`, so for the parent of A, re-exporting it requires A to expose it first — chain works automatically. Document this in PPM hint.

## 8. Effort estimate

- 1 commit for §3.1 helpers + tests.
- 1 commit for §3.2 DRC check + tests.
- 1 commit for §3.3 loader migration + tests.
- 1 commit for §3.4 + §3.5 GUI switch.
- 1 commit for §3.6 PPM refactor (largest UI change).
- 1 commit cleanup (`net_is_internal_to_group` fix, drop old helper if unused).

~6 commits, all small. No persistence schema change → no `.yaml` versioning required.
