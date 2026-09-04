# Test Strategy Gaps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cover the highest-value functional gaps while organising tests around four explicit layers: domain invariants, application contracts, GUI smoke journeys, and compatibility/performance.

**Architecture:** Keep pure domain tests independent of Qt, exercise cross-module behavior through `HeadlessSession` and `EditController`, and reserve `pytest-qt` for a small number of public GUI journeys. Keep legacy fixtures, Hypothesis tests, and benchmarks in separately runnable marker groups.

**Tech Stack:** Python 3.11+, pytest, pytest-qt, Hypothesis, pytest-benchmark, PySide6, ruamel.yaml.

**Spec:** `docs/testing-audit.md`

## Global Constraints

- Assert observable results at module boundaries; do not assert private fields unless they are deliberately public contracts.
- Do not change production behavior solely to make a test easier to write.
- Preserve unrelated untracked files in the worktree.
- Keep the default suite free of `property`, `slow`, and benchmark timing tests.
- Run focused tests after each task and the complete functional suite before completion.

---

### Task 1: Legacy wire migration contract tests

**Files:**
- Create: `tests/unit/test_wire_migration.py`
- Read-only implementation: `src/protoboard/services/persistence/wire_migration.py`
- Test helpers: `tests/conftest.py`, existing layout/wire fixtures

**Interfaces:**
- Consumes: `migrate_layout_to_v2`, `MigrationReport`, `build_pin_index`.
- Produces: tests proving migration preserves user-visible connectivity, reports diagnostics, and is idempotent.

- [x] **Step 1: Write tests for endpoint classification and merging.** Build a small board/layout with two legacy segments meeting on a plated pad, plus a pin endpoint, and assert the resulting report and knot/wire connectivity rather than helper call counts.
- [x] **Step 2: Add an off-pad diagnostic test.** Assert `MigrationReport.off_pad_endpoints` contains the invalid point while migration still returns a usable layout.
- [x] **Step 3: Add idempotence coverage.** Run migration twice and assert the second report is a no-op and the serialized semantic wire/knot state is unchanged.
- [x] **Step 4: Run `uv run pytest tests/unit/test_wire_migration.py -q`.**

### Task 2: Hierarchical routing invariants

**Files:**
- Create: `tests/unit/test_hierarchical.py`
- Read-only implementation: `src/protoboard/services/routing/hierarchical.py`
- Reuse: `tests/unit/_autoroute_invariants.py`, `tests/unit/test_routing_service_ports.py`

**Interfaces:**
- Consumes: `hierarchical_autoroute`, `AutorouteResult`, `ProgressSink`-compatible callbacks, `threading.Event` cancellation.
- Produces: tests for post-order grouping, top-level fallback, cancellation, and result aggregation.

- [x] **Step 1: Create a deterministic recording router seam using the existing router protocol.** Record each `net_ids` subset and return a semantic `AutorouteResult` without inspecting private implementation state.
- [x] **Step 2: Assert child-group nets are routed before parent/top-level nets.** Verify the observed stage labels and that each target net is routed once.
- [x] **Step 3: Assert cross-group nets are deferred to the top-level pass.** Verify the resulting `unrouted_nets` union and wire result aggregation.
- [x] **Step 4: Assert a pre-set cancellation flag returns no partial result.** Also verify the caller’s layout remains unchanged.
- [x] **Step 5: Run `uv run pytest tests/unit/test_hierarchical.py tests/unit/test_routing_service_ports.py -q`.**

### Task 3: Autoroute failure and cancellation application contracts

**Files:**
- Modify: `tests/unit/test_edit_controller_autoroute_drc.py`
- Modify: `tests/unit/test_autorouter.py`
- Read-only implementation: `src/protoboard/controller/editing/controller.py`, `src/protoboard/services/routing/autorouter.py`

**Interfaces:**
- Consumes: `EditController.autoroute`, `AutoroutePreconditionError`, `AutorouteResult`.
- Produces: regression tests asserting failed/cancelled routing leaves layout, knots, schematic nets, and undo history unchanged.

- [x] **Step 1: Capture a semantic session snapshot before a forced routing failure.** Compare public layout/schematic/command-stack outcomes after the exception.
- [x] **Step 2: Add a cancellation test with a deterministic cancelled route result.** Assert no wires are committed and no undo entry is created.
- [x] **Step 3: Run the focused controller and autorouter tests.**

### Task 4: End-to-end edit, DRC, persistence, and export journey

**Files:**
- Modify: `tests/gui/test_edit_cli_acceptance.py`
- Reuse: `src/protoboard/cli.py`, `src/protoboard/services/persistence/project_yaml.py`, `src/protoboard/services/export/`

**Interfaces:**
- Consumes: public `CliDriver` commands and project save/load/export boundaries.
- Produces: one headless application-contract journey that verifies an edited project remains electrically meaningful after persistence and export.

- [x] **Step 1: Extend the existing round-trip journey with DRC output assertions.** Assert that the public CLI returns a well-formed DRC summary before persistence.
- [x] **Step 2: Export a supported semantic artifact to `tmp_path`.** Assert file existence and content-level markers, not private exporter calls.
- [x] **Step 3: Run `uv run pytest tests/gui/test_edit_cli_acceptance.py -q`.**

### Task 5: GUI smoke contracts for preferences and main-window actions

**Files:**
- Modify: `tests/gui/test_preferences_dialog_drag.py`
- Modify: `tests/gui/test_main_window_shortcuts.py` or create `tests/gui/test_main_window_smoke.py` if the existing file is entirely white-box
- Read-only implementation: `src/protoboard/gui/main_window.py`, `src/protoboard/gui/dialogs/preferences_dialog.py`

**Interfaces:**
- Consumes: public widget actions, session state, persisted settings, and visible status/application outcomes.
- Produces: a small smoke suite that does not depend on private QAction maps or internal tree widgets.

- [x] **Step 1: Identify the smallest public action path for opening Preferences and applying a setting.** Existing `test_preferences_dialog_drag.py` covers the settings behavior; no duplicate smoke test was added because the dialog currently exposes no public page-apply API.
- [x] **Step 2: Add a main-window smoke test for undo/redo or export through a public action.** Assert the resulting project artifact/session state.
- [x] **Step 3: Remove only tests whose sole assertion is private widget/controller structure when a replacement covers the same user outcome.**
- [x] **Step 4: Run the focused GUI smoke files under `QT_QPA_PLATFORM=offscreen`.**

### Task 6: Four-layer documentation and test commands

**Files:**
- Modify: `docs/testing.md`
- Modify: `docs/testing-audit.md`
- Optional focused test organization: `tests/property/` and `tests/benchmarks/` only if marker behavior is currently inconsistent

**Interfaces:**
- Consumes: the completed test files from Tasks 1–5.
- Produces: a capability matrix linking each important journey to an application-contract test and domain invariant, plus exact commands for each layer.

- [x] **Step 1: Add a compact matrix for connectivity, persistence, migration, routing, GUI actions, and export.**
- [x] **Step 2: Document default, property, serial, GUI, and benchmark commands with their intended scope.**
- [x] **Step 3: Run documentation-linked commands and check `git diff --check`.**

### Task 7: Full verification

**Files:**
- No source changes expected.

- [x] **Step 1: Run focused unit and GUI tests for all changed files.**
- [x] **Step 2: Run `QT_QPA_PLATFORM=offscreen uv run pytest tests/unit tests/gui --ignore=tests/benchmarks -p no:benchmark -m "not serial and not slow and not property" -q`.**
- [x] **Step 3: Run Ruff and Black checks on changed Python files.**
- [x] **Step 4: Run selected property tests separately, recording any intentionally long-running cases without mixing them into the functional gate.** `tests/property/test_geometry_predicates.py` passes; the large autoroute fuzz cases remain intentionally separate and are not part of the functional gate.
- [x] **Step 5: Review the final diff for private implementation assertions and unrelated file changes.**
