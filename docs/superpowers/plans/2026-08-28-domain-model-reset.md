# SPDX-License-Identifier: MIT
<!-- Date: 2026-08-28 -->
# Domain model reset plan

## Why a reset branch

The `footprint-ref-model` branch contains useful design intent, but it mixes:

- target-model changes,
- partial call-site migration,
- temporary experiments that currently break domain contracts.

The new branch starts from `main` and imports only the decisions worth keeping.

## Scope

This reset focuses on the component/package/footprint model only.

Out of scope for the first pass:

- knot hierarchy,
- selection/drag state machines,
- GUI redesign,
- broad cleanup outside the touched model.

## Target model

The model must have one source of truth per concept.

### 1. ComponentDefinition

Keep as the static logical definition of a component.

Required responsibilities:

- logical identity: `name`, `component_class`, `symbol`, metadata,
- pin naming and pin variants,
- declared package choices,
- one default geometry derived from those choices.

Required invariant:

- every loaded definition exposes one default geometry through one stable API.

### 2. ComponentInstance

Keep as the placed-or-placeable runtime value.

Required responsibilities:

- instance identity: `id`, `refdes`, `value`,
- selected package option for this placement,
- selected pin variant for this placement,
- resolving the concrete geometry from definition + instance state.

Required invariant:

- placement-time geometry is resolved through the instance, never by guessing from the definition alone.

### 3. PackageOption

Keep one option type only. Remove the `FootprintOption` / `PackageOption` split.

Required responsibilities:

- represent one user-selectable geometry choice,
- carry either a catalogue reference or inline package data,
- expose a label and optional visual override.

Required invariant:

- exactly one of `ref` or `package` is set.

### 4. PackageFamily / PackageVariant

Keep as the catalogue model.

Required responsibilities:

- group built packages under one stable family id,
- provide default and explicit variant resolution,
- stay independent from GUI picker details.

Required invariant:

- resolution rules are deterministic and testable without GUI.

### 5. Package vs Footprint

Keep both concepts, but make the relation explicit:

- `Package` is the full body model,
- `Footprint` is the geometry protocol consumed by placement/render/layout code,
- every `Package` used by components must implement the `Footprint` protocol directly.

This avoids two parallel object graphs and keeps package-specific behaviour on the package type.

## Keep / merge / delete

### Keep

- `src/protoboard/domain/components/definition.py`
- `src/protoboard/domain/components/instance.py`
- `src/protoboard/domain/components/package/family.py`
- `src/protoboard/domain/components/package/shapes.py`
- `src/protoboard/services/library/loader.py`

### Merge

- `FootprintOption` into `PackageOption`
- legacy package resolution paths into one `resolve_component_package(...)`
- default-footprint derivation into one definition-level API

### Delete

- duplicate model in `src/protoboard/domain/components/component.py`
- stale adapter paths that preserve old naming without adding behaviour
- fallback paths that silently swap unresolved geometry for a different body

### Temporary adapters allowed

- thin compatibility wrappers at GUI/service boundaries,
- field aliases during one migration step,
- loader normalization for old authoring forms if they terminate in one runtime model.

## Naming rules

- Use one term for the per-instance body choice. Recommended: `package_variant`.
- Use one term for the per-definition choice list. Recommended: `footprints` only if the list is user-facing; otherwise rename to `package_options`.
- Do not keep both `package_option_ref` and `package_variant`.
- Do not reintroduce `defn.footprint` as stored state.

## Architectural rules

### Rule 1: one runtime model

Authoring may have sugar, but runtime may not.

- loader normalizes `package:` / `footprints:` input into one structure,
- domain services operate only on that normalized structure.

### Rule 2: no silent geometry fallback

If a selected reference does not resolve, the failure must be explicit.

Allowed:

- load error,
- typed degradation with an explicit test.

Not allowed:

- quietly using a different package because some branch fell through.

### Rule 3: behaviour stays on the model

Callers should not need to know which concrete package/footprint type they hold to perform routine operations.

Using polymorphism: shared interface on `Package` / `Footprint`, because geometry behaviour already varies by concrete type and the existing type branches have clear growth pressure.

## Refactor order

1. Remove model duplication.
   Verify: there is one `ComponentDefinition`, one option type, one package-resolution path.

2. Lock the core invariants with small domain tests.
   Verify: tests describe behaviour, not internal fields or list positions beyond the domain contract.

3. Refactor the loader to normalize authoring into the single runtime model.
   Verify: loader outputs the same shape regardless of authoring sugar.

4. Repoint service and GUI boundaries to the stable API.
   Verify: GUI code does not reconstruct domain logic on its own.

5. Remove temporary adapters.
   Verify: no dead aliasing remains in the touched model.

## First test gate

These tests should exist before the invasive refactor starts:

- loading a component with one inline package yields one default geometry,
- loading a component with multiple package options yields the first as the default,
- selecting an instance package variant changes resolved geometry,
- unresolved selected variant fails explicitly,
- family default resolution and explicit variant resolution are both covered,
- `ComponentInstance.resolved_footprint()` is the only placement-path geometry API.

## Success criteria

The first refactor stage is done when:

- one component model remains,
- one option model remains,
- one package-resolution path remains,
- the loader produces one normalized runtime shape,
- the replacement tests guard those invariants on `main`-derived code.
