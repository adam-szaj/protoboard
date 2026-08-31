<!-- SPDX-License-Identifier: MIT -->
# Narrow Editing Contexts and Routing Ports Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the broad editing-state contract into coherent read/effect/
interaction contexts and remove every concrete routing-service import from the
controller layer without changing edit, routing, undo, or GUI behaviour.

**Architecture:** Session objects remain composition objects that structurally
satisfy several narrow controller-owned protocols. `EditController` accepts a
composite `EditingContext`, while helpers depend on the smallest coherent
constituent and `PreviewState` implements only the project/spatial preview
surface. Four routing ports—pathfinding, simplification, optimization, and
unweaving—hide service implementations behind controller-owned domain-shaped
requests and results; default adapters live in services.

**Tech Stack:** Python 3.11+, pytest, PySide6, standard-library `ast`, Ruff,
Black, strict mypy, Pyright, uv

**Spec:** `docs/superpowers/specs/2026-08-30-module-boundaries-and-deduplication-design.md`

## Global Constraints

- Preserve runtime behaviour, routing choices, visible messages, command and
  undo grouping, cancellation, persistence, and export output.
- Do not change the component/package domain model or YAML schema.
- Do not add automatic delegation, compatibility aliases, old-path re-exports,
  service defaults inside controller code, or import-contract allow-lists.
- Ports expose domain values and controller DTOs only; service request/result,
  registry, optimizer, progress-sink, and Qt types cannot appear in controller
  annotations.
- Keep geometry/routing/concurrency/undo invariant comments. Remove only prose
  made obsolete by the changed boundary.
- Add or update the closest focused test before behaviour-affecting changes.
- Use `/home/yvdev/.local/venv/bin/uv` for project commands.
- Commit after every completed task. Do not merge until the complete parent
  refactoring goal is finished.
- Defer interactive manual GUI verification until the final parent-workstream
  audit; use offscreen automated GUI coverage in this plan.

## Baseline and Exit Criteria

At plan creation, `src/protoboard` contains 312 Python modules and 96,895
physical lines. `EditState` declares 28 members and `PreviewState` is 194 lines.
Fourteen AST-visible controller imports reference concrete routing services in
`route_tool.py`, `route_preview.py`, `simplify_tool.py`, `select_tool.py`,
`selection/wire_edit.py`, and `editing/placement_conflict.py`.

This plan is complete when:

- `EditState` no longer exists and every consumer uses `EditingContext` or a
  narrower constituent protocol;
- `PreviewState` exposes only the preview project/spatial surface used by snap
  and drag classification;
- all routing implementation selection and service DTO construction lives in
  service adapters;
- the generic AST contract forbids both `protoboard.services` and
  `protoboard.gui` everywhere under `controller/`, with zero allow-listed
  modules;
- focused tests, full unit and offscreen GUI suites, Ruff, Black, strict mypy,
  Pyright, and property tests pass.

## Explicitly Deferred

- Canonical component resolution, collection lookup, snapping, serialization,
  MRU storage, action metadata, and legacy deletion belong to Workstream 3.
- Shared export rendering extraction and large GUI/controller decomposition
  belong to Workstream 4.
- Interactive create/place/drag/wire/save/export verification is performed at
  the final parent-goal gate.

---

### Task 1: Replace `EditState` with coherent context protocols

**Files:**
- Create: `src/protoboard/controller/editing/contexts.py`
- Modify: `src/protoboard/controller/editing/controller.py`
- Modify: `src/protoboard/controller/editing/__init__.py`
- Modify: controller callers returned by
  `rg -l 'EditState' src/protoboard/controller -g '*.py'`
- Modify: `tests/unit/test_view_proto_parity.py`
- Create: `tests/unit/test_editing_contexts.py`

**Interfaces:**
- `ProjectModel`: `SchematicState`, package families, and axis-label settings.
- `SpatialQueries`: component bounds, pin lookup/iteration, component/wire
  indices, meta-pin lookup, and cached plated-through pads.
- `ConnectivityQueries`: wire lookup, group tree, wire-net map, conflict
  scanner, pad index, and connectivity graph.
- `EditingEffects`: command stack, model notification, and graph invalidation.
- `InteractionState`: selection, tool/selection notifications, controller,
  routing settings/net, selection service, and highlight stack.
- `EditingContext`: composite of the five protocols; accepted only by the main
  controller/tool composition seams.
- `PreviewProjectContext`: project data plus the four preview reads actually
  used by snap/drag: `comp_spatial_index`, `iter_pin_positions`, `group_tree`,
  and `cached_wire_net_map`.

- [x] **Step 1: Add failing context-surface tests**

In `tests/unit/test_editing_contexts.py`, reuse the live headless-session fixture
and assert it satisfies every runtime-checkable constituent and the composite:

```python
def test_headless_session_satisfies_editing_contexts() -> None:
    session = _headless_session()
    assert isinstance(session, ProjectModel)
    assert isinstance(session, SpatialQueries)
    assert isinstance(session, ConnectivityQueries)
    assert isinstance(session, EditingEffects)
    assert isinstance(session, InteractionState)
    assert isinstance(session, EditingContext)
```

Add an AST assertion that no class named `EditState` remains after migration.

- [x] **Step 2: Run the new test and verify the intended failure**

Run:

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_editing_contexts.py -q
```

Expected: FAIL because `controller.editing.contexts` does not exist.

- [x] **Step 3: Define the protocols without runtime implementations**

Create `contexts.py` using `@runtime_checkable` and `Protocol`. Keep all heavy
domain/controller types in `TYPE_CHECKING`; use only exact existing signatures.
The composite is declaration-only:

```python
@runtime_checkable
class EditingContext(
    ProjectModel,
    SpatialQueries,
    ConnectivityQueries,
    EditingEffects,
    InteractionState,
    Protocol,
):
    pass
```

Do not add adapters or `__getattr__`; `HeadlessSession` and `EditorSession`
satisfy the protocols structurally.

- [x] **Step 4: Repoint the broad type name mechanically**

Move the protocol out of `controller.py`, rename broad uses to
`EditingContext`, and export only `EditController`, `EditingContext`, and
`ResolvedPlacement` from the appropriate modules. Replace all controller
imports of `EditState`; do not leave `EditState = EditingContext`.

- [x] **Step 5: Verify type and parity contracts**

Run:

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_editing_contexts.py tests/unit/test_view_proto_parity.py tests/unit/test_controller_facade.py -q
/home/yvdev/.local/venv/bin/uv run mypy src/protoboard/controller src/protoboard/headless_session.py
/home/yvdev/.local/venv/bin/uv run pyright
rg -n '\bEditState\b' src tests
```

Expected: tests/type checks pass and the search has no output.

- [x] **Step 6: Commit the context vocabulary**

```bash
git add src/protoboard/controller tests/unit/test_editing_contexts.py tests/unit/test_view_proto_parity.py
git commit -m "refactor(controller): split editing context contracts"
```

---

### Task 2: Shrink `PreviewState` to the preview project surface

**Files:**
- Modify: `src/protoboard/controller/editing/preview_state.py`
- Modify: `src/protoboard/controller/editing/armed_transform.py`
- Modify: `src/protoboard/controller/editing/drag_commit.py`
- Modify: `src/protoboard/controller/editing/controller.py`
- Modify: `tests/unit/test_view_proto_parity.py`
- Modify: `tests/unit/test_layout_preview_copy.py`
- Modify: `tests/unit/test_classify_drag_commit.py`

**Interfaces:**
- `PreviewState(live: PreviewProjectContext, preview: Layout)` implements
  `PreviewProjectContext` only.
- Preview-owned reads: `layout`, `comp_spatial_index()`, and
  `iter_pin_positions()`.
- Delegated reads: project values, `group_tree()`, and
  `cached_wire_net_map()`.

- [x] **Step 1: Replace broad parity with an exact preview contract test**

Change the parity test to compare `PreviewProjectContext` members and assert
interaction/effect members are absent:

```python
def test_preview_state_is_only_a_preview_project_context() -> None:
    preview = PreviewState(live, live.layout.preview_copy())
    assert isinstance(preview, PreviewProjectContext)
    assert not hasattr(preview, "selection")
    assert not hasattr(preview, "notify_changed")
    assert not hasattr(preview, "edit_controller")
    assert not hasattr(preview, "highlight_stack")
    assert not hasattr(preview, "routing_settings")
```

- [x] **Step 2: Run the exact surface test and verify it fails**

Run:

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_view_proto_parity.py::test_preview_state_is_only_a_preview_project_context -q
```

Expected: FAIL because the current 194-line proxy exposes interaction/effects.

- [x] **Step 3: Delete unused delegation explicitly**

Keep only the constructor fields and methods listed in the interface block.
Do not replace them with `__getattr__`. Update `ArmedTransform`, drag
classification, and the preview branch of `EditController.snap()` to accept
`PreviewProjectContext` rather than the composite context.

- [x] **Step 4: Verify preview geometry and drag behaviour**

Run:

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_view_proto_parity.py tests/unit/test_layout_preview_copy.py tests/unit/test_classify_drag_commit.py tests/unit/test_edit_controller_armed_transform.py tests/gui/test_armed_transform_tool.py -q
/home/yvdev/.local/venv/bin/uv run mypy src/protoboard/controller/editing
```

Expected: PASS; preview pins/index reflect the preview layout and hard/soft drag
classification remains unchanged.

- [x] **Step 5: Commit the preview boundary**

```bash
git add src/protoboard/controller/editing tests/unit/test_view_proto_parity.py tests/unit/test_layout_preview_copy.py tests/unit/test_classify_drag_commit.py
git commit -m "refactor(controller): narrow preview editing context"
```

---

### Task 3: Type helpers against constituent read contexts

**Files:**
- Modify: `src/protoboard/controller/editing/contexts.py`
- Modify: `src/protoboard/controller/editing/group_lock.py`
- Modify: `src/protoboard/controller/editing/wire_nets.py`
- Modify: `src/protoboard/controller/editing/placement_conflict.py`
- Modify: `src/protoboard/controller/editing/drag_commit.py`
- Modify: `src/protoboard/controller/selection/queries.py`
- Modify: `src/protoboard/controller/selection/wire_ops.py`
- Modify: `src/protoboard/controller/tools/simplify_tool.py`
- Create: `tests/unit/test_context_import_contract.py`

**Interfaces:**
- `ProjectSpatialContext(ProjectModel, SpatialQueries, Protocol)`.
- `ProjectConnectivityContext(ProjectModel, ConnectivityQueries, Protocol)`.
- `EditingReadContext(ProjectModel, SpatialQueries, ConnectivityQueries,
  Protocol)`.
- `RoutingReadContext(EditingReadContext, Protocol)` adds routing settings.

- [x] **Step 1: Add an AST guard for helper signatures**

The new test parses the listed helper modules and fails when they import or
annotate `EditingContext`. It does not inspect controller/tool composition
classes, where the composite remains intentional.

- [x] **Step 2: Run the helper guard and verify current offenders**

Run:

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_context_import_contract.py -q
```

Expected: FAIL listing the helper files still importing the broad context.

- [x] **Step 3: Repoint helpers to coherent context combinations**

Use these mappings:

```text
wire_nets                  ProjectModel
drag_commit                PreviewProjectContext
group_lock                 ProjectConnectivityContext
placement_conflict         EditingReadContext
selection.queries          ProjectConnectivityContext
selection.wire_ops         EditingReadContext
simplify_tool computation  RoutingReadContext
```

Do not change arguments, return values, mutations, or exception behaviour.

- [x] **Step 4: Run focused behaviour and strict typing**

Run:

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_context_import_contract.py tests/unit/test_wire_net_resolver.py tests/unit/test_group_ownership.py tests/unit/test_component_model_invariants.py tests/unit/test_selection_queries.py tests/gui/test_simplify_tool.py -q
/home/yvdev/.local/venv/bin/uv run mypy src/protoboard/controller
/home/yvdev/.local/venv/bin/uv run pyright
```

Expected: PASS with no casts from a narrow context back to `EditingContext`.

- [x] **Step 5: Commit the narrow helper signatures**

```bash
git add src/protoboard/controller tests/unit/test_context_import_contract.py
git commit -m "refactor(controller): narrow editing helper contexts"
```

---

### Task 4: Define routing ports and default service adapters

**Files:**
- Create: `src/protoboard/controller/ports/routing.py`
- Modify: `src/protoboard/controller/ports/edit_services.py`
- Create: `src/protoboard/services/editing/routing_adapters.py`
- Modify: `src/protoboard/services/editing/adapters.py`
- Modify: `tests/unit/test_edit_service_ports.py`
- Create: `tests/unit/test_routing_service_ports.py`

**Interfaces:**
- `RoutePath`: read-only `path: Sequence[tuple[Point, str]]`, matching the
  existing top/bottom path-step representation without exposing A* types.
- `SimplifyInputs`: immutable controller DTO containing pin positions,
  pad/net maps, board pads, wire/net map, and `PadIndex`; its
  `request(wire, other_wires, mode) -> SimplifyRequest` method binds one
  operation to the shared input snapshot.
- `SimplifyRequest`: wire, other wires, inputs, and `SimplifyMode`.
- `OptimizerOption(name: str, label: str, notes: str)`.
- `UnweaveResult(wires: Sequence[Wire], before_crossings: int,
  after_crossings: int)`.
- `PathfinderPort.find_paths(...) -> Sequence[RoutePath]`.
- `FindPaths`: callable protocol with the same keyword-only signature as
  `PathfinderPort.find_paths`.
- `WireSimplifierPort.simplify(request: SimplifyRequest) -> Wire`.
- `SimplifyWire`: callable protocol accepting one `SimplifyRequest`.
- `WireOptimizerPort.options() -> Sequence[OptimizerOption]` and
  `optimize(name, ..., wires: Sequence[Wire]) -> Sequence[Wire]`.
- `WireUnweaverPort.unweave(..., wires: Sequence[Wire]) -> UnweaveResult`.
- `RoutingServicePorts`: frozen bundle of the four ports.
- `EditServicePorts.routing`: mandatory; no controller-side default.

- [ ] **Step 1: Add recording-fake contract tests**

Construct `RoutingServicePorts` from four recording fakes and add it to every
`EditServicePorts` test fixture. Assert the request/result DTOs contain only
domain/controller types and preserve wire/path order.

- [ ] **Step 2: Run the port tests and verify the missing API failure**

Run:

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_routing_service_ports.py tests/unit/test_edit_service_ports.py -q
```

Expected: FAIL because the routing port module and required bundle member do
not exist.

- [ ] **Step 3: Implement port declarations**

Use `Protocol`, frozen dataclasses, `Sequence`, and `Mapping`. Do not import
`services.routing` in this module, including under `TYPE_CHECKING`.

- [ ] **Step 4: Implement concrete adapters**

Adapters delegate exactly as follows:

```text
DefaultPathfinder       services.routing.astar.find_paths
DefaultWireSimplifier   services.routing.postprocess.generic_simplify_wire
DefaultWireOptimizer    builtin optimizer registration + registry +
                        RouteRequest + AutorouteResult + ProgressSink
DefaultWireUnweaver     unweave_wires + count_mutual_crossings
```

The optimizer adapter converts service objects to `Sequence[Wire]`; the
unweaver computes and returns both crossing counts. Preserve plugin exceptions
for the controller caller to format as the existing status message.

- [ ] **Step 5: Wire default composition and run adapter tests**

Update `default_edit_service_ports()` and all fakes. Run:

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_routing_service_ports.py tests/unit/test_edit_service_ports.py tests/unit/test_optimizer_discovery.py tests/unit/test_unweave.py -q
/home/yvdev/.local/venv/bin/uv run mypy src/protoboard/controller/ports src/protoboard/services/editing
/home/yvdev/.local/venv/bin/uv run pyright
```

Expected: PASS; controller port modules contain no service import.

- [ ] **Step 6: Commit routing port composition**

```bash
git add src/protoboard/controller/ports src/protoboard/services/editing tests/unit/test_edit_service_ports.py tests/unit/test_routing_service_ports.py
git commit -m "refactor(routing): define application routing ports"
```

---

### Task 5: Route all A* pathfinding through `PathfinderPort`

**Files:**
- Modify: `src/protoboard/controller/editing/controller.py`
- Modify: `src/protoboard/controller/editing/placement_conflict.py`
- Modify: `src/protoboard/controller/editing/wire_edit_arm.py`
- Modify: `src/protoboard/controller/selection/wire_edit.py`
- Modify: `src/protoboard/controller/tools/route_tool.py`
- Modify: `src/protoboard/controller/tools/route_preview.py`
- Modify: `src/protoboard/gui/tools/route_preview.py`
- Modify: `src/protoboard/gui/renderers/routing_overlay.py`
- Modify: `src/protoboard/gui/placement_conflict.py`
- Modify: `tests/unit/test_edit_service_ports.py`
- Modify: `tests/unit/test_wire_edit_arm.py`
- Modify: `tests/gui/test_route_tool.py`
- Modify: `tests/gui/test_place_tool.py`

**Interfaces:**
- `EditController.find_paths(...) -> Sequence[RoutePath]` delegates to the
  pathfinder port using explicit domain arguments.
- Placement conflict receives a `PathfinderPort` explicitly.
- `WireEditArm` receives a `FindPaths` callable at construction; only its
  `autoroute()` operation requires it.
- Route preview/rendering consumes `Sequence[RoutePath]`.

- [ ] **Step 1: Add delegation tests for the three pathfinding callers**

Use a recording pathfinder to prove route hints, placement reroute, and wire
fragment autoroute forward graph/source/targets/side/cost/k/insulation/net id
without importing A*.

- [ ] **Step 2: Run one converted caller test and verify it fails**

Run:

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_wire_edit_arm.py -q
```

Expected: FAIL until `WireEditArm` accepts the injected finder.

- [ ] **Step 3: Replace direct A* calls**

Route tool calls `session.edit_controller.find_paths`. Controller placement
passes its pathfinder to conflict resolution. Wire edit passes the controller
finder into `WireEditArm`; the free helper accepts that callable. Preserve
`RoutingCostModel()`, `k`, `insulated`, and `active_net_id` values exactly.

- [ ] **Step 4: Remove the service result type from previews**

Change controller and GUI preview/render signatures from `list[RoutingResult]`
to `Sequence[RoutePath]`. Do not move the service `RoutingResult`; it satisfies
the protocol structurally.

- [ ] **Step 5: Verify pathfinding behaviour and imports**

Run:

```bash
rg -n 'services\.routing\.astar|RoutingResult' src/protoboard/controller
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_wire_edit_arm.py tests/unit/test_edit_service_ports.py tests/unit/test_component_model_invariants.py tests/gui/test_route_tool.py tests/gui/test_place_tool.py tests/gui/test_wire_edit_tool.py -q
/home/yvdev/.local/venv/bin/uv run mypy src/protoboard/controller src/protoboard/gui/tools/route_preview.py src/protoboard/gui/renderers/routing_overlay.py
```

Expected: search has no output and tests/types pass.

- [ ] **Step 6: Commit pathfinding migration**

```bash
git add src/protoboard/controller src/protoboard/gui tests/unit/test_edit_service_ports.py tests/unit/test_wire_edit_arm.py tests/gui/test_route_tool.py tests/gui/test_place_tool.py
git commit -m "refactor(routing): inject pathfinding into controller callers"
```

---

### Task 6: Route wire simplification through `WireSimplifierPort`

**Files:**
- Modify: `src/protoboard/controller/editing/controller.py`
- Modify: `src/protoboard/controller/editing/wire_edit_arm.py`
- Modify: `src/protoboard/controller/selection/wire_edit.py`
- Modify: `src/protoboard/controller/selection/wire_ops.py`
- Modify: `src/protoboard/controller/tools/select_tool.py`
- Modify: `src/protoboard/controller/tools/simplify_tool.py`
- Modify: `tests/unit/test_edit_service_ports.py`
- Modify: `tests/unit/test_wire_edit_arm.py`
- Modify: `tests/gui/test_simplify_tool.py`
- Modify: `tests/gui/test_select_tool.py`

**Interfaces:**
- `build_simplify_inputs(context: EditingReadContext) -> SimplifyInputs`.
- `SimplifyInputs.request(wire, other_wires, mode) -> SimplifyRequest`.
- `EditController.simplify_wire(request: SimplifyRequest) -> Wire`.
- `WireEditArm` receives a `SimplifyWire` callable at construction.

- [ ] **Step 1: Convert simplify tests to a recording simplifier**

Assert the whole-wire tool, selection worker, and fragment-range arm produce
the same command/path while the fake receives the correct `SimplifyRequest`.

- [ ] **Step 2: Run the converted tests and verify direct-service behaviour fails**

Run:

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_wire_edit_arm.py tests/gui/test_simplify_tool.py -q
```

Expected: FAIL because callers do not use the injected simplifier yet.

- [ ] **Step 3: Move the input DTO owner and delegate simplification**

Remove the local `SimplifyInputs` definition from `selection/wire_ops.py` and
import the controller-port DTO. Build requests before worker submission; call
the controller simplifier inside the existing worker. Preserve id handling,
path stitching, parity comments, and command construction in controller code.

- [ ] **Step 4: Verify all simplification entry points**

Run:

```bash
rg -n 'services\.routing\.postprocess' src/protoboard/controller
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_wire_edit_arm.py tests/unit/test_routing_postprocess.py tests/gui/test_simplify_tool.py tests/gui/test_select_tool.py tests/gui/test_wire_edit_tool.py -q
/home/yvdev/.local/venv/bin/uv run mypy src/protoboard/controller
/home/yvdev/.local/venv/bin/uv run pyright
```

Expected: search has no output and checks pass.

- [ ] **Step 5: Commit simplifier migration**

```bash
git add src/protoboard/controller tests/unit/test_edit_service_ports.py tests/unit/test_wire_edit_arm.py tests/gui/test_simplify_tool.py tests/gui/test_select_tool.py
git commit -m "refactor(routing): inject wire simplification"
```

---

### Task 7: Route optimizer discovery/execution and unweaving through ports

**Files:**
- Modify: `src/protoboard/controller/editing/controller.py`
- Modify: `src/protoboard/controller/tools/select_tool.py`
- Modify: `tests/gui/test_optimize_selected_wires.py`
- Modify: `tests/gui/test_select_tool.py`
- Modify: `tests/unit/test_edit_service_ports.py`
- Modify: `tests/unit/test_unweave.py`

**Interfaces:**
- `EditController.optimizer_options() -> Sequence[OptimizerOption]`.
- `EditController.optimize_wires(name, wires) -> Sequence[Wire]` supplies the
  current project/routing snapshot to the optimizer port.
- `EditController.unweave_wires(wires) -> UnweaveResult` supplies schematic,
  graph, and the two routing flags.

- [ ] **Step 1: Change UI tests to port-owned optimizer metadata/results**

Record calls from context-menu option creation, optimizer execution, and
unweave execution. Preserve labels, tooltip notes, plugin failure text, no-op
messages, crossing counts, and generated Remove/Add command macros.

- [ ] **Step 2: Run converted tests and verify they fail on registry access**

Run:

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/gui/test_optimize_selected_wires.py tests/gui/test_select_tool.py -q
```

Expected: FAIL until `SelectTool` calls controller methods instead of service
registries/functions.

- [ ] **Step 3: Replace optimizer metadata and execution**

Build context-menu entries from `optimizer_options()`. The adapter owns built-in
registration, lookup, `RouteRequest`, `AutorouteResult`, and `ProgressSink`.
Keep command diffing in `SelectTool`, because converting wire changes to an
undoable application command is controller responsibility.

- [ ] **Step 4: Replace unweave execution**

Submit one controller-port call through the existing blocking executor. Use
`UnweaveResult.before_crossings/after_crossings` for current early/no-op/status
branches and keep Remove/Add macro construction unchanged.

- [ ] **Step 5: Verify the controller has no service imports**

Run:

```bash
rg -n 'protoboard\.services' src/protoboard/controller
/home/yvdev/.local/venv/bin/uv run pytest tests/gui/test_optimize_selected_wires.py tests/gui/test_select_tool.py tests/gui/test_headless_tool_flows.py tests/unit/test_unweave.py tests/unit/test_edit_service_ports.py -q
/home/yvdev/.local/venv/bin/uv run mypy src/protoboard/controller
/home/yvdev/.local/venv/bin/uv run pyright
```

Expected: search has no output and all checks pass.

- [ ] **Step 6: Commit optimizer/unweave migration**

```bash
git add src/protoboard/controller tests/gui/test_optimize_selected_wires.py tests/gui/test_select_tool.py tests/unit/test_edit_service_ports.py tests/unit/test_unweave.py
git commit -m "refactor(routing): isolate optimizer and unweave services"
```

---

### Task 8: Enforce the complete controller boundary and finalize Workstream 2

**Files:**
- Modify: `tests/unit/test_layer_import_contract.py`
- Modify: `docs/superpowers/specs/2026-08-30-module-boundaries-and-deduplication-design.md`
- Modify: this plan's checkboxes and execution record

**Interfaces:**
- `test_controller_does_not_import_outer_layers()` applies
  `_assert_no_imports("controller", ("protoboard.services", "protoboard.gui"))`
  without an allow-list.

- [ ] **Step 1: Generalize the AST contract**

Replace the separate GUI-only and scoped editing-core guards with one blanket
controller guard. Retain service-to-controller, domain, and instrumentation
tests unchanged.

- [ ] **Step 2: Run boundary and legacy-path audits**

Run:

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_layer_import_contract.py -q
rg -n 'protoboard\.services|protoboard\.gui' src/protoboard/controller
rg -n '\bEditState\b|services\.routing\.(astar|postprocess|unweave|optimizer|snapshot|protocol|autorouter)' src/protoboard/controller tests
git diff --check
```

Expected: tests pass and searches have no output.

- [ ] **Step 3: Run focused Workstream 2 acceptance suites**

Run:

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_editing_contexts.py tests/unit/test_context_import_contract.py tests/unit/test_edit_service_ports.py tests/unit/test_wire_edit_arm.py tests/unit/test_layer_import_contract.py -q
QT_QPA_PLATFORM=offscreen /home/yvdev/.local/venv/bin/uv run pytest tests/gui/test_route_tool.py tests/gui/test_simplify_tool.py tests/gui/test_optimize_selected_wires.py tests/gui/test_select_tool.py tests/gui/test_headless_tool_flows.py -q
```

Expected: PASS.

- [ ] **Step 4: Run the full automated verification gate**

Run:

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit -q
QT_QPA_PLATFORM=offscreen /home/yvdev/.local/venv/bin/uv run pytest tests/gui -q
/home/yvdev/.local/venv/bin/uv run ruff check src tests
/home/yvdev/.local/venv/bin/uv run black --check src tests
/home/yvdev/.local/venv/bin/uv run mypy src
/home/yvdev/.local/venv/bin/uv run pyright
/home/yvdev/.local/venv/bin/uv run pytest -m property -q
```

Expected: all commands pass; record existing xfail/warnings separately. Do not
perform the deferred interactive GUI checklist here.

- [ ] **Step 5: Record metrics and architecture status**

Run:

```bash
rg --files src/protoboard -g '*.py' | wc -l
rg --files src/protoboard -g '*.py' | xargs wc -l | tail -n 1
wc -l src/protoboard/controller/editing/preview_state.py
git diff --stat 6ea2336e..HEAD
git diff --check
```

Update the parent design to mark Workstream 2 complete and Workstream 3
(canonical reusable operations and legacy deletion) next. Report LOC as an
informational metric.

- [ ] **Step 6: Commit the Workstream 2 audit**

```bash
git add tests/unit/test_layer_import_contract.py docs/superpowers/specs/2026-08-30-module-boundaries-and-deduplication-design.md docs/superpowers/plans/2026-08-31-narrow-editing-contexts-and-routing-ports.md
git commit -m "docs(architecture): complete controller service boundary"
```

## Self-Review Checklist

- [ ] `EditingContext` is used only at real composition/orchestration seams.
- [ ] Helper signatures use coherent constituent contexts without casts back to
  the composite.
- [ ] `PreviewState` has no interaction, command, DRC, or notification surface.
- [ ] Controller ports contain no service, GUI, Qt, registry, or progress types.
- [ ] Service adapters own all routing implementation selection.
- [ ] Controller routing callers preserve exact costs, options, status text,
  worker boundaries, commands, and undo semantics.
- [ ] The blanket controller boundary sees local and `TYPE_CHECKING` imports.
- [ ] Every task has its own verified commit and no merge was performed.
