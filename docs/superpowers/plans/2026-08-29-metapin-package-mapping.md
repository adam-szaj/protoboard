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

Remove `PinVariant`, `pin_names`, `variants`, `pin_count`, `pin_name()`,
`effective_pin_names()`, and label-based grouping. Validate non-empty labels and
positive MetaPin keys. Implement 1-based `meta_pin()` lookup.

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

### Task 7: Remove legacy API and verify repository

**Files:**
- Modify: remaining files found by repository-wide searches
- Modify: remaining tests and authoring documentation

- [ ] **Step 1: Require no production matches**

```bash
rg -n "pin_names|PinVariant|pin_variant|defn\.pin_count|PinRef\([^)]*pin_number" src
```

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

- [ ] **Step 4: Run the established GUI smoke workflow**, update architecture
and authoring documentation, then commit only after every check passes.
