<!-- SPDX-License-Identifier: MIT -->
# Module Boundaries and Deduplication Design

**Date:** 2026-08-30
**Status:** approved; Workstreams 1–3 implemented, Workstream 4 next

## Purpose

Simplify `src/protoboard` without changing application behaviour. The work
removes implementation leakage between layers, replaces repeated mechanical
idioms with a small set of explicit reusable operations, deletes superseded
legacy paths, and reduces commentary that only restates the code.

The one-third line-count reduction is an aspirational target, not an acceptance
gate. Readability, explicit domain semantics, and behavioural equivalence take
priority over minimizing physical lines.

## Baseline and Findings

The current source contains 298 Python modules and 96,669 physical lines. The
largest areas are `gui` (30,533 lines), `services` (24,929), `domain` (20,813),
and `controller` (19,049).

The audit found five recurring causes of size and coupling:

1. Layer violations hidden by local and `TYPE_CHECKING` imports. Domain code
   imports instrumentation, routing policy, pad policy, and value formatting
   from `services` or `gui`. Persistence and export services import GUI state
   and renderers.
2. Broad context objects. `EditState` combines project data, command execution,
   connectivity caches, spatial queries, selection, tool state, and GUI
   notifications. `PreviewState` consequently delegates almost the complete
   interface although it changes only preview geometry.
3. Repeated query pipelines. Component, placement, definition, package option,
   wire, pad, and net resolution are repeatedly reconstructed at call sites.
4. Parallel implementations. Examples include old and new group helpers,
   action/keybinding metadata, export setup, drag fallbacks, and compatibility
   aliases retained after callers moved to their replacements.
5. Narrative source comments. Long histories and architecture explanations
   duplicate `docs/` and make the executable path harder to scan.

Exact syntax clones are not the main issue. Most removable volume is semantic
duplication: the same operation expressed with slightly different local data
plumbing.

## Design Principles

- A dependency points inward. Inner code never imports a concrete outer
  implementation.
- Reuse is explicit and typed. Prefer a named value object or a small pure
  function over reflection, `__getattr__`, anonymous tuples, or configuration
  machinery.
- A shared abstraction must represent one domain operation and have at least
  two real consumers. Similar-looking complex algorithms remain explicit when
  their invariants differ.
- Delete a superseded path in the same phase that moves its final callers.
- Preserve comments that explain *why*: invariants, coordinate systems,
  algorithm derivations, concurrency, undo safety, and persistence hazards.
  Remove comments that narrate history or restate clear code.
- Avoid compatibility scaffolding. This project stage does not require legacy
  YAML compatibility, and internal aliases or re-exports are removed after
  migration.

## Target Dependency Model

Existing package names remain stable where practical; a repository-wide rename
of `controller` to `application` would add churn without improving the model.

```text
domain                 entities, value objects, pure policies and queries
  ^
controller             use cases, editing orchestration, inward-facing ports
  ^
services               persistence, routing engines, recording, file adapters
  ^
gui                    Qt composition, interaction adapters, rendering/export
```

The arrows mean "may be depended upon." The concrete rules are:

- `domain` may import only domain modules and neutral root utilities such as
  `geometry`, `types`, errors, and dependency-free instrumentation.
- `controller` may import domain code and its own port declarations, but not
  `gui` or concrete service adapters.
- `services` may import domain code plus explicitly designated controller ports
  and stable view/configuration DTOs. It may not import controller tools/use
  cases or GUI code.
- GUI and headless/CLI entry points are composition roots. They connect
  controller ports to service or Qt implementations; neither connection leaks
  back into controller code.

An AST-based import-contract test enforces module-scope, local, and
`TYPE_CHECKING` imports. A small allow-list covers only intentional port
imports; it does not allow package-wide exceptions.

### Initial relocations

- Move `pad_strategy` into the domain layer.
- Move pure value-code parsing out of `gui.renderers` into domain components.
- Move routing settings, cost-model value types, and simplify-mode types so
  domain configuration no longer imports routing implementations.
- Move call counters and tracing decorators to a dependency-neutral
  instrumentation module.
- Move the pure drag detector and structured conflict facts into domain layout.
  It returns typed reason codes and action intents instead of opaque
  `tuple[object, ...]` actions or presentation strings. Controller code maps
  those facts to commit operations; GUI code maps them to user-facing text.
- Keep PDF/PNG/SVG as technical service adapters, but extract the board-rendering
  kernel they share with the GUI from `gui.renderers` into
  `services.rendering`. Interaction overlays remain in GUI. `EditController`
  invokes export, persistence, DRC, and routing through small injected ports
  rather than local imports of implementations.
- Move `Theme`, `LayerOverrides`, `LayerVisibility`, and other stable view DTOs
  into a controller-owned `view_options` module. Theme/YAML file I/O stays in
  services, while rendering behaviour and z-order stay in GUI. Services may
  import these DTOs through the narrow allow-list, not arbitrary controller
  modules.

## Narrow Contexts Instead of `EditState` Leakage

`HeadlessSession` and `EditorSession` remain composition objects, but consumers
request only the capabilities they need. Protocols are grouped by coherent use
case rather than created per method:

- project model reads: board, layout, schematic, instances, definitions, and
  package families;
- spatial reads: component bounds, pin positions, and spatial indices;
- connectivity reads: wires, pad index, graph, conflicts, and net maps;
- editing effects: command execution and model-change notification;
- interaction state: selection, highlights, active routing net, and tool
  notifications.

`EditController` may accept a composite editing context, while helpers and
tools use the narrow constituent protocols. Preview logic receives a spatial
project view only. `PreviewState` therefore becomes a small preview-geometry
adapter instead of a proxy for selection, DRC, tool state, and notifications.

The design does not use automatic delegation. Explicit narrow contracts make
missing dependencies visible and prevent the protocol from silently growing.

## Canonical Reusable Operations

The following operations replace proven repeated idioms:

1. **Component context resolution.** A domain query returns a named
   `ResolvedComponent` containing the instance, definition, placement, selected
   package option, and concrete package when available. Callers explicitly
   handle a missing result. It reuses existing package-option resolution rather
   than duplicating it.
2. **Collection lookup.** Canonical indexed collections provide wire and group
   lookup; call sites stop scanning with `next(...)`.
3. **Board and geometry primitives.** All callers use the existing canonical
   plated-through-pad query. Shared segment orientation/intersection primitives
   move to `geometry`; routing-specific policy stays in routing algorithms.
4. **Command serialization.** A typed `Codec[T]` registry and explicit field
   codecs replace symmetric `*_to`/`*_from` boilerplate. Commands with nested,
   versioned, or heterogeneous structures keep custom codecs. The serialized
   schema remains visible in code; unrestricted dataclass reflection is not
   used.
5. **Export snapshot construction.** One builder captures canonical board state;
   PDF, PNG, and SVG adapters only select their output device and options.
6. **Recent-value persistence.** One generic MRU store owns JSON loading,
   validation, deduplication, truncation, and atomic writing. Footprints and kind
   variants provide typed encode/decode functions.
7. **Snapping.** One pure snap operation consumes a narrow spatial-query port
   and a request value object. Board view, controller, and tools share its
   candidate selection and tie-breaking rules.
8. **Tool command execution.** A controller-owned execution port replaces local
   copies of command dispatch, status reporting, and wire lookup in selection
   tools.
9. **Action metadata.** Action identifier, label, default binding, category, and
   enablement metadata have one authoritative declaration. GUI builders and
   keymaps consume it instead of maintaining parallel tables.

These abstractions are introduced only after characterization tests show that
the candidate call sites have the same semantics.

## Legacy and Comment Removal

Each legacy candidate must identify its replacement and all production callers.
The phase then:

1. repoints remaining callers and tests;
2. runs focused behavioural checks;
3. removes the shim, alias, fallback, and tests that exist only for the old
   path;
4. verifies no references remain.

Initial targets include free-function group shims, old drag fallback fields,
duplicate action/keybinding declarations, legacy highlight state, redundant
export entry paths, compatibility aliases, and obsolete YAML migrations.

Comments and docstrings are reviewed only in files changed for functional or
structural reasons. Module histories, task references, obvious forwarding
descriptions, and repeated architecture prose move to or remain in `docs/`.
Complex routing, geometry, transformation, undo, threading, and persistence
invariants stay next to the code they constrain.

## Delivery Sequence

The scope is too large for one safe implementation batch. This design is the
parent contract for four independently planned workstreams:

1. dependency enforcement and relocation of pure policies;
2. application ports and narrow editing contexts;
3. canonical reusable operations and legacy deletion;
4. GUI/controller decomposition, comment cleanup, and final audit.

Each workstream receives its own executable plan and verification gate. Work on
the next one starts only after the previous workstream leaves the full relevant
suite green.

Implementation status as of 2026-08-31:

- Workstream 1 established dependency enforcement and relocated pure policies.
- Workstream 2A introduced editing-service ports, structured domain drag
  analysis, stable view-option DTOs, and root-level headless/CLI composition.
- Workstream 2B split editing state into focused contexts, reduced
  `PreviewState` to 98 lines, and moved all routing implementation selection
  behind service adapters. The blanket AST contract now reports zero
  controller imports from `services` or `gui`.
- Workstream 3A completed canonical reusable operations for component
  resolution, indexed wire lookup, plated-pad enumeration, and geometry
  predicates; superseded group shims and duplicate implementations were
  deleted. Workstream 3B completed typed command codecs, shared export snapshot
  construction, and generic atomic MRU persistence. Workstream 3C completed the
  pure snapping operation, controller-owned tool command execution, and the
  action metadata registry. Workstream 4 is next: GUI/controller decomposition,
  selective comment cleanup, and the final audit.

The Workstream 2B exit gate covered 315 source modules and 97,346 physical
lines. The Workstream 3A exit gate covers 316 source modules and 97,121
physical lines: 2,842 unit tests passed (68 skipped), 1,494 offscreen GUI
tests passed (1 expected xfail and 1 existing PySide disconnect warning), and 3
property tests passed. Ruff, Black, strict mypy, and Pyright reported no
errors.

The Workstream 3B exit gate covers 317 source modules and 97,183 physical
lines: 2,848 unit tests passed (68 skipped), 1,494 offscreen GUI tests passed
(1 expected xfail and 1 existing PySide disconnect warning). Ruff, Black,
strict mypy, and Pyright reported no errors; the Workstream 3A property gate
remains valid because 3B changes do not affect routing or geometry inputs.

The Workstream 3C exit gate covers 318 source modules and 97,128 physical
lines: 2,854 unit tests passed (68 skipped), 1,496 offscreen GUI tests passed
(1 expected xfail and 1 existing PySide disconnect warning). Ruff, Black,
strict mypy, Pyright, and `git diff --check` reported no errors. The audit
found one canonical snap operation and no direct command-stack execution in
the migrated tools. Manual GUI smoke testing remains deferred to Workstream 4.

Workstream 4 has started with a focused decomposition of `SelectTool`: pure
selection and persistent-highlight context option builders now live in a
Qt-free selection module. Stale stage-only commentary was also removed from
the touched controller and GUI modules. The first checkpoint covers 319 source
modules and 97,139 physical lines; 109 focused unit/GUI tests pass, and manual
GUI smoke testing remains deferred to the final Workstream 4 gate.

The next Workstream 4 checkpoint relocates the Qt-free `LayerState` DTO to
`controller.view_options` and keeps tool-dependent construction in a GUI
adapter. The tree now contains 320 source modules and 97,145 physical lines;
the layer-boundary gate and 49 focused rendering tests pass. No compatibility
alias for the removed GUI module remains.

Within those workstreams, the order is:

1. Record reproducible LOC and import baselines; add the import-contract test.
2. Relocate pure policies and neutral instrumentation, one dependency family at
   a time.
3. Introduce application ports and remove controller imports of concrete
   services.
4. Split consumer signatures away from the broad `EditState`; shrink preview
   delegation.
5. Introduce canonical queries and generic utilities, migrating one complete
   idiom at a time.
6. Consolidate action/keybinding and export pipelines.
7. Delete verified legacy paths and their compatibility-only tests.
8. Decompose the largest controller and GUI files along the new use-case
   boundaries; do not split files merely to lower per-file LOC.
9. Perform selective comment/docstring reduction and update architecture docs.
10. Run the complete behavioural, type, GUI, performance, dependency, and LOC
    audit.

Every stage must leave the repository runnable and independently reviewable.
Commits, when requested, should align with these stages rather than combine
unrelated moves and behaviour changes.

## Behaviour Preservation and Verification

Before changing a repeated path, add or identify tests that characterize its
observable behaviour. During implementation:

- focused unit tests cover each moved policy or canonical query;
- property tests cover geometry, snapping, and serialization round trips;
- parity tests compare old and new paths immediately before the old path is
  deleted;
- import-contract tests prove the dependency rules;
- the headless unit suite and offscreen GUI suite run after each affected layer;
- `ruff`, Black, strict mypy, and Pyright validate the final source;
- routing benchmarks and representative project smoke tests guard performance
  and end-to-end behaviour;
- GUI smoke tests cover project creation/loading, component placement, package
  selection, wiring, undo/redo, saving, and export.

Legacy YAML fixtures are updated to the current schema rather than preserved as
compatibility requirements.

## Metrics and Acceptance

The baseline is recorded both as physical source lines and as logical code
excluding blanks, comments, and docstrings. Final reporting includes:

- total and per-layer LOC delta;
- removed legacy modules/functions and compatibility branches;
- eliminated duplicate-idiom call sites;
- forbidden import count, which must be zero outside explicit port imports;
- size and member count of broad context protocols;
- test, static-analysis, GUI-smoke, and benchmark results.

A one-third physical LOC reduction is desirable but not required. No code is
compressed into opaque expressions, reflection, or oversized generic helpers to
reach it. The refactoring is accepted when behaviour is preserved, forbidden
dependencies are eliminated, identified duplicate idioms use their canonical
operations, superseded paths are gone, and the resulting code is demonstrably
easier to read.

## Non-goals

- Adding application features or changing the component/package domain model.
- Rewriting mature routing algorithms solely to reduce line count.
- Renaming every top-level package.
- Preserving obsolete internal APIs or YAML schemas.
- Removing algorithmic explanations or safety-critical rationale.
