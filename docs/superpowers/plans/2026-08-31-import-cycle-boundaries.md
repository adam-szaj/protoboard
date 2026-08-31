# Import-Cycle Boundary Corrections Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Remove runtime import cycles that obscure ownership of domain, controller,
service, and GUI responsibilities without changing behaviour or the data model.

**Architecture:** Keep value objects in leaf modules and move recursive/query
behaviour behind explicit services or query modules. Import facades may re-export
public names only when doing so does not reintroduce a cycle. Optional GUI,
plugin, and heavy-service imports remain lazy by design.

**Tech Stack:** Python 3.11+, `ast`, pytest, Ruff, Black, mypy, Pyright.

**Spec:** `docs/architecture/modules.md` and the runtime SCC inventory from
2026-08-31.

## Global Constraints

- Preserve runtime behaviour and public domain semantics.
- Do not add compatibility aliases solely to hide a cycle.
- Keep `domain/` free of Qt, controller, and services imports.
- Add or update a focused import-contract test for every removed cycle.
- Commit each independently verified boundary correction.

### Task 1: Make package resolution a leaf dependency — [x]

Modify `domain/layout/placement.py` to import `resolved_footprint` directly from
`domain/components/package/component_package.py`, never through the package
facade. Keep `reach.py` on the same leaf module. Add an AST cycle assertion and
run placement, package-reach, connectivity, and GUI smoke tests.

### Task 2: Separate group value objects from group-tree queries — [x]

Move the free query shims currently in `domain/layout/group.py` to a dedicated
`domain/layout/group_queries.py` module (or update callers directly to
`GroupTree`). Keep `ComponentGroup` as a leaf value object. Update all source and
tests imports, remove the reverse `group -> group_tree` dependency, and preserve
the existing query tests.

### Task 3: Narrow the editing-state dependency graph — [x]

Extract shared protocols/value types used by `editing.controller`,
`headless_session`, `armed_transform`, and `preview_state` into a leaf
`controller/editing/state_ports.py`. Update imports and add a cycle regression
test; do not split or rename `EditState` fields without evidence from callers.

### Task 4: Break service registration cycles — [x]

Move router result values to `services/routing/results.py`, so optimizer
implementations do not import the concrete router module during initialization.
Move built-in package definition loading to `services/library/builtin_package_defs.py`,
so the component and family loaders share a leaf dependency.

### Task 5: Re-run the SCC audit and full verification — [x]

Require zero unexpected runtime SCCs in the targeted domain/controller/service
groups. The AST contract test now checks the complete runtime graph (including
function-local imports while excluding `TYPE_CHECKING` imports). The graph has
zero runtime SCCs. Full unit and offscreen GUI suites pass, as do Ruff, Black,
and the layer boundary checks. Mypy and Pyright retain only the three known
`QTreeWidgetItem | None` diagnostics in
`gui/dialogs/footprint_picker_dialog.py`; no changed module adds a diagnostic.
