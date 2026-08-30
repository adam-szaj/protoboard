# MetaPin and Package Pin Mapping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace component-level physical pin numbering with ordered MetaPins and map them to physical Package pins in each PackageOption.

**Architecture:** `ComponentDefinition` owns immutable logical `MetaPin` values. `PackageOption` owns the only logical-to-physical mapping, while mapping helpers provide validated forward and inverse lookup. Schematic identity uses MetaPin ordinals; geometry translates them only at the active package boundary.

**Tech Stack:** Python 3.14, frozen dataclasses, ruamel.yaml, pytest, PySide6, mypy --strict, Pyright

**Spec:** `docs/superpowers/specs/2026-08-29-metapin-package-mapping-design.md`

## Global Constraints

- No backward compatibility for component-library YAML.
- MetaPin ordinals are 1-based and derive only from tuple position.
- One MetaPin maps to zero or more physical Package pins.
- One physical Package pin maps to at most one MetaPin per PackageOption.
- Package geometry remains exclusively in the package-family catalogue.
- Do not introduce component-class or package-kind dispatch.

---

### Task 1: MetaPin and PackageOption invariants

**Files:**
- Modify: `src/protoboard/domain/components/definition.py`
- Test: `tests/unit/test_component_model_invariants.py`

**Interfaces:**
- Produces: `MetaPin(labels: tuple[str, ...])`
- Produces: `ComponentDefinition.meta_pins: tuple[MetaPin, ...]`
- Produces: `PackageOption.pin_map: dict[int, tuple[int, ...]]`
- Produces: `ComponentDefinition.meta_pin(index: int) -> MetaPin`

- [ ] **Step 1: Write failing model tests**

```python
def test_metapin_requires_non_empty_labels() -> None:
    with pytest.raises(ValueError):
        MetaPin(())

def test_package_option_maps_metapin_to_many_package_pins() -> None:
    option = PackageOption("DIP", "dip-8/std", {1: (1, 8), 2: ()})
    assert option.pin_map[1] == (1, 8)
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m pytest tests/unit/test_component_model_invariants.py -q`
Expected: failure because `MetaPin` and `pin_map` do not exist.

- [ ] **Step 3: Implement minimal model**

Make `meta_pins` the stored source of truth. Validate non-empty labels and
positive MetaPin keys, and implement 1-based `meta_pin()` lookup. Keep
`pin_names` and `pin_count` temporarily as read-only projections of the first
MetaPin label so consumers can be migrated in later tasks without a second
stored pin model. Do not load or author new `PinVariant` data.

- [ ] **Step 4: Verify and commit**

```bash
.venv/bin/python -m pytest tests/unit/test_component_model_invariants.py -q
git add src/protoboard/domain/components/definition.py tests/unit/test_component_model_invariants.py
git commit -m "refactor(component): introduce metapins"
```

### Task 2: YAML normalization and mapping validation

**Files:**
- Modify: `src/protoboard/services/library/loader.py`
- Modify: `src/protoboard/services/library/parametric.py`
- Test: `tests/unit/test_library_loader.py`
- Test: `tests/unit/test_parametric_components.py`

**Interfaces:**
- Consumes: `MetaPin`, `PackageOption.pin_map`
- Produces: normalized MetaPins and validated mappings for loaded definitions

- [ ] **Step 1: Add failing loader tests** for scalar and multi-label entries,
unmapped MetaPins, one-to-many mappings, duplicate physical pins, and invalid
logical or physical indices.

```python
raw = {
    "name": "Q",
    "meta_pins": [["B"], ["C", "collector"], "E"],
    "package": {"kind": "generic", "pin_count": 3},
    "pin_map": {1: [2], 2: [1], 3: [3]},
}
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `.venv/bin/python -m pytest tests/unit/test_library_loader.py tests/unit/test_parametric_components.py -q --maxfail=1`

- [ ] **Step 3: Parse new YAML only**

Normalize a scalar to one label. Put top-level inline `pin_map` on its generated
default option and parse `package_options[].pin_map` for references. Resolve the
package before checking duplicates and ranges. Parametric concretization creates
one MetaPin per terminal and an identity mapping.

- [ ] **Step 4: Verify and commit**

```bash
.venv/bin/python -m pytest tests/unit/test_library_loader.py tests/unit/test_parametric_components.py -q
git add src/protoboard/services/library/loader.py src/protoboard/services/library/parametric.py tests/unit/test_library_loader.py tests/unit/test_parametric_components.py
git commit -m "refactor(loader): parse metapin mappings"
```

### Task 3: Active option mapping API

**Files:**
- Create: `src/protoboard/domain/components/package/pin_mapping.py`
- Modify: `src/protoboard/domain/components/package/__init__.py`
- Test: `tests/unit/test_component_pin_mapping.py`

**Interfaces:**
- Produces: `resolve_package_option(defn, instance) -> PackageOption`
- Produces: `package_pins_for_meta_pin(defn, instance, index) -> tuple[int, ...]`
- Produces: `meta_pin_for_package_pin(defn, instance, number) -> int | None`

- [ ] **Step 1: Write failing forward/inverse mapping tests**, including an
instance override and an unmapped physical pin.
- [ ] **Step 2: Verify RED** with `.venv/bin/python -m pytest tests/unit/test_component_pin_mapping.py -q`.
- [ ] **Step 3: Implement lookup without kind/type branches.** Select the option
matching `instance.package_variant`, otherwise the first option. Forward lookup
uses `pin_map.get(index, ())`; inverse lookup scans the validated mapping.
- [ ] **Step 4: Verify and commit**

```bash
.venv/bin/python -m pytest tests/unit/test_component_pin_mapping.py tests/unit/test_component_package_resolve.py -q
git add src/protoboard/domain/components/package/pin_mapping.py src/protoboard/domain/components/package/__init__.py tests/unit/test_component_pin_mapping.py
git commit -m "refactor(package): map metapins to physical pins"
```

### Task 4: PinRef and schematic connectivity

**Files:**
- Modify: `src/protoboard/domain/schematic/net.py`
- Modify: `src/protoboard/domain/components/instance.py`
- Modify: `src/protoboard/domain/schematic/schematic.py`
- Modify: `src/protoboard/services/persistence/project_yaml.py`
- Modify: `src/protoboard/services/recording/serialization.py`
- Test: `tests/unit/test_schematic.py`
- Test: `tests/unit/test_project_yaml.py`

**Interfaces:**
- Produces: `PinRef.meta_pin_index: int`
- Produces: `ComponentInstance.meta_pin_ref(index: int) -> PinRef`

- [ ] **Step 1: Add failing tests** proving two physical pins of one MetaPin
share one `PinRef`, while duplicate labels on distinct MetaPins do not merge.
- [ ] **Step 2: Verify RED** with the schematic and project-YAML tests.
- [ ] **Step 3: Rename identity and persistence.** Replace `pin_number` with
`meta_pin_index`, build one identity per ordinal, serialize `meta_pin`, and stop
reading old `pin` and `pin_variant` keys.
- [ ] **Step 4: Verify and commit**

```bash
.venv/bin/python -m pytest tests/unit/test_schematic.py tests/unit/test_project_yaml.py -q
git add src/protoboard/domain/schematic src/protoboard/domain/components/instance.py src/protoboard/services/persistence/project_yaml.py src/protoboard/services/recording/serialization.py tests/unit/test_schematic.py tests/unit/test_project_yaml.py
git commit -m "refactor(schematic): identify connections by metapin"
```

### Task 5: Physical geometry consumers

**Files:**
- Modify: `src/protoboard/domain/board/pad_state.py`
- Modify: `src/protoboard/domain/layout/`
- Modify: `src/protoboard/domain/connectivity/incremental.py`
- Modify: `src/protoboard/services/drc/engine.py`
- Modify: `src/protoboard/services/routing/`
- Test: `tests/unit/test_pad_state.py`
- Test: `tests/unit/test_connectivity.py`
- Test: `tests/unit/test_drc.py`

- [ ] **Step 1: Add failing vertical tests** showing one MetaPin mapped to two
pads puts both pads on one net and an unmapped package pin creates no PinRef.
- [ ] **Step 2: Verify RED** with pad-state, connectivity, and DRC tests.
- [ ] **Step 3: Translate only at geometry boundaries.** Package loops retain
physical numbers. Translate before net access; expand MetaPins before locating
pads. Remove assumptions that logical and physical counts match.
- [ ] **Step 4: Run `.venv/bin/python -m pytest tests/unit -q --maxfail=1` and
commit as `refactor(layout): translate metapins at package boundary`.

### Task 6: GUI, integrations, and authored libraries

**Files:**
- Modify: `src/protoboard/gui/`
- Modify: `src/protoboard/controller/`
- Modify: `src/protoboard/services/kicad/`
- Modify: `src/protoboard/services/export/`
- Modify: repository component-library YAML
- Test: `tests/gui/`

- [ ] **Step 1: Add failing GUI tests** for displaying MetaPin labels, selecting
a physical pin, and showing one net on multiple mapped pins.
- [ ] **Step 2: Verify RED** with renderer and panel tests.
- [ ] **Step 3: Render the first label compactly and all labels in detailed UI.**
Remove pin-variant actions. Convert every library component to `meta_pins` and
explicit `pin_map`. Translate KiCad physical pads at its boundary.
- [ ] **Step 4: Run `.venv/bin/python -m pytest tests/gui -q` and commit as
`refactor(gui): present and select metapins`.

### Task 7: Remove legacy pin API and establish a green baseline

**Files:**
- Modify: remaining files found by repository-wide searches
- Modify: remaining tests and authoring documentation

- [ ] **Step 1: Require no production matches**

```bash
rg -n "pin_names|PinVariant|pin_variant|defn\.pin_count|PinRef\([^)]*pin_number" src
```

At this point remove the temporary `pin_names` and `pin_count` projections and
the remaining `PinVariant` implementation.

- [ ] **Step 2: Run formatting and types**

```bash
.venv/bin/python -m black --check src tests
.venv/bin/python -m mypy --strict src
.venv/bin/pyright
git diff --check
```

- [ ] **Step 3: Run complete automated verification**

```bash
.venv/bin/python -m pytest tests/unit -q
.venv/bin/python -m pytest tests/gui -q
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/gui -q
```

- [ ] **Step 4: Run the established GUI smoke workflow**, update MetaPin
architecture and authoring documentation, then commit only after every check
passes. This is the baseline for Tasks 8-12, not the final branch verification.

### Task 8: Typed package references normalized to PackageOption

**Files:**
- Modify: `src/protoboard/services/library/loader.py`
- Modify: `src/protoboard/services/library/parametric.py`
- Test: `tests/unit/test_library_loader.py`
- Test: `tests/unit/test_parametric_components.py`

**Interfaces:**
- Consumes: `PackageOption(name, ref, pin_map, style)` and the package-family
  catalogue
- Produces: YAML refs `@family[/variant]`, `#named-package`, and `$kind`
  normalized to canonical unprefixed `PackageOption.ref` addresses

- [ ] **Step 1: Add failing reference tests** proving that `@` resolves a
family or variant, `#` builds a named package, `$` materializes a kind, and a
bare or unknown ref fails loudly. Assert that every resulting option has a
validated `pin_map` and contains no inline `Package`.
- [ ] **Step 2: Verify RED** with
`.venv/bin/python -m pytest tests/unit/test_library_loader.py tests/unit/test_parametric_components.py -q --maxfail=1`.
- [ ] **Step 3: Implement parse-time dispatch.** Strip `@` after resolving the
family reference. Build `#` and `$` references through the existing package
registry/generator, register the result under an internal family id, and store
only its canonical address. Reject `package:` together with
`package_options:`. Keep `package:` sugar registering
`<component-name>-package/std` and attach its top-level `pin_map` to the
generated option.
- [ ] **Step 4: Verify and commit** the focused loader tests, then
`git diff --check`; commit as `refactor(loader): normalize typed package refs`.

### Task 9: Namespaced family invariants

**Files:**
- Modify: `src/protoboard/domain/components/package/family.py`
- Modify: `src/protoboard/services/library/package_family_loader.py`
- Test: `tests/unit/test_package_family.py`
- Test: `tests/unit/test_package_family_loader.py`

**Interfaces:**
- Produces: family refs resolving their declared default variant
- Produces: catalogue validation rejecting a family id that shadows another
  family's variant address

- [ ] **Step 1: Add failing tests** for `r-tht/DIN` resolving its default,
`r-tht/DIN/0207` resolving a concrete size, missing defaults, and the collision
between family `r-tht/DIN/0207` and variant `0207` of `r-tht/DIN`.
- [ ] **Step 2: Verify RED** with the two focused package-family test files.
- [ ] **Step 3: Implement the minimum validation** in the catalogue loader.
Preserve the existing flat `PackageFamily -> PackageVariant` dataclasses; the
namespace lives only in `family_id`. Do not add type/kind dispatch.
- [ ] **Step 4: Verify and commit** as
`refactor(package): validate namespaced family addresses`.

### Task 10: Migrate catalogue, libraries, and fixtures in one pass

**Files:**
- Rewrite: `assets/package_families/*.yaml`
- Modify: `library/**/*.yaml`
- Modify: package addresses in `tests/**/*.yaml` and affected Python fixtures
- Test: `tests/unit/test_package_builtin_yaml.py`
- Test: `tests/unit/test_library_loader.py`

**Interfaces:**
- Consumes: typed refs from Task 8 and namespaced families from Task 9
- Produces: repository-authored YAML using only `meta_pins`,
  `package_options`, explicit `pin_map`, and typed package refs

- [ ] **Step 1: Record the complete old-to-new address table in the migration
commit description.** Merge nominal sizes into real families such as
`r-tht/DIN/{0204,0207,0411,0617,0922}`, `d-tht/DO/{15,35,41,201}`,
`q-tht/TO-92/std`, and `chip-smd/imperial/{0402,...,1206}`. Preserve the old
`typ` geometry exactly and drop `S`/`L` pseudo-variants.
- [ ] **Step 2: Add or update loader smoke tests** that load the entire shipped
catalogue and library, assert representative final addresses, and reject one
old unprefixed address. Verify the test fails before rewriting assets.
- [ ] **Step 3: Rewrite catalogue assets and component libraries once.** Every
component option receives an explicit mapping. Family refs may expand to
several physical sizes only when every expanded choice has the same valid
MetaPin mapping; otherwise declare separate options.
- [ ] **Step 4: Rewrite all repository fixtures and saved test projects.** Do
not add aliases, address rename maps, or compatibility parsing.
- [ ] **Step 5: Verify** the complete loader/package test group and run
`rg -n 'DIN-0207/typ|DO-35/typ|SMD-0805/typ|package_family_ref' library assets tests src`;
only intentional migration-error assertions may remain.
- [ ] **Step 6: Commit** the single migration as
`refactor(catalogue)!: namespace package families and refs`.

### Task 11: Explicit package-option picker tree

**Files:**
- Modify: `src/protoboard/domain/components/package/generated_families.py`
- Modify: `src/protoboard/gui/footprint_choice.py`
- Modify: `src/protoboard/gui/dialogs/footprint_picker_dialog.py`
- Modify: `src/protoboard/gui/panels/properties_panel.py`
- Test: `tests/unit/test_generated_families.py`
- Test: `tests/gui/test_footprint_picker_dialog.py`
- Test: `tests/gui/test_panels.py`

**Interfaces:**
- Produces: picker options derived only from
  `ComponentDefinition.package_options`
- Produces: namespace path metadata for rendering options in a `QTreeWidget`

- [ ] **Step 1: Add failing domain tests** proving that an unrelated package
with the same pin count is never offered, an unmapped package is never offered,
and each offered address corresponds to a declared `PackageOption`.
- [ ] **Step 2: Remove the pin-count catalogue fallback.** Expand only a family
explicitly referenced by an option; copy that option's `pin_map` to the
concrete choice produced for a family variant. Reject an instance selection
that does not resolve back to one of these declared/expanded options.
- [ ] **Step 3: Add failing GUI tests** for nested namespace groups, family
variant leaves, search, keyboard confirmation, recent selections, and a
multi-label MetaPin shown in the detailed UI.
- [ ] **Step 4: Replace the flat picker list with a tree** while retaining the
existing selection and kind-parameter dialog behavior. Group nodes are not
selectable; only concrete options produce an instance address.
- [ ] **Step 5: Verify** the focused domain and GUI tests, then the complete GUI
suite; commit as `refactor(gui): pick declared packages from namespace tree`.

### Task 12: Documentation and final repository verification

**Files:**
- Modify: `docs/footprints/definition-variants.md`
- Modify: `docs/footprints/package-model.md`
- Modify: `docs/footprints/authoring.md`
- Modify: `docs/data-model.md`
- Modify: `docs/gui.md`
- Modify: remaining architecture or example documents found by searches

- [ ] **Step 1: Rewrite documentation** around one runtime concept:
`PackageOption(ref, pin_map)`. Document `package:` as normalization sugar,
the `@/#/$` authoring syntax, namespaced families, explicit picker offers, and
the absence of backward compatibility.
- [ ] **Step 2: Require no legacy production matches**:

```bash
rg -n "PackageFootprint|FootprintOption|ComponentDefinition\.package|pin_names|PinVariant|pin_variant|defn\.pin_count|package_family_ref" src
```

- [ ] **Step 3: Run formatting and strict types**:

```bash
.venv/bin/python -m black --check src tests
.venv/bin/python -m ruff check src tests
.venv/bin/python -m mypy --strict src
.venv/bin/pyright
git diff --check
```

- [ ] **Step 4: Run complete automated verification**:

```bash
.venv/bin/python -m pytest tests/unit -q
.venv/bin/python -m pytest tests/gui -q
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/gui -q
```

- [ ] **Step 5: Run the established GUI smoke workflow**, including selecting
a family variant from the tree, placing it, assigning a net through a MetaPin,
and confirming that every mapped physical pad renders on that net.
- [ ] **Step 6: Commit only after every check passes** as
`docs: finalize metapin and package reference model`.
