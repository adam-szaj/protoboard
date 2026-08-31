# Canonical Domain Queries and Legacy Layout API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace repeated component, group, wire, pad, and segment-resolution
idioms with canonical domain operations and remove the superseded group-query
API.

**Architecture:** Add one pure component-context query in `domain/components`,
use the existing indexed layout collections and `GroupTree` directly, and move
only mathematically identical segment primitives to `geometry.py`. Routing
policy remains in routing services; missing component records remain explicit
through `None`, while invalid package selections continue to raise.

**Tech Stack:** Python 3.11+, dataclasses, pytest, Hypothesis, Ruff, Black, mypy,
Pyright.

**Spec:** `docs/superpowers/specs/2026-08-30-module-boundaries-and-deduplication-design.md`

## Global Constraints

- Preserve observable application behaviour and command/undo semantics.
- Do not add compatibility aliases or preserve superseded internal APIs.
- Do not change the component/package model or YAML schema in this plan.
- Keep routing-specific crossing policy in routing modules.
- Preserve comments that explain geometry, performance, persistence, or undo
  invariants; remove only comments made obsolete by these changes.
- Treat the one-third physical LOC reduction as aspirational, not an acceptance
  gate.
- Commit every completed task separately; do not merge the branch until the
  complete parent goal is finished.
- Defer interactive GUI smoke testing to the final parent-goal gate.

---

### Task 1: Canonical component context resolution

**Files:**
- Create: `src/protoboard/domain/components/resolution.py`
- Modify: `src/protoboard/domain/components/__init__.py`
- Modify: `src/protoboard/domain/layout/group_tree.py`
- Modify: `src/protoboard/domain/layout/group_ownership.py`
- Test: `tests/unit/test_component_resolution.py`
- Test: `tests/unit/test_group_tree.py`
- Test: `tests/unit/test_group_ownership.py`

**Interfaces:**
- Consumes: `PlacementMap`, component-instance and definition mappings,
  `resolve_package_option`, and a bound instance's `resolved_package()`.
- Produces: `ResolvedComponent(instance, definition, placement,
  package_option, package)` and
  `resolve_component(component_id, placements, instances, definitions) ->
  ResolvedComponent | None`.

- [x] **Step 1: Write failing resolver tests**

Cover the complete result, each missing structural record, an unbound instance
whose `package` is `None`, and propagation of an invalid package selection:

```python
resolved = resolve_component(cid, placements, instances, definitions)
assert resolved is not None
assert resolved.instance is instance
assert resolved.definition is definition
assert resolved.placement is placement
assert resolved.package_option is definition.package_options[0]
assert resolved.package is package

assert resolve_component(cid, {}, instances, definitions) is None
assert resolve_component(cid, placements, {}, definitions) is None
assert resolve_component(cid, placements, instances, {}) is None
```

- [x] **Step 2: Verify the new tests fail**

Run:

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_component_resolution.py -q
```

Expected: collection fails because `resolution.py` does not exist.

- [x] **Step 3: Implement the pure query**

Use a frozen, slotted dataclass. Return `None` only when placement, instance, or
definition is absent. Resolve the selected option without suppressing
`ValueError`; set the concrete package only when the instance is bound:

```python
@dataclass(frozen=True, slots=True)
class ResolvedComponent:
    instance: ComponentInstance
    definition: ComponentDefinition
    placement: ComponentPlacement
    package_option: PackageOption
    package: Package | None


def resolve_component(
    component_id: uuid.UUID,
    placements: PlacementMap,
    instances: dict[uuid.UUID, ComponentInstance],
    definitions: dict[str, ComponentDefinition],
) -> ResolvedComponent | None:
    placement = placements.get(component_id)
    instance = instances.get(component_id)
    if placement is None or instance is None:
        return None
    definition = definitions.get(instance.definition_ref)
    if definition is None:
        return None
    option = resolve_package_option(definition, instance)
    package = instance.resolved_package() if instance.definition is not None else None
    return ResolvedComponent(instance, definition, placement, option, package)
```

- [x] **Step 4: Migrate two complete repeated pipelines**

Use the query in `GroupTree.group_origin` and
`group_ownership.group_bbox_with_owned_wires`. Each caller must explicitly skip
`None` and `package is None`; obtain footprint geometry from the resolved
package. Do not migrate partial lookups that do not need placement or package.

- [x] **Step 5: Run focused and static checks**

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_component_resolution.py tests/unit/test_group_tree.py tests/unit/test_group_ownership.py -q
/home/yvdev/.local/venv/bin/uv run ruff check src/protoboard/domain/components/resolution.py src/protoboard/domain/layout/group_tree.py src/protoboard/domain/layout/group_ownership.py tests/unit/test_component_resolution.py
/home/yvdev/.local/venv/bin/uv run mypy src/protoboard/domain/components/resolution.py src/protoboard/domain/layout/group_tree.py src/protoboard/domain/layout/group_ownership.py
```

Expected: PASS.

- [x] **Step 6: Commit**

```bash
git add src/protoboard/domain/components/resolution.py src/protoboard/domain/components/__init__.py src/protoboard/domain/layout/group_tree.py src/protoboard/domain/layout/group_ownership.py tests/unit/test_component_resolution.py
git commit -m "refactor(domain): add canonical component resolution"
```

### Task 2: Delete free-function group query shims

**Files:**
- Modify: `src/protoboard/domain/layout/group.py`
- Modify: `src/protoboard/domain/commands/group_cmds.py`
- Modify: `src/protoboard/domain/layout/group_ownership.py`
- Modify: `src/protoboard/domain/layout/pad_conflict.py`
- Modify: `src/protoboard/controller/editing/controller.py`
- Modify: `src/protoboard/controller/editing/drag_set.py`
- Modify: `src/protoboard/controller/tools/select_tool.py`
- Modify: `src/protoboard/services/drc/engine.py`
- Modify: `src/protoboard/services/persistence/project_yaml.py`
- Modify: `src/protoboard/services/routing/hierarchical.py`
- Modify: tests that import the removed functions from `domain.layout.group`
- Test: `tests/unit/test_group_tree.py`
- Test: `tests/unit/test_layer_import_contract.py`

**Interfaces:**
- Consumes: `GroupTree(groups)` and existing methods with unchanged semantics.
- Produces: `domain.layout.group` exporting only `ComponentGroup`; every query
  caller uses `GroupTree` or `session.group_tree()`.

- [x] **Step 1: Add a failing legacy-API contract**

Add an AST assertion that `group.py` defines none of the eleven superseded
query functions and that production files do not import those names from it.
Keep behavioural tests on `GroupTree` as the source of truth.

- [x] **Step 2: Verify the contract fails**

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_layer_import_contract.py tests/unit/test_group_tree.py -q
```

Expected: FAIL listing the current shim definitions/imports.

- [x] **Step 3: Migrate production callers**

Instantiate one `GroupTree(groups)` per coherent operation and replace calls,
for example:

```python
tree = GroupTree(state.layout.groups)
component_ids = tree.all_component_ids(group_id)
if tree.would_create_cycle(parent_id, child_id):
    ...
```

For `internal_net_ids_for_components` and `schematic_projection_for_group`, use
the already available tree or `GroupTree({})` only where no group mapping
exists. Do not add replacement wrappers.

- [x] **Step 4: Migrate tests and remove compatibility-only parity tests**

Change behavioural tests to construct `GroupTree(groups)`. Delete the parity
test that merely compares each shim with the corresponding method. Preserve
tests of the actual group behaviour.

- [x] **Step 5: Delete all eleven shims**

Remove `all_component_ids`, `exposable_surface`, `validate_exported_pins`,
`top_level_group_for`, `external_pins_for_group`,
`internal_net_ids_for_components`, `schematic_projection_for_group`,
`would_create_cycle`, `top_level_group_under`,
`promote_selection_to_top_level`, and `parent_group_of` from `group.py`, along
with imports made unused by their deletion.

- [x] **Step 6: Verify focused behaviour and absence**

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_group_tree.py tests/unit/test_groups.py tests/unit/test_edit_controller_group.py tests/unit/test_group_membership_command.py tests/unit/test_group_ownership.py tests/unit/test_drc.py -q
rg -n 'from protoboard\.domain\.layout\.group import .*\b(all_component_ids|exposable_surface|validate_exported_pins|top_level_group_for|external_pins_for_group|internal_net_ids_for_components|schematic_projection_for_group|would_create_cycle|top_level_group_under|promote_selection_to_top_level|parent_group_of)\b' src tests
```

Expected: tests pass and `rg` has no output.

- [x] **Step 7: Commit**

```bash
git add src tests/unit tests/gui
git commit -m "refactor(layout): remove group query shims"
```

### Task 3: Use indexed wire lookup at live-layout call sites

**Files:**
- Modify: `src/protoboard/headless_session.py`
- Modify: `src/protoboard/gui/view_camera.py`
- Modify: `src/protoboard/controller/tools/simplify_tool.py`
- Modify: `src/protoboard/controller/tools/select_tool.py`
- Modify: `src/protoboard/controller/tools/wire_edit_tool.py`
- Modify: `src/protoboard/controller/editing/placement_conflict.py`
- Modify: `src/protoboard/domain/layout/pad_conflict.py`
- Test: `tests/unit/test_wire_collection_lookup.py`
- Test: closest existing tool and conflict tests for each changed path

**Interfaces:**
- Consumes: `WireCollection.get(wire_id) -> Wire | None` and
  `WireCollection.values()`.
- Produces: no scan-based `next((wire ... if wire.id == ...), None)` against a
  live `Layout.wires` collection.

- [x] **Step 1: Characterize lookup parity**

Extend `test_wire_collection_lookup.py` to assert hit, miss, insertion-order
iteration, and UUID membership. These behaviours permit replacing scans
without changing selection order.

- [x] **Step 2: Run characterization tests**

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_wire_collection_lookup.py -q
```

Expected: PASS before implementation.

- [x] **Step 3: Replace live collection scans**

Use `layout.wires.get(wire_id)` at every listed live-layout call site. For
worker snapshots, create one `dict[UUID, Wire]` before the loop and use
`snapshot_by_id.get(wid)`; retain `Sequence[Wire]` scans only in functions that
genuinely accept arbitrary sequences.

- [x] **Step 4: Verify focused behaviour and audit scans**

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_wire_collection_lookup.py tests/unit/test_placement_conflict_variant.py tests/unit/test_pad_conflict.py tests/gui/test_select_tool.py tests/gui/test_wire_edit_tool.py -q
rg -n 'next\(\(w for w in ((self\._session|session)\.layout|layout)\.wires if w\.id ==' src/protoboard
```

Expected: tests pass and the audit has no output.

- [x] **Step 5: Commit**

```bash
git add src/protoboard/headless_session.py src/protoboard/gui/view_camera.py src/protoboard/controller/tools/simplify_tool.py src/protoboard/controller/tools/select_tool.py src/protoboard/controller/tools/wire_edit_tool.py src/protoboard/controller/editing/placement_conflict.py src/protoboard/domain/layout/pad_conflict.py tests/unit/test_wire_collection_lookup.py
git commit -m "refactor(layout): use indexed wire lookup"
```

### Task 4: Reuse the canonical plated-through-pad query

**Files:**
- Modify: `src/protoboard/services/routing/autorouter.py`
- Modify: `src/protoboard/services/routing/cost_analyzer.py`
- Test: `tests/unit/test_wire_utils.py`
- Test: `tests/unit/test_autorouter.py`
- Test: `tests/unit/test_cost_analyzer.py`

**Interfaces:**
- Consumes: `domain.layout.wire_utils.plated_through_pads(board) -> set[Point]`.
- Produces: no private routing copy of board plated-pad enumeration.

- [ ] **Step 1: Add parity fixtures for pad and strip atoms**

Ensure the canonical query test covers horizontal and vertical plated strips,
a plated single pad, and non-plated atoms that must be excluded.

- [ ] **Step 2: Run characterization tests**

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_wire_utils.py::TestBoardPadsForSide tests/unit/test_cost_analyzer.py -q
```

Expected: PASS before implementation.

- [ ] **Step 3: Replace both duplicate enumerators**

Import `plated_through_pads` in autorouter and cost analyzer, replace
`_collect_plated_through_pads(board)` and `_plated_through_pads(board)`, then
delete both private functions and their now-unused `PadAtom`/`StripAtom`
imports only where unused.

- [ ] **Step 4: Verify routing behaviour and absence**

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_wire_utils.py tests/unit/test_autorouter.py tests/unit/test_cost_analyzer.py -q
rg -n 'def (_collect_plated_through_pads|_plated_through_pads)' src/protoboard/services/routing
```

Expected: tests pass and the audit has no output.

- [ ] **Step 5: Commit**

```bash
git add src/protoboard/services/routing/autorouter.py src/protoboard/services/routing/cost_analyzer.py tests/unit/test_wire_utils.py
git commit -m "refactor(routing): reuse canonical plated pad query"
```

### Task 5: Move shared segment predicates to geometry

**Files:**
- Modify: `src/protoboard/geometry.py`
- Modify: `src/protoboard/services/routing/astar.py`
- Modify: `src/protoboard/services/routing/postprocess.py`
- Modify: `src/protoboard/services/routing/cost_analyzer.py`
- Test: `tests/unit/test_geometry.py`
- Test: `tests/unit/test_astar_rust_parity.py`
- Test: `tests/unit/test_routing_postprocess.py`
- Test: `tests/unit/test_cost_analyzer.py`

**Interfaces:**
- Produces: `orientation(a, b, point) -> float`,
  `segments_cross_strict(a1, a2, b1, b2) -> bool`,
  `segment_intersection_point(a1, a2, b1, b2) -> tuple[float, float]`, and
  `point_on_segment_interior(point, a, b, *, eps) -> bool`.
- Preserves: exact strict-crossing and collinear-fallback behaviour; callers
  pass their existing epsilon explicitly where it differs.

- [ ] **Step 1: Add property and example tests**

Test strict crossing symmetry, endpoint-only contact exclusion, collinear
overlap exclusion, orientation sign, known intersection coordinates,
degenerate/collinear fallback, and interior-point endpoint exclusion. Add a
Hypothesis property asserting
`segments_cross_strict(a, b, c, d) == segments_cross_strict(c, d, a, b)`.

- [ ] **Step 2: Verify tests fail**

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_geometry.py -q
```

Expected: FAIL because the public primitives do not exist.

- [ ] **Step 3: Implement the primitives and migrate consumers**

Move the identical mathematics without policy into `geometry.py`. Replace
private A* functions and both point-on-interior copies. Keep
`_segs_cross_or_touch`, overlap handling, pad classification, and perpendicular
diagonal policy in routing modules, calling the shared primitives.

- [ ] **Step 4: Verify geometry and routing parity**

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_geometry.py tests/unit/test_astar_rust_parity.py tests/unit/test_routing_postprocess.py tests/unit/test_cost_analyzer.py -q
/home/yvdev/.local/venv/bin/uv run pytest tests/property -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/protoboard/geometry.py src/protoboard/services/routing/astar.py src/protoboard/services/routing/postprocess.py src/protoboard/services/routing/cost_analyzer.py tests/unit/test_geometry.py
git commit -m "refactor(geometry): centralize segment predicates"
```

### Task 6: Workstream 3A verification and status record

**Files:**
- Modify: `docs/superpowers/plans/2026-08-31-canonical-domain-queries-and-legacy-layout-api.md`
- Modify: `docs/superpowers/specs/2026-08-30-module-boundaries-and-deduplication-design.md`
- Test: `tests/unit/test_layer_import_contract.py`

**Interfaces:**
- Produces: verified Workstream 3A exit record and identifies serialization,
  export, MRU, snapping, tool execution, and action metadata as remaining 3B/3C
  work.

- [ ] **Step 1: Run the full automated gate**

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit -q
QT_QPA_PLATFORM=offscreen /home/yvdev/.local/venv/bin/uv run pytest tests/gui -q
/home/yvdev/.local/venv/bin/uv run pytest tests/property -q
/home/yvdev/.local/venv/bin/uv run ruff check src tests
/home/yvdev/.local/venv/bin/uv run black --check src tests
/home/yvdev/.local/venv/bin/uv run mypy src
/home/yvdev/.local/venv/bin/uv run pyright
git diff --check
```

Expected: all commands pass; record existing xfails and warnings separately.

- [ ] **Step 2: Audit superseded paths**

Run the exact absence searches from Tasks 2–5 and verify the blanket layer
contract still passes. Record removed functions and migrated call-site counts.

- [ ] **Step 3: Update design and plan status**

Mark this plan complete, add automated gate results and LOC metrics, and mark
Workstream 3B (serialization/export/MRU) next. Interactive GUI smoke testing
remains explicitly deferred.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/plans/2026-08-31-canonical-domain-queries-and-legacy-layout-api.md docs/superpowers/specs/2026-08-30-module-boundaries-and-deduplication-design.md tests/unit/test_layer_import_contract.py
git commit -m "docs(architecture): complete canonical domain queries"
```

## Self-Review Checklist

- [ ] Every new abstraction has at least two production consumers.
- [ ] Missing component records return `None`; invalid package choices are not
  hidden.
- [ ] No group query compatibility wrapper or re-export remains.
- [ ] Live wire lookups are indexed; arbitrary sequence helpers remain honest
  about linear lookup.
- [ ] Plated-pad enumeration has one implementation.
- [ ] Geometry owns only mathematical predicates; routing owns policy.
- [ ] Every task has its own verified commit and no merge was performed.
