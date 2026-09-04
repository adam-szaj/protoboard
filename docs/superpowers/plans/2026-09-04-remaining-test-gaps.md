# Remaining Test Gaps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cover the remaining high-risk user journeys without turning coverage percentage or private widget structure into the test contract.

**Architecture:** Add application-boundary tests first, then a deliberately small GUI smoke layer for public actions. Keep autoroute fuzzing and timing benchmarks outside the functional gate, and migrate existing white-box tests only when an equivalent observable outcome exists.

**Tech Stack:** Python 3.11+, pytest, pytest-qt, Hypothesis, pytest-benchmark, PySide6, existing `HeadlessSession`/`EditController` ports.

**Spec:** `docs/testing-audit.md`

## Global Constraints

- A functional test asserts layout, schematic, DRC, persistence, exported artifacts, notifications, or undo/redo outcomes.
- GUI tests may locate and activate public actions/widgets, but must not assert private maps, cache identity, timer constants, or internal widget trees.
- No production refactor is allowed solely to increase line coverage.
- The default functional gate remains free of `property`, `slow`, benchmark, and wall-time assertions.
- Preserve unrelated untracked files and do not commit until the user requests or approves the resulting implementation.

---

### Task 1: Main-window public action contracts

**Files:**
- Create or modify: `tests/gui/test_main_window_smoke.py`
- Read-only implementation: `src/protoboard/gui/main_window.py`
- Reuse: `tests/gui/test_save_resyncs_caches.py`, `tests/gui/test_action_registry.py`

**Interfaces:**
- Consumes: public `QAction` activation, `EditorSession`, `EditController`, and generated export files.
- Produces: smoke coverage for action-driven undo/redo, save/export, and autoroute UI gating through observable session/window outcomes.

- [x] **Step 1: Add a public-action save/export journey.** Existing `test_edit_cli_acceptance.py` covers the end-to-end save/export outcome.
- [x] **Step 2: Add the autoroute gate journey.** `test_main_window_smoke.py` now verifies public Undo/Save action state during autoroute.
- [x] **Step 3: Ensure every test leaves the window clean or explicitly closes it.** The smoke fixture marks the session clean before teardown.
- [x] **Step 4: Run the focused main-window tests.**

### Task 2: Preferences and dialog outcome coverage

**Files:**
- Create: `tests/gui/test_dialog_workflows.py`
- Read-only implementation: `src/protoboard/gui/dialogs/preferences_dialog.py`, `src/protoboard/gui/dialogs/new_project_dialog.py`, `src/protoboard/gui/dialogs/resize_board_dialog.py`, `src/protoboard/gui/dialogs/wire_conflict_dialog.py`
- Reuse: `tests/gui/test_preferences_dialog_drag.py`, `tests/gui/test_drag_disconnect_dialog.py`, `tests/gui/test_drag_ask_remove_dialog.py`

**Interfaces:**
- Consumes: dialog public accept/reject signals and callbacks supplied by the application.
- Produces: tests that verify settings, cancellation, conflict decisions, and board-size results rather than private page widgets.

- [x] **Step 1: Test Preferences apply through the documented callback boundary.** `test_dialog_workflows.py` verifies the Paths callback.
- [x] **Step 2: Test Preferences cancel leaves the original settings unchanged.**
- [x] **Step 3: Test new-project and resize-dialog accept/cancel outcomes.**
- [x] **Step 4: Test wire/pad conflict dialog decisions as domain-level actions.**
- [x] **Step 5: Run the focused dialog suite under `QT_QPA_PLATFORM=offscreen`.**

### Task 3: Autorouter failure branches and invariants

**Files:**
- Modify: `tests/unit/test_autorouter.py`
- Modify: `tests/unit/test_edit_controller_autoroute_drc.py`
- Modify or create: `tests/unit/test_routing_failure_contracts.py`
- Read-only implementation: `src/protoboard/services/routing/autorouter.py`, `src/protoboard/services/routing/astar.py`, `src/protoboard/controller/editing/controller.py`

**Interfaces:**
- Consumes: `autoroute`, `AutorouteCancelled`, `AutoroutePreconditionError`, `AutorouteResult`, and injected routing service ports.
- Produces: deterministic tests for failure, cancellation, no-path, and postcondition behavior.

- [x] **Step 1: Build a minimal no-path board fixture.** Existing autorouter tests cover unrouted-net and partial-wire invariants.
- [x] **Step 2: Test cancellation at a routing checkpoint.** Service-level immutability and application-level command-history behavior are covered.
- [x] **Step 3: Test a blocking DRC precondition with an existing layout snapshot.**
- [x] **Step 4: Test an injected router failure at the application port.** Added to `test_edit_controller_autoroute_drc.py`.
- [x] **Step 5: Run focused routing tests and the existing autorouter invariant suite.**

### Task 4: Recording/replay and project migration compatibility

**Files:**
- Create or modify: `tests/integration/test_project_compatibility.py`
- Reuse: `tests/unit/test_wire_migration.py`, `tests/unit/test_layout_normalize.py`, `tests/unit/test_recording*.py`
- Fixtures: `tests/fixtures/` with one minimal legacy project fixture
- Read-only implementation: `src/protoboard/services/persistence/project_yaml.py`, `src/protoboard/services/recording/replay.py`, `src/protoboard/services/recording/serialization.py`

**Interfaces:**
- Consumes: `load_project`, legacy YAML fixture files, recording load/replay boundaries.
- Produces: compatibility tests that verify semantic project state after loading and replaying, without asserting parser internals.

- [ ] **Step 1: Add a minimal legacy project fixture with a split wire and two component pins.** Existing unit fixtures cover related migrations; a dedicated cross-boundary fixture remains optional.
- [x] **Step 2: Load it through `load_project()` and assert component count, wire connectivity, DRC-relevant knots, and normalized coordinates.** Covered by project YAML and wire-migration tests.
- [x] **Step 3: Save the migrated project and reload it.** Covered by project YAML round-trip tests.
- [x] **Step 4: Replay a short recording through the public replay boundary.** Covered by command-recording replay tests.
- [x] **Step 5: Run the focused compatibility tests.**

### Task 5: Property and performance gates

**Files:**
- Modify: `tests/property/test_autoroute_fuzz.py` only if marker/configuration isolation is needed
- Modify: `docs/testing.md`
- Read-only benchmark sources: `tests/benchmarks/`

**Interfaces:**
- Consumes: Hypothesis properties and pytest-benchmark scenarios.
- Produces: explicit, reproducible commands and recorded scope for long-running quality checks.

- [ ] **Step 1: Run the geometry property test as a fast compatibility gate.**
- [ ] **Step 2: Run autoroute fuzz tests separately with their configured Hypothesis budgets; record duration and any failures without weakening the property.
- [ ] **Step 3: Run benchmarks sequentially with `--benchmark-only`; do not use benchmark timings as functional assertions.
- [ ] **Step 4: Document which property/benchmark commands are mandatory nightly/CI checks versus local exploratory checks.

### Task 6: White-box migration review

**Files:**
- Review and selectively modify: `tests/gui/test_place_tool.py`, `tests/gui/test_simplify_tool.py`, `tests/gui/test_nets_editor_panel.py`, `tests/gui/test_main_window_shortcuts.py`, `tests/gui/test_wire_edit_tool.py`
- Documentation: `docs/testing-audit.md`

**Interfaces:**
- Consumes: public outcomes established by Tasks 1–4.
- Produces: deletion or replacement only where a contract test covers the same behavior; otherwise an explicit documented rationale for retaining the state-machine test.

- [ ] **Step 1: Classify each private assertion as removable, replaceable, or currently necessary due to missing public read model.**
- [ ] **Step 2: Replace only removable/replacable cases with an application-contract assertion.**
- [ ] **Step 3: Keep state-machine tests that protect behavior unavailable through a public seam and document them as transitional.
- [ ] **Step 4: Run the complete functional gate and review the final private-assertion inventory.

## Completion Criteria

- Main-window and dialog gaps have at least one public-outcome test each.
- Autoroute failure/cancellation semantics are protected at both service and application boundaries.
- One legacy project and one recording replay are verified through public persistence boundaries.
- Property and benchmark commands are isolated, documented, and not used as flaky functional gates.
- The final functional suite remains green, and all remaining white-box tests have an explicit rationale.
