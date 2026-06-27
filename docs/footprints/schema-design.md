<!-- SPDX-License-Identifier: MIT -->
# Package parameter schema — design

**Status:** design / plan. Nothing here is implemented yet. This document is
the agreed model from the 2026-06-27 design session; it exists to be reviewed
before any code is written.

The decision still open after this plan: **scope** — does schema land only on
the YAML/families build path (Phase 1), or do the procedural `kind` generators
get unified onto it too (Phase 2)? See §7.

---

## 1. The problem

Today an abstract base package declares its dimension fields directly inside
`dims:` as bare values (`dims: {diameter: null, length: null}`), where `null`
means "required, a child must fill this". A concrete family then overrides with
a number (`dims: {diameter: 4.55}`). The loader (`loader.py:build_package`,
lines 343-408) reads `dims.<field>` straight through `_require_float`
(loader.py:104) and expects a **float**.

Two limitations follow:

1. **No field metadata survives.** There is no place to record a field's type,
   its default, or whether it is required *other than* the value slot itself.
   The recently-drafted meta-format (`dims.diameter: {type, default}` in
   `assets/packages/generic.yaml`) tried to fix this by putting the description
   *in the same slot* as the value — but `extends` deep-merge
   (`loader.py:_deep_merge`, lines 53-66) then collapses schema and value onto
   one key: a child's `diameter: 5.0` replaces the parent's `{type, default}`
   dict wholesale, and the schema is gone exactly when it is needed.

2. **No way to express a derived parameter.** A DIP body's `length` is a
   function of its pin count, not a number a human wants to type. There is no
   declarative hook for "compute this from another parameter".

## 2. The model

Three separate concerns, each in its own place. **This separation is the whole
design** — it is what makes `extends` deep-merge correct and what gives the
panels something to read.

### 2.1 `schema` — field *declaration*, namespaced and path-mirrored

A package declares its fields under a top-level `schema:` key whose **inner
structure mirrors the value tree**. A field that holds a value at `dims.width`
is described at `schema.dims.width`. A field at `leads.direction` is described
at `schema.leads.direction`. (This path-mirroring is the user's refinement over
a flat `schema.width`: it generalises to *any* section — `dims`, `leads`,
`mount`, whatever comes later — with one mechanism.)

A field declaration is a leaf dict:

```yaml
schema:
  dims:
    width:
      type: float
      required: true
    length:
      type: float
      required: true
    radius:
      type: [float, [float, float], [float, [float, float]]]  # union of shapes
      default: 0.0
  leads:
    direction:
      type: [float, float, float]
      default: [0.0, 0.0, -1.0]
```

Leaf keys:

| key        | meaning                                                                 |
|------------|-------------------------------------------------------------------------|
| `type`     | a single shape (`float`, `int`, `[float, float]`, …) or a **list of allowed shapes** (a union). A list whose elements are themselves shape-tuples is a union; a flat `[float, float, float]` is one shape (a 3-vector). |
| `default`  | value used when the instance omits the field. Mutually informative with `required`. |
| `required` | `true` ⇒ instance (or an `extends` descendant) must supply a value. A field with `required: true` and no `default` is the new spelling of today's `null`. |

**Leaf vs group rule:** a node is a *field declaration* iff it contains a
`type:` key. Otherwise it is a *section* (a branch grouping more fields). The
loader walks the tree with this single rule — no name special-casing.

**Validation is loose (decided 2026-06-27).** Schema validates **only the
fields it declares**. A value present under `dims`/`leads` with *no* matching
`schema` leaf passes through untouched to `build_package` (today's behaviour) —
e.g. `flat_radius`, `flat_normal`, `corner_radius`, `margin`. This keeps the
migration small (bases declare the main dimensions; the long tail of optional
fields needs no schema entry) at the cost of a silent gap: an unknown field is
*not* an error. Strict mode (unknown field ⇒ `PackageDefError`) is a possible
later tightening, tracked in `docs/todo.md`, not done now.

### 2.2 Values — bare, under the mirrored path, in a *separate* subtree

The instance supplies values under `dims:` / `leads:` exactly as today —
nothing in `package_families/` has to change its shape:

```yaml
dims:
  diameter: 4.55      # bare value, shape governed by schema.dims.diameter.type
  length: 4.6
```

Because `schema.dims.diameter` (a dict) and `dims.diameter` (a float) are
**different paths**, `extends` deep-merge never collides them. The schema
subtree merges with schema; the value subtree merges with values; independently
and correctly. This is the structural fix for limitation §1.1.

### 2.3 `auto` — derived parameter, resolved by a generator (not a YAML expression)

When a value should be *computed* rather than typed, the instance writes the
sentinel string `auto` in the value slot:

```yaml
kind: dip_ic
dims:
  pins_per_row: 4     # new input parameter, declared in this kind's schema
  width: 7.62
  length: auto        # "compute me" — not a number
```

The **generator for that `kind`** (Python code) resolves every `auto` to a
concrete number, reading the kind's own new parameters (`pins_per_row`). There
is deliberately **no expression language in YAML** — the value stays pure data,
the computation stays in testable Python. `required` is satisfied by the
*presence* of `auto`; the **type check runs after** the generator has replaced
it (see §3 step 5), so `_require_float` never sees the string.

### 2.4 Schema is overriding, not just additive

A descendant may **narrow** an inherited field: change its `default`, add
`required: true`, or tighten `type` to a subset of shapes. This falls out of
deep-merge for free — `schema.dims.length.default` in a child replaces the
parent's. (Confirmed decision: schema narrows.)

### 2.5 Editability derives from the resolved value — no stored flag

**The read-only rule (decided 2026-06-27):** a field is **editable iff its
resolved value is `null`** — an authored hole the user must fill. Every other
state is read-only:

| resolved value                          | editability | why                          |
|-----------------------------------------|-------------|------------------------------|
| `null` (required, no value)             | **RW**      | a hole the author left open  |
| a concrete value set in YAML            | RO          | the package declared it      |
| a filled `default`                      | RO          | a value like any other       |
| `auto`                                  | RO          | a generator computes it      |

The consequence the author relies on: to make a defaulted field editable, **write
`null` for it explicitly** — `null` is not "absent", it is a deliberate "this is
the user's to set". This means read-only needs **no stored flag**: it is a pure
function of the value tree after `extends` + `apply_to`. The interface is
`ParamSchema.fields_view(values) -> list[FieldView]`; each `FieldView` carries
`{path, spec, value, status}` where `status ∈ {REQUIRED_EMPTY, SET, AUTO}` and
`FieldView.editable` is `status is REQUIRED_EMPTY`. Both panels (§8) render one
`FieldView` per declared field and disable the editor when not `editable`.

> **§A resolved.** No parameter bag is needed for editability. The full
> parameter set lives in `schema.fields` (declarations) crossed with the value
> tree (values); `fields_view` joins them. The bodies stay frozen dataclasses;
> persisting the *resolved* values per instance (so a generated `length`
> round-trips) is a separate persistence concern, not a UI flag — see §6.

## 3. Loader order of operations

The schema-driven build is a fixed pipeline. This *is* the contract:

1. **Resolve `extends`** — deep-merge `schema` subtree and value subtrees
   independently (existing `resolve_extends`, unchanged in spirit).
2. **Apply `default`** — for every leaf in `schema` whose value path is absent,
   fill the value from `default`.
3. **Check `required`** — every `required: true` leaf must have a value
   *present* (where `auto` counts as present).
4. **Run the `kind` generator** — resolve all `auto` value slots to concrete
   numbers using the kind's new parameters. (No-op when there are no `auto`
   slots and no `kind`.)
5. **Validate `type`** — now that no `auto` remains, check each value against
   its `schema.…​.type` (union-aware). This is the new validation layer that
   today does not exist (`type:` is currently ignored).
6. **Build the shape** — dispatch by `shape` into `Cylinder`/`Prism`/`Disc`.
   `_require_float` now receives only clean numbers.

Step 5-after-4 is the crux: today `_require_float` (loader.py:104) would throw
on `auto`; here it never sees it.

## 4. YAML format — before / after

`assets/packages/generic.yaml`, abstract `prism` base:

```yaml
# BEFORE (current draft — schema and value share the dims slot)
- name: prism
  abstract: true
  extends: package
  shape: prism
  dims:
    width:  {type: float, default: null}
    length: {type: float, default: null}
    height: {type: float, default: null}
    radius: {type: [float, [float, float]], default: 0.0}
    radius_direction: {type: [float, float, float], default: [0, 0, 1]}

# AFTER (schema namespace, path-mirrored)
- name: prism
  abstract: true
  extends: package
  shape: prism
  schema:
    dims:
      width:  {type: float, required: true}
      length: {type: float, required: true}
      height: {type: float, required: true}
      radius: {type: [float, [float, float], [float, [float, float]]], default: 0.0}
      radius_direction: {type: [float, float, float], default: [0.0, 0.0, 1.0]}
    leads:
      # direction declared on `package` root (where the field is introduced)
```

A concrete family is **unchanged** — it keeps writing bare values under `dims`:

```yaml
# assets/package_families/smd_chip.yaml — no change required
variants:
  - id: typ
    dims: {length: 2.0, width: 1.25, height: 0.55, corner_radius: 0.0}
```

A kind-based body opts into `auto`:

```yaml
# a DIP family variant (Phase 2 shape)
- id: dip8
  kind: dip_ic
  dims: {pins_per_row: 4, width: 7.62, length: auto}
```

## 5. Where schema is declared (locality)

A field is declared at the level of the `extends` chain that **introduces** it
(user's rule "schema lives where the field is declared"):

- `package` (root) → `schema.leads.direction`
- `prism` → `schema.dims.width/length/height/radius/radius_direction`
- `cylinder` → `schema.dims.diameter/length/radius/radius_direction`
- a `kind` (e.g. `dip_ic`) → `schema.dims.pins_per_row` (its new input) and may
  set `schema.dims.length` to be generator-derived.

`extends` merges these fragments into the descendant's full schema.

## 6. The dataclass / parameter-bag question (§A)

"All parameters persist + generated ones read-only" needs a home. Two options;
**this is a real fork to settle when implementing**, not now:

- **(a) Keep frozen dataclasses, add a sidecar.** Bodies stay as-is
  (`Prism.width_mm`…). Add a parallel `params: Mapping[str, ParamValue]` +
  `derived: frozenset[str]` carrying the full set and the read-only flags, built
  by the loader from schema. Minimal churn to existing geometry code; two
  sources of the same number (typed field + bag) to keep in sync.
- **(b) Bodies hold a parameter mapping as the source of truth.** `width_mm`
  etc. become `@property` over a `params` dict. One source of truth, cleaner
  for arbitrary kinds, but touches every body and every reader of `*_mm`.

Leaning (a) for Phase 1 (smaller, reversible), revisit (b) if kinds proliferate.
Tracked, not decided.

## 7. Scope: Phase 1 (YAML) vs Phase 2 (generators)

The two build paths are disjoint today:

- **YAML/families path:** `generic.yaml` → `package_family_loader` →
  `resolve_extends` → `build_package`. Schema and `auto` fit here natively.
- **Procedural `kind` path:** `kind.py:_KIND_BUILDERS` (`_dip(n)`, `_sil(n)`,
  `_connector(n, rows)`) synthesise a whole `Package` from `pin_count`,
  **bypassing YAML `dims` and `build_package`'s dims parsing**
  (`generated_families.py` calls them at session start).

### Phase 1 — schema on the YAML path only (smaller, isolated)

- Parse `schema:`; implement the §3 pipeline (default → required → validate)
  in/around `build_package`.
- `auto` resolved by a **small dimension solver** invoked before
  `build_package`, for the YAML path. Procedural generators (`kind.py`)
  **untouched** — they remain a separate mechanism.
- Properties panel + place-picker read schema for the YAML path.
- **Risk:** low. Additive. No existing generator test repointed.
- **Cost of leaving Phase 2 undone:** kind-generated bodies (DIP/SIL) do not yet
  expose an editable `pins_per_row` through schema; they stay procedural. Track
  in `docs/todo.md` (CLAUDE.md rule: deferred parity is tracked debt).

### Phase 2 — **limited scope: generator API only** (no chain, no rewrite)

Decided 2026-06-27: do **not** rewrite `_dip/_sil/_connector` to emit `dims` +
`auto`. The procedural builders keep their behaviour. The only change is the
**interface**, so a generator can read schema + current/derived values and so a
generator *chain* becomes possible later — the chain itself is **not built now**.

New signature shape (frozen in → frozen out, composable):

```
generator_X(ctx, schema) -> new_ctx
generator_Y(new_ctx, schema) -> newer_ctx
```

```python
@dataclass(frozen=True, slots=True)
class GeneratorContext:
    pin_count: int
    values: Mapping[str, object]      # current values incl. earlier-computed
    package: Package | None = None    # built body once it exists (None on input)

@dataclass(frozen=True, slots=True)
class ParamSchema:                     # the "Schema" arg
    fields: Mapping[str, FieldSpec]    # path -> {type, default, required}
```

A generator returns a **new** `GeneratorContext` with added/derived values (and
optionally `package`). No chain driver, no `auto`-resolution wired in yet — just
the shape that makes both possible.

**Public API moves too** (decided): `build_kind_package` changes to take/return
the context, and its callers + tests are repointed.

Scope facts (from the call-site map):

- **Only one production caller** of `build_kind_package`:
  `services/library/loader.py:210` (`_build_component_package`); it builds
  `params` from the component's `raw` dict (only real extra key today: `rows`
  for `pin_header`). Repoint this to build a `GeneratorContext` and read the
  `package` out of the returned context.
- **`dip_family`/`sil_family` do NOT call `build_kind_package`** — they go
  through the private `_prism_from_footprint`. So `generated_families.py` is
  largely untouched; the public-API change does not ripple there.
- **Tests to repoint (11, two files):** `tests/gui/test_body_shapes.py` (8:
  dip_ic/dip_switch/pin_header×3/jst/connectors-loop) and
  `tests/unit/test_library_loader.py` (3: dip_ic-matches-dual-row,
  odd-count-rejected, unknown-kind-rejected). Repoint their *behaviour*
  assertions onto the new signature; do not delete them (CLAUDE.md).
- **Untouched importers** of other `kind.py` symbols (`is_generic_package` in
  `drc/engine.py`, `reach.py`; `_prism_from_footprint` in
  `generated_families.py`) — out of scope, leave alone.

**Out of scope (deferred, track in `docs/todo.md`):** the chain driver,
`auto`-resolution *through* generators, and emitting `dims` from generators.

## 8. Panels (read schema) — designed *after* schema lands

Both panels are **consumers** of the schema, so they are specified only once the
schema API exists. Recorded here as intent; full design follows implementation.

### 8.1 Shared core

A single schema-driven field-list widget renders one row per `schema` leaf:
label (from field name), an editor chosen by `type` (float spin, int spin,
2-/3-vector triple-spin, union → the active shape's editor), and a read-only
presentation when the field is `derived`/generator-owned. Both panels below
embed this core; they differ only in data source and commit target.

### 8.2 Properties panel (`gui/panels/properties_panel.py`)

- **Source:** the selected `ComponentInstance` → its resolved package → schema +
  current values.
- **Editable:** non-derived fields and `pins_per_row`-style inputs. Editing a
  generator input recomputes derived fields live (they stay read-only).
- **Commit:** through the existing command path (undo/redo). Per-instance
  overrides ride on `ComponentInstance.package_variant` /
  appearance-style override slots (instance.py:21-80).

### 8.3 Place-tool package picker

- **Source:** the package family/variant being placed → same schema.
- **Shared widget:** identical field-list core; difference is it edits a
  *pending* package spec, not a committed instance, and feeds the place command.
- Resolves the prior "wspólny kod?" question: **yes, one widget**, two hosts
  (properties vs place dialog), parameterised by source + commit callback.

## 9. Tests that must not regress (Phase 1)

From `tests/unit/test_package_loader.py`: `test_merge_chain`,
`test_required_null_raises`, `test_abstract_not_built`, `test_resistor_axial`,
`test_dip8_runlength_and_topology`, `test_flat_sided_cylinder_parsed`,
`test_disc_shape_built`, eccentricity tests.
From `tests/unit/test_package_family_loader.py`: `test_loads_builtin_families`,
`test_every_variant_builds`, `test_to92_is_flat_sided_cylinder`,
`test_smd_chip_is_two_pin_axial_prism`.

The `null`-required tests change spelling (`required: true`) — repoint, do not
delete, their behaviour assertions.
