<!-- SPDX-License-Identifier: MIT -->
# Application Ports and Composition Roots Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove concrete persistence, export, DRC, routing, library-loading, and
drag-detection dependencies from the controller editing core by introducing
narrow application ports and moving headless/CLI wiring to explicit composition
roots, without changing observable behaviour.

**Architecture:** Controller code owns small protocols and stable result/view
DTOs. Service adapters implement those protocols. Root-level headless and CLI
composition modules assemble the controller with concrete services. Pure drag
classification moves into domain layout and emits typed conflict reasons; GUI
code alone formats those reasons for display. Stable theme and layer options
move under `controller.view_options`, while YAML I/O stays in services and
rendering/z-order remains in GUI.

**Tech Stack:** Python 3.11+, pytest, PySide6, standard-library `ast`, Ruff,
Black, strict mypy, Pyright, uv

**Spec:** `docs/superpowers/specs/2026-08-30-module-boundaries-and-deduplication-design.md`

## Global Constraints

- Preserve runtime behaviour, command/undo semantics, persistence schema,
  export output, and existing GUI text unless a task explicitly replaces a
  presentation string with an equivalent formatter.
- Do not add compatibility modules, old-path re-exports, automatic delegation,
  or broad import-contract allow-lists. Repoint all callers and delete the old
  path in the same task.
- Keep this workstream limited to application ports and composition seams. The
  broad `EditState` split and remaining tool-level routing imports belong to the
  follow-up Workstream 2B.
- Treat the one-third LOC reduction as aspirational. Prefer explicit typed
  contracts over metaprogramming or compressed code.
- Preserve comments explaining geometry, routing, concurrency, persistence,
  or undo invariants. Remove only comments made obsolete by these moves.
- Add a focused failing test before every behaviour-affecting change. Pure file
  moves may begin with characterization tests that already pass.
- Use `/home/yvdev/.local/venv/bin/uv` for all project commands.
- Create commits only when explicitly authorized. When authorized, use the
  commit boundary at the end of each task.

## Baseline and Exit Criteria

At plan creation, `src/protoboard` contains 302 Python modules and 96,652
physical lines. Eleven controller files import `protoboard.services`; one
service file imports controller tools; persistence and export services import
GUI layer state.

This plan is complete when:

- `controller/editing/controller.py`, `controller/editing/preview_state.py`,
  `controller/editing/drag_commit.py`, and all `controller/ports/` modules have
  no `protoboard.services` or `protoboard.gui` imports, including local and
  `TYPE_CHECKING` imports;
- services import only the explicitly allowed controller port and
  `view_options` modules, never controller editing/use-case/tool modules;
- `HeadlessSession` and `CliDriver` are composition roots outside
  `protoboard.controller`, and the old controller paths no longer exist;
- drag analysis is domain-owned, contains typed reasons rather than presentation
  text or `tuple[object, ...]`, and all visible conflict text is formatted in
  GUI code;
- persistence no longer imports GUI layer DTOs, and theme YAML I/O no longer
  lives in controller;
- focused tests, the full unit suite, offscreen GUI suite, Ruff, Black, strict
  mypy, Pyright, and GUI smoke tests pass.

## Explicitly Deferred to Workstream 2B

- Removing routing implementation imports from `controller/tools/`,
  `controller/selection/`, and `controller/editing/placement_conflict.py`.
- Splitting `EditState` into project, spatial, connectivity, editing-effect,
  and interaction protocols.
- Shrinking `PreviewState` to the final spatial-project adapter after consumers
  accept narrow contexts.
- Enabling a blanket `controller -> services` prohibition. This plan adds a
  scoped guard for the migrated controller core; Workstream 2B makes it generic.

---

### Task 1: Lock the application boundary with scoped AST contracts

**Files:**
- Modify: `tests/unit/test_layer_import_contract.py`

**Interfaces:**
- Extend the existing AST walker with an `_assert_paths_no_imports(...)` helper.
- The tests must inspect module-scope, local, and `TYPE_CHECKING` imports.

- [x] **Step 1: Record the current offenders**

Run:

```bash
rg -n 'protoboard\.services' src/protoboard/controller
rg -n 'protoboard\.controller\.(tools|editing)' src/protoboard/services
rg -n 'protoboard\.gui' src/protoboard/services
```

Expected: the known controller routing/tool imports, controller editing service
imports, `services/drag_detector.py -> controller.tools`, and export/persistence
GUI imports are visible.

- [x] **Step 2: Add failing scoped boundary tests**

Add tests with these responsibilities:

```python
def test_controller_editing_core_does_not_import_outer_layers() -> None:
    _assert_paths_no_imports(
        (
            SRC / "controller/editing/controller.py",
            SRC / "controller/editing/drag_commit.py",
            SRC / "controller/editing/preview_state.py",
            SRC / "controller/ports",
        ),
        ("protoboard.services", "protoboard.gui"),
    )


def test_services_do_not_import_controller_implementation() -> None:
    _assert_no_imports(
        "services",
        (
            "protoboard.controller.editing",
            "protoboard.controller.selection",
            "protoboard.controller.tools",
        ),
    )
```

`_assert_paths_no_imports` accepts files or directories and reports relative
file, line, and imported module. Do not create an allow-list here.

- [x] **Step 3: Prove the tests fail for the intended seams**

Run:

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_layer_import_contract.py::test_controller_editing_core_does_not_import_outer_layers tests/unit/test_layer_import_contract.py::test_services_do_not_import_controller_implementation -q
```

Expected: FAIL for controller editing imports and `services/drag_detector.py`.

- [x] **Step 4: Keep the failing tests uncommitted until Tasks 2-6 make them green**

Do not weaken the assertions or add transitional exclusions.

---

### Task 2: Move stable view options inward and theme I/O outward

**Files:**
- Create: `src/protoboard/controller/view_options/__init__.py`
- Move data only: `src/protoboard/controller/theme.py` ->
  `src/protoboard/controller/view_options/theme.py`
- Create: `src/protoboard/controller/view_options/layers.py`
- Create: `src/protoboard/services/preferences/__init__.py`
- Create: `src/protoboard/services/preferences/theme_yaml.py`
- Modify: `src/protoboard/gui/layer_state.py`
- Modify: `src/protoboard/services/persistence/project_yaml.py`
- Modify: every caller returned by
  `rg -l 'protoboard\.controller\.theme|protoboard\.gui\.layer_state' src tests -g '*.py'`
- Delete: `src/protoboard/controller/theme.py`
- Modify focused tests: `tests/unit/test_theme_body_font.py`,
  `tests/unit/test_layer_state_pin_reveal.py`,
  `tests/gui/test_board_view.py`, `tests/gui/test_hover_reveal.py`

**Interfaces:**
- `controller.view_options.theme`: `Theme`, `DEFAULT_THEME`.
- `controller.view_options.layers`: `ViewTransform`, `LayerVisibility`,
  `LayerOverrides`.
- `services.preferences.theme_yaml`: `load_theme`, `save_theme`.
- `gui.layer_state`: `LayerState`, `layer_state_for_tool`, z-order and rendering
  alpha constants only.

- [x] **Step 1: Add characterization coverage for DTO and YAML behaviour**

Extend the focused tests to prove:

- `LayerVisibility.cycle()` retains `FULL -> GHOST -> HIDDEN -> FULL`;
- `LayerOverrides.reset()` and `all_full()` retain their behaviour;
- a customized `Theme` survives `save_theme` then `load_theme`;
- invalid theme YAML still falls back to `DEFAULT_THEME`.

Run:

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_theme_body_font.py tests/unit/test_layer_state_pin_reveal.py -q
```

Expected: PASS before the move.

- [x] **Step 2: Separate stable DTOs from rendering state**

Move `ViewTransform`, `LayerVisibility`, and `LayerOverrides` unchanged into
`controller/view_options/layers.py`. Keep `LayerState`, z-order constants,
alpha policy, and `layer_state_for_tool` in GUI. Update `gui/layer_state.py` to
import the three DTOs from the new module.

Do not re-export the DTOs from `gui.layer_state`; split mixed imports at every
caller so old imports disappear.

- [x] **Step 3: Separate theme data from theme persistence**

Move `Theme` and `DEFAULT_THEME` into `controller/view_options/theme.py`. Move
`load_theme` and `save_theme`, including YAML/path handling, into
`services/preferences/theme_yaml.py`. The service imports the DTO; the DTO
module must not import YAML, filesystem configuration, GUI, or services.

Update GUI and export renderers to import theme data from
`controller.view_options.theme`. Update preferences/main-window callers to
import theme I/O from `services.preferences.theme_yaml`.

- [x] **Step 4: Repoint persistence to stable layer DTOs**

Change `services/persistence/project_yaml.py` to import `LayerOverrides` and
`LayerVisibility` from `controller.view_options.layers`. Keep the serialized
field names and values unchanged; no YAML compatibility code is added.

Export modules may still import GUI `LayerState` for rendering in this plan,
but any import of `LayerOverrides`/`LayerVisibility` must use the stable DTO
module. Shared rendering extraction remains Workstream 4.

- [x] **Step 5: Prove old paths are gone and run focused tests**

Run:

```bash
test ! -e src/protoboard/controller/theme.py
rg -n 'protoboard\.controller\.theme' src tests
rg -n 'from protoboard\.gui\.layer_state import.*(LayerOverrides|LayerVisibility)' src tests
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_theme_body_font.py tests/unit/test_layer_state_pin_reveal.py tests/gui/test_board_view.py tests/gui/test_hover_reveal.py -q
/home/yvdev/.local/venv/bin/uv run ruff check src/protoboard/controller/view_options src/protoboard/services/preferences src/protoboard/gui/layer_state.py src/protoboard/services/persistence/project_yaml.py
```

Expected: searches have no output and all checks pass.

- [ ] **Step 6: Commit the view-options boundary when authorized**

```bash
git add src/protoboard/controller/view_options src/protoboard/services/preferences src/protoboard/gui/layer_state.py src/protoboard/services/persistence/project_yaml.py src tests
git commit -m "refactor(architecture): separate view options from adapters"
```

---

### Task 3: Make drag analysis a pure domain result with typed reasons

**Files:**
- Create: `src/protoboard/domain/layout/drag_analysis.py`
- Move implementation: `src/protoboard/services/drag_detector.py` ->
  `src/protoboard/domain/layout/drag_detector.py`
- Create: `src/protoboard/gui/tools/drag_messages.py`
- Modify: `src/protoboard/controller/editing/drag_commit.py`
- Modify: `src/protoboard/controller/selection/drag_controller.py`
- Modify: `src/protoboard/controller/selection/drop_resolution.py`
- Modify: `src/protoboard/controller/editing/armed_transform.py`
- Modify: `src/protoboard/controller/tools/armed_transform_tool.py`
- Modify: `src/protoboard/controller/tools/route_tool.py`
- Modify: `src/protoboard/controller/tools/select_tool.py`
- Modify: `src/protoboard/controller/tools/transform_overlays.py`
- Modify: `src/protoboard/gui/tools/transform_overlays.py`
- Delete: `src/protoboard/services/drag_detector.py`
- Delete: `src/protoboard/controller/tools/drag_commit_decision.py`
- Modify: `tests/unit/test_drag_detector.py`,
  `tests/unit/test_drag_detector_classification.py`,
  `tests/unit/test_classify_drag_commit.py`,
  `tests/unit/test_group_lock_internal_wires.py`
- Modify GUI tests returned by
  `rg -l 'DragCommitDecision|DragSeverity' tests/gui -g '*.py'`

**Interfaces:**
- `DragSeverity`: unchanged `CLEAR`, `SOFT`, `HARD` values.
- `DragReasonCode`: `BODY_OVERLAP`, `DIFFERENT_NETS`, `SHARED_PAD`,
  `SAME_NET_WIRE`, `FOREIGN_WIRE`, `ALIEN_PAD_CROSSING`, `SKEWED_WIRE`,
  `LOCKED_GROUP_WIRE`, `ROUTE_ALIEN_PAD_CROSSING`.
- `DragReason`: typed code plus only structured context needed for formatting
  (`refdes`, `pin_number`, `related_refdes`, `count`).
- `DragAnalysis`: existing structural commit fields, `reasons`, and severity
  predicates; no `description` and no `auto_net_actions`.
- `format_drag_reasons(reasons: Sequence[DragReason]) -> str` lives in GUI.

- [x] **Step 1: Change tests from presentation assertions to reason codes**

In unit tests, replace assertions such as `"different-net" in
decision.description` with exact `DragReasonCode` membership. Add a test that
the clear result has an empty reason tuple. Add a GUI formatter test covering
each reason code and the multi-reason `"; "` join order.

Run one converted test before implementation:

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_drag_detector.py -q
```

Expected: FAIL because the domain result does not exist yet.

- [x] **Step 2: Introduce the domain result types**

Create dependency-free frozen dataclasses/enums in
`domain/layout/drag_analysis.py`. Preserve the current wire-classification and
drop bucket fields with precise types. Remove the unused `auto_net_actions`
field: the current detector always returns an empty tuple and no production
consumer reads it.

Do not add `__getattr__`, aliases, a `description` compatibility property, or
an old-path re-export.

- [x] **Step 3: Move the pure detector into domain layout**

Move the detector and snapshot types, update imports, and replace every
description append with a structured `DragReason`. Keep detection order,
severity precedence, pad positions, wire classifications, and drop buckets
unchanged. Retain comments that explain group-owned-wire and topology
invariants; remove migration-history comments made obsolete by deleting the
fallback.

- [x] **Step 4: Map domain reasons to GUI text**

Implement `gui/tools/drag_messages.py` as an explicit `match`/mapping over all
`DragReasonCode` values. `gui/tools/transform_overlays.py` calls the formatter
and retains the current generic fallback for an empty non-clear reason set.

Controller commit/drop code consumes `DragAnalysis` and structured action
fields only. `route_tool.py` may keep its existing route-status text, but must
construct the same domain analysis type for its overlay rather than a parallel
controller decision type.

- [x] **Step 5: Remove the old decision/detector paths and verify callers**

Run:

```bash
rg -n 'services\.drag_detector|controller\.tools\.drag_commit_decision|DragCommitDecision|auto_net_actions|decision\.description' src tests
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_drag_detector.py tests/unit/test_drag_detector_classification.py tests/unit/test_classify_drag_commit.py tests/unit/test_group_lock_internal_wires.py tests/gui/test_drag_controller.py tests/gui/test_headless_tool_flows.py tests/gui/test_select_tool.py tests/gui/test_armed_transform_tool.py tests/gui/test_armed_subtool_lifecycle.py -q
```

Expected: search has no output and all tests pass.

- [x] **Step 6: Run the service-to-controller boundary test**

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_layer_import_contract.py::test_services_do_not_import_controller_implementation -q
```

Expected: PASS.

- [ ] **Step 7: Commit the drag boundary when authorized**

```bash
git add src/protoboard/domain/layout src/protoboard/controller src/protoboard/gui/tools src/protoboard/services/drag_detector.py tests
git commit -m "refactor(drag): return structured domain analysis"
```

---

### Task 4: Introduce injected ports for EditController services

**Files:**
- Create: `src/protoboard/controller/ports/drc.py`
- Create: `src/protoboard/controller/ports/edit_services.py`
- Create: `src/protoboard/services/editing/__init__.py`
- Create: `src/protoboard/services/editing/adapters.py`
- Modify: `src/protoboard/services/drc/engine.py`
- Modify: `src/protoboard/controller/editing/controller.py`
- Modify: `src/protoboard/controller/editing/preview_state.py`
- Modify: `src/protoboard/gui/workers/drc_worker.py`
- Modify: `src/protoboard/gui/session.py`
- Modify: DRC/result callers returned by `rg -l 'DRCIssue' src tests -g '*.py'`
- Modify: `tests/unit/test_edit_controller_autoroute_drc.py`
- Modify: `tests/unit/test_edit_controller_scaffold.py`
- Add: `tests/unit/test_edit_service_ports.py`

**Interfaces:**
- `controller.ports.drc.DRCIssue`: current frozen issue DTO and validation.
- `RouteResult` protocol: read-only `wires: Sequence[Wire]`.
- `AutoroutePort.route(...) -> RouteResult`.
- `DrcPort.check(...) -> list[DRCIssue]`.
- `ProjectStorePort.save(...) -> None`.
- `BoardExportPort.export(kind, path, ...) -> None`.
- `EditServicePorts`: frozen bundle of the four ports.
- `EditController(state, ports)`: ports are mandatory; no service defaults in
  controller code.

- [x] **Step 1: Add fake-port controller tests**

In `tests/unit/test_edit_service_ports.py`, build recording fakes for all four
ports and prove:

- `autoroute()` forwards immutable current board/layout/schematic/component
  state, routing settings, net IDs, and report path without mutating layout;
- `run_drc()` returns the exact issue list from the DRC port;
- `save()` forwards the directory and current project values;
- `export()` forwards kind/path/current rendering inputs;
- `apply_autoroute()` remains controller-owned and executes one undoable macro.

Run:

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_edit_service_ports.py -q
```

Expected: FAIL because ports and constructor injection do not exist.

- [x] **Step 2: Move the DRC result DTO to the port layer**

Move `DRCIssue` and severity validation into `controller/ports/drc.py`.
`services/drc/engine.py` imports and returns it. Repoint GUI worker/panel/session
and tests. Leave DRC algorithms and message construction in the service.

Delete the old DTO definition; do not re-export it from the engine.

- [x] **Step 3: Define narrow service protocols**

Use explicit keyword-only domain arguments rather than passing `EditState` into
adapters. Do not expose concrete `AutorouteResult`, `RouteRequest`, Qt types,
or service registries in controller annotations. Use `Sequence[Wire]` for the
minimal route result contract actually consumed by `apply_autoroute`.

The export port accepts the already-required board/layout/components/instances/
schematic/package-family values. Preserve current `ValueError` behaviour for an
unknown export kind in the concrete adapter.

- [x] **Step 4: Implement service adapters**

In `services/editing/adapters.py`:

- build `RouteRequest`, register built-ins, and invoke the routing registry;
- run `DRCEngine` with the supplied graph/pad index/axis settings;
- delegate persistence to `save_project`;
- select PDF/PNG/SVG adapters and preserve SVG file encoding/contents;
- expose `default_edit_service_ports() -> EditServicePorts`.

Only this adapter module knows the concrete service implementations.

- [x] **Step 5: Inject ports into EditController**

Store `EditServicePorts` in the constructor and replace local service imports
in `autoroute`, `run_drc`, `save`, and `export` with port calls. Remove
`run_drc()` from `EditState`; the controller supplies all DRC state explicitly
to its DRC port.

Update the two direct `EditController(...)` test constructions to pass fakes.
The composition root will supply defaults in Task 5.

- [x] **Step 6: Run focused port and behaviour tests**

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_edit_service_ports.py tests/unit/test_edit_controller_autoroute_drc.py tests/unit/test_edit_controller_scaffold.py tests/unit/test_drc.py -q
/home/yvdev/.local/venv/bin/uv run mypy src/protoboard/controller/ports src/protoboard/controller/editing/controller.py src/protoboard/services/editing src/protoboard/services/drc/engine.py
```

Expected: PASS with no concrete service types leaking into controller ports.

- [ ] **Step 7: Commit the application ports when authorized**

```bash
git add src/protoboard/controller/ports src/protoboard/controller/editing/controller.py src/protoboard/controller/editing/preview_state.py src/protoboard/services/editing src/protoboard/services/drc src/protoboard/gui tests/unit
git commit -m "refactor(controller): inject editing service ports"
```

---

### Task 5: Move headless and CLI composition out of controller

**Files:**
- Move: `src/protoboard/controller/editing/headless_session.py` ->
  `src/protoboard/headless_session.py`
- Move: `src/protoboard/controller/editing/cli.py` -> `src/protoboard/cli.py`
- Create: `src/protoboard/controller/editing/spatial_index.py`
- Modify: `src/protoboard/controller/editing/preview_state.py`
- Modify: `src/protoboard/controller/editing/__init__.py`
- Modify: `src/protoboard/gui/session.py`
- Modify: every caller returned by
  `rg -l 'controller\.editing(\.headless_session)? import HeadlessSession|controller\.editing\.cli' src tests -g '*.py'`
- Modify: `tests/unit/test_view_proto_parity.py`
- Modify: `tests/gui/test_edit_cli_acceptance.py`

**Interfaces:**
- `protoboard.headless_session.HeadlessSession`: root composition object with
  the existing public constructor plus optional explicit `edit_ports` for tests.
- `protoboard.cli.CliDriver`, `protoboard.cli.CliError`.
- `controller.editing.spatial_index.build_component_spatial_index(...)`: pure
  helper shared by live and preview state.

- [x] **Step 1: Add root-path acceptance assertions**

Update the scaffold and CLI acceptance tests to import the new root paths.
Add assertions that constructing `HeadlessSession` without explicit ports wires
the default adapters and that constructing it with fake ports uses the fakes.

Run one updated test before moving files:

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_edit_controller_scaffold.py -q
```

Expected: FAIL on the new import path.

- [x] **Step 2: Extract the pure spatial-index helper**

Move `_build_comp_spatial_index` into
`controller/editing/spatial_index.py`, rename it
`build_component_spatial_index`, and update both live and preview callers.
This prevents controller preview code from importing a root composition module.

- [x] **Step 3: Move the composition modules and inject defaults**

Move the files with `git mv`. `HeadlessSession` may import controller and
services because it is now a composition root. Its lazy `edit_controller`
construction must pass either the explicitly supplied `EditServicePorts` or
`default_edit_service_ports()`.

Keep library loading/package-family/catalog and cost-analysis wiring in the
root object for now; do not push concrete services back into controller.

- [x] **Step 4: Repoint every caller and remove old exports**

Import `HeadlessSession` from `protoboard.headless_session` and `CliDriver`
from `protoboard.cli`. Remove `HeadlessSession` from
`controller/editing/__init__.py` and update its documentation/`__all__`.

Do not leave stub modules or re-exports at either old path.

- [x] **Step 5: Verify the moves and acceptance behaviour**

Run:

```bash
test ! -e src/protoboard/controller/editing/headless_session.py
test ! -e src/protoboard/controller/editing/cli.py
rg -n 'controller\.editing(\.headless_session)? import HeadlessSession|controller\.editing\.cli' src tests
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_edit_controller_scaffold.py tests/unit/test_view_proto_parity.py tests/gui/test_edit_cli_acceptance.py tests/unit/test_edit_controller_autoroute_drc.py -q
```

Expected: searches have no output and all tests pass.

- [x] **Step 6: Make the scoped application boundary green**

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_layer_import_contract.py -q
```

Expected: PASS. Remaining controller-to-service imports are limited to the
explicitly deferred routing tool/selection files listed by the baseline.

- [ ] **Step 7: Commit composition-root relocation when authorized**

```bash
git add src/protoboard/headless_session.py src/protoboard/cli.py src/protoboard/controller/editing src/protoboard/gui/session.py src tests
git commit -m "refactor(architecture): move editing composition roots"
```

---

### Task 6: Final audit, documentation, and GUI smoke verification

**Files:**
- Modify: `docs/superpowers/specs/2026-08-30-module-boundaries-and-deduplication-design.md`
- Modify: this plan's checkboxes as tasks complete

- [x] **Step 1: Audit dependency edges mechanically**

Run:

```bash
rg -n 'protoboard\.services' src/protoboard/controller
rg -n 'protoboard\.controller\.(tools|editing|selection)' src/protoboard/services
rg -n 'protoboard\.gui' src/protoboard/services
rg -n 'controller\.theme|services\.drag_detector|drag_commit_decision|controller\.editing\.headless_session|controller\.editing\.cli' src tests
```

Expected:

- controller service imports remain only in deferred routing tool/selection
  files;
- services have no controller implementation imports;
- services-to-GUI imports remain only in the explicitly deferred export paint/
  snapshot rendering seam and plugin-discovery documentation;
- all legacy-path searches have no output.

- [x] **Step 2: Run focused unit and GUI suites**

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_layer_import_contract.py tests/unit/test_edit_service_ports.py tests/unit/test_edit_controller_autoroute_drc.py tests/unit/test_drag_detector.py tests/unit/test_classify_drag_commit.py tests/unit/test_drc.py -q
QT_QPA_PLATFORM=offscreen /home/yvdev/.local/venv/bin/uv run pytest tests/gui/test_edit_cli_acceptance.py tests/gui/test_drag_controller.py tests/gui/test_select_tool.py tests/gui/test_board_view.py -q
```

Expected: PASS.

- [x] **Step 3: Run the complete verification gate**

```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit -q
QT_QPA_PLATFORM=offscreen /home/yvdev/.local/venv/bin/uv run pytest tests/gui -q
/home/yvdev/.local/venv/bin/uv run ruff check src tests
/home/yvdev/.local/venv/bin/uv run black --check src tests
/home/yvdev/.local/venv/bin/uv run mypy src
/home/yvdev/.local/venv/bin/uv run pyright
/home/yvdev/.local/venv/bin/uv run pytest -m property -q
```

Expected: all commands pass; the existing GUI xfail/warnings may remain only if
they match the pre-workstream baseline exactly.

- [ ] **Step 4: Run manual GUI smoke tests**

With `uv run protoboard`, verify:

1. create a new project and place a component;
2. change its package option and confirm pads/pins render;
3. drag it through CLEAR, SOFT, and HARD conflict states and confirm overlay
   severity/text and commit blocking;
4. create/edit a wire, then undo and redo;
5. run DRC and select an issue;
6. save, reopen, and verify layer overrides/theme;
7. export PDF, PNG, and SVG and open each output.

Expected: no traceback and behaviour matches the pre-workstream application.

- [x] **Step 5: Record final metrics and update architecture status**

```bash
rg --files src/protoboard -g '*.py' | wc -l
rg --files src/protoboard -g '*.py' | xargs wc -l | tail -n 1
git diff --stat
git diff --check
```

Update the parent design to mark application ports/composition complete and
name Workstream 2B as the next step: narrow editing contexts plus removal of
remaining controller routing-service imports. Report LOC as information, not a
pass/fail criterion.

- [ ] **Step 6: Commit final docs when authorized**

```bash
git add docs tests/unit/test_layer_import_contract.py
git commit -m "docs(architecture): record application port boundary"
```

## Execution Record

- Full unit suite: 2818 passed, 68 skipped.
- Offscreen GUI suite: 1494 passed, 1 expected xfail, 1 pre-existing PySide
  disconnect warning.
- Property suite: 2 tests passed (3000 Hypothesis examples), 4394 deselected.
- Ruff, Black, strict mypy (312 source files), and Pyright (0 diagnostics) pass.
- Offscreen application startup reached the Qt event loop without a traceback;
  automated GUI tests covered the edit journey and PDF/PNG/SVG exports. The
  interactive manual checklist remains intentionally unchecked.
- `src/protoboard`: 302 to 312 Python modules and 96,652 to 96,906 physical
  lines (+254). Layer deltas are controller -985, domain +738, GUI -5, and
  services -479 lines; the root-level composition modules account for the
  corresponding move out of controller. LOC is informational, not a gate.
- No task commits were created because commit authorization was not given for
  this execution batch.

## Self-Review Checklist

- [x] Every concrete service used by `EditController` has one focused port and
  one default adapter.
- [x] No port annotation imports a service or GUI type.
- [x] `DRCIssue`, theme, and layer DTOs have exactly one authoritative owner.
- [x] No old-path compatibility modules or re-exports remain.
- [x] Drag reasons are typed domain facts and GUI is the only formatter.
- [x] Composition roots, not controller modules, choose concrete adapters.
- [x] Deferred routing imports are enumerated, not hidden by an allow-list.
- [x] Tests prove behaviour and dependency direction before completion is
  claimed.
