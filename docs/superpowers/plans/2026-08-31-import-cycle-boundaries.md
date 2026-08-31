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

### Task 1: Make package resolution a leaf dependency

Modify `domain/layout/placement.py` to import `resolved_footprint` directly from
`domain/components/package/component_package.py`, never through the package
facade. Keep `reach.py` on the same leaf module. Add an AST cycle assertion and
run placement, package-reach, connectivity, and GUI smoke tests.

### Task 2: Separate group value objects from group-tree queries

Move the free query shims currently in `domain/layout/group.py` to a dedicated
`domain/layout/group_queries.py` module (or update callers directly to
`GroupTree`). Keep `ComponentGroup` as a leaf value object. Update all source and
tests imports, remove the reverse `group -> group_tree` dependency, and preserve
the existing query tests.

### Task 3: Narrow the editing-state dependency graph

Extract shared protocols/value types used by `editing.controller`,
`headless_session`, `armed_transform`, and `preview_state` into a leaf
`controller/editing/state_ports.py`. Update imports and add a cycle regression
test; do not split or rename `EditState` fields without evidence from callers.

### Task 4: Break service registration cycles

Make router registration depend on a neutral registry/protocol module, so
`autorouter` does not import `builtin_optimizers` and optimizer plugins do not
import the concrete router implementation during module initialization. Apply
the same principle to library loader/family loader if the cycle is confirmed at
runtime.

### Task 5: Re-run the SCC audit and full verification

Require zero unexpected runtime SCCs in the targeted domain/controller/service
groups. Run unit and offscreen GUI suites, Ruff, Black, mypy, Pyright, and the
existing layer-import contract. Document intentionally retained GUI-only cycles.
