<!-- SPDX-License-Identifier: MIT -->
# Domain Boundary Enforcement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `src/protoboard/domain` independent of `services`, `controller`,
and `gui` by relocating domain policies and neutral instrumentation without
changing runtime behaviour.

**Architecture:** Introduce an AST import-contract test that sees module-scope,
local, and `TYPE_CHECKING` imports. Move cross-cutting diagnostics to a neutral
top-level package and move pad policy, value decoding, routing cost types,
presets, and simplify mode into domain-owned modules. Update every caller in one
step and delete old modules rather than adding compatibility re-exports.

**Tech Stack:** Python 3.11+, pytest, standard-library `ast`, Ruff, Black, mypy,
Pyright, uv

**Spec:** `docs/superpowers/specs/2026-08-30-module-boundaries-and-deduplication-design.md`

## Global Constraints

- Preserve observable application behaviour; this workstream changes ownership
  and imports, not algorithms or data formats.
- The one-third LOC reduction remains aspirational. Do not compress code or add
  reflection to improve the metric.
- Do not add compatibility modules, import aliases at old paths, or legacy YAML
  handling.
- Keep comments that explain algorithmic, geometric, concurrency, undo, or
  persistence invariants. Do not perform unrelated comment cleanup.
- Use characterization tests before each move and keep the old and new module
  from coexisting after a task completes.
- Do not touch unrelated untracked files in the repository root.
- Create commits only when the user has explicitly authorized commits for the
  execution session. When authorized, use the commit boundary at the end of
  each task.

## Baseline

At plan creation, `src/protoboard` contains 298 Python modules and 96,669
physical lines. The domain has imports from:

- `protoboard.services.call_counters` and
  `protoboard.services.debug_trace`;
- `protoboard.services.pad_strategy`;
- `protoboard.gui.renderers.value_codes`;
- `protoboard.services.routing.cost_model`, `cost_presets`, and
  `postprocess.SimplifyMode`.

The workstream is complete only when the generic AST guard reports no domain
import whose root is `protoboard.services`, `protoboard.controller`, or
`protoboard.gui`.

## Explicitly Deferred to Later Workstreams

- Moving the drag detector and replacing opaque drag actions belongs to the
  application-port/context workstream because it changes the
  service-to-controller seam.
- Extracting view DTOs and the shared Qt rendering kernel belongs to the
  application-port and GUI decomposition workstreams.
- Splitting `EditState`, adding canonical component/snap/serialization/MRU
  operations, removing remaining legacy paths, and broad comment cleanup are
  covered by their own plans after this domain boundary is green.
- This plan retains existing `services → gui` and controller-to-concrete-service
  violations as documented debt; it must not weaken their later removal with
  broad allow-lists.

---

### Task 1: Replace the regex boundary check with an AST import contract

**Files:**
- Create: `tests/unit/test_layer_import_contract.py`
- Delete: `tests/unit/test_controller_no_gui_import.py`

**Interfaces:**
- Consumes: Python `ast.Import` and `ast.ImportFrom` nodes.
- Produces: `_assert_no_imports(layer: str, forbidden: tuple[str, ...]) -> None`,
  reused by later tasks, plus the existing controller-to-GUI invariant.

- [ ] **Step 1: Record the baseline commands and confirm the current controller guard passes**

Run:

```bash
uv run pytest tests/unit/test_controller_no_gui_import.py -q
rg --files src/protoboard -g '*.py' | wc -l
rg --files src/protoboard -g '*.py' | xargs wc -l | tail -n 1
```

Expected: the test passes; the counts are `298` modules and `96669 total`
physical lines.

- [ ] **Step 2: Create the AST-based replacement test**

Create `tests/unit/test_layer_import_contract.py` with:

```python
# SPDX-License-Identifier: MIT
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).parents[2] / "src" / "protoboard"


def _imported_modules(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            imports.extend((node.lineno, alias.name) for alias in node.names)
    return imports


def _assert_no_imports(layer: str, forbidden: tuple[str, ...]) -> None:
    offenders: list[str] = []
    for path in sorted((SRC / layer).rglob("*.py")):
        for lineno, module in _imported_modules(path):
            if any(
                module == root or module.startswith(f"{root}.")
                for root in forbidden
            ):
                relative = path.relative_to(SRC)
                offenders.append(f"{relative}:{lineno}: {module}")
    assert not offenders, f"{layer} has forbidden imports:\n" + "\n".join(offenders)


def test_controller_does_not_import_gui() -> None:
    _assert_no_imports("controller", ("protoboard.gui",))
```

This intentionally walks the entire AST, so imports inside functions and
`TYPE_CHECKING` blocks are covered.

- [ ] **Step 3: Run the replacement test**

Run:

```bash
uv run pytest tests/unit/test_layer_import_contract.py -q
```

Expected: PASS.

- [ ] **Step 4: Delete the obsolete regex test and run the replacement again**

Delete `tests/unit/test_controller_no_gui_import.py`, then run:

```bash
uv run pytest tests/unit/test_layer_import_contract.py -q
uv run ruff check tests/unit/test_layer_import_contract.py
uv run black --check tests/unit/test_layer_import_contract.py
```

Expected: all commands pass and the old test file no longer exists.

- [ ] **Step 5: Commit the import-contract foundation when authorized**

```bash
git add tests/unit/test_layer_import_contract.py tests/unit/test_controller_no_gui_import.py
git commit -m "test(architecture): enforce imports through AST"
```

---

### Task 2: Move call counting and tracing to neutral instrumentation

**Files:**
- Create: `src/protoboard/instrumentation/__init__.py`
- Move: `src/protoboard/services/call_counters.py` →
  `src/protoboard/instrumentation/call_counters.py`
- Move: `src/protoboard/services/debug_trace.py` →
  `src/protoboard/instrumentation/debug_trace.py`
- Modify: `tests/unit/test_layer_import_contract.py`
- Modify: `tests/unit/test_call_counters.py`
- Modify: `tests/unit/test_debug_trace.py`
- Modify: every production caller returned by
  `rg -l 'protoboard\.services\.(call_counters|debug_trace)' src --glob '*.py'`

**Interfaces:**
- Consumes: the existing public functions from both instrumentation modules.
- Produces: unchanged APIs at
  `protoboard.instrumentation.call_counters` and
  `protoboard.instrumentation.debug_trace`; the old service paths are deleted.

- [ ] **Step 1: Add a failing domain-instrumentation boundary test**

Append to `tests/unit/test_layer_import_contract.py`:

```python
def test_domain_does_not_import_service_instrumentation() -> None:
    _assert_no_imports(
        "domain",
        (
            "protoboard.services.call_counters",
            "protoboard.services.debug_trace",
        ),
    )
```

- [ ] **Step 2: Verify the new test fails for the known domain callers**

Run:

```bash
uv run pytest tests/unit/test_layer_import_contract.py::test_domain_does_not_import_service_instrumentation -q
```

Expected: FAIL listing `domain/board/board.py`, package loader/shape/reach/solver
modules, and layout placement/group/wire modules.

- [ ] **Step 3: Move the modules without leaving service shims**

Run:

```bash
mkdir -p src/protoboard/instrumentation
git mv src/protoboard/services/call_counters.py src/protoboard/instrumentation/call_counters.py
git mv src/protoboard/services/debug_trace.py src/protoboard/instrumentation/debug_trace.py
```

Create `src/protoboard/instrumentation/__init__.py`:

```python
# SPDX-License-Identifier: MIT
"""Dependency-neutral diagnostics used by every application layer."""
```

Do not re-export the APIs from `__init__.py`; callers import the focused module.

- [ ] **Step 4: Repoint all production and test imports**

Apply these exact mappings everywhere under `src/` and `tests/`:

```python
from protoboard.instrumentation.call_counters import counted
from protoboard.instrumentation.debug_trace import traced
```

Tests importing control functions use the same module paths while retaining
their existing imported names. Update example imports inside both module
docstrings. Verify mechanically:

```bash
rg -n 'protoboard\.services\.(call_counters|debug_trace)' src tests
```

Expected: no output.

- [ ] **Step 5: Run instrumentation and boundary tests**

Run:

```bash
uv run pytest tests/unit/test_call_counters.py tests/unit/test_debug_trace.py tests/unit/test_layer_import_contract.py -q
uv run ruff check src/protoboard/instrumentation tests/unit/test_call_counters.py tests/unit/test_debug_trace.py tests/unit/test_layer_import_contract.py
uv run mypy src/protoboard/instrumentation
```

Expected: all commands pass.

- [ ] **Step 6: Commit the neutral instrumentation move when authorized**

```bash
git add \
  src/protoboard/instrumentation/__init__.py \
  src/protoboard/instrumentation/call_counters.py \
  src/protoboard/instrumentation/debug_trace.py \
  src/protoboard/services/call_counters.py \
  src/protoboard/services/debug_trace.py \
  src/protoboard/services/library/loader.py \
  src/protoboard/controller/tools/base.py \
  src/protoboard/controller/tools/place_tool.py \
  src/protoboard/controller/tools/select_tool.py \
  src/protoboard/controller/tools/wire_edit_tool.py \
  src/protoboard/domain/board/board.py \
  src/protoboard/domain/components/package/loader.py \
  src/protoboard/domain/components/package/reach.py \
  src/protoboard/domain/components/package/shapes.py \
  src/protoboard/domain/components/package/solver.py \
  src/protoboard/domain/layout/group_ownership.py \
  src/protoboard/domain/layout/placement.py \
  src/protoboard/domain/layout/wire_utils.py \
  src/protoboard/gui/board_view.py \
  src/protoboard/gui/board_view_tool.py \
  src/protoboard/gui/highlight/renderer.py \
  src/protoboard/gui/keymap.py \
  src/protoboard/gui/panels/properties_panel.py \
  src/protoboard/gui/panels/selection_filter_widget.py \
  src/protoboard/gui/panels/tree_panel_mixin.py \
  src/protoboard/gui/renderers/packages/base.py \
  src/protoboard/gui/renderers/packages/body_label.py \
  src/protoboard/gui/renderers/packages/canvas.py \
  src/protoboard/gui/renderers/pin_pad_renderer.py \
  src/protoboard/gui/scene_composer.py \
  src/protoboard/gui/tool_host.py \
  src/protoboard/gui/view_camera.py \
  tests/unit/test_call_counters.py \
  tests/unit/test_debug_trace.py \
  tests/unit/test_layer_import_contract.py
git commit -m "refactor(architecture): move instrumentation to neutral layer"
```

---

### Task 3: Move pad policy into domain layout

**Files:**
- Move: `src/protoboard/services/pad_strategy.py` →
  `src/protoboard/domain/layout/pad_strategy.py`
- Modify: `tests/unit/test_layer_import_contract.py`
- Modify: `src/protoboard/domain/commands/component_cmds.py`
- Modify: `src/protoboard/domain/layout/wire_utils.py`
- Modify: `src/protoboard/controller/editing/drag_commit.py`
- Modify: `src/protoboard/controller/editing/headless_session.py`
- Modify: `src/protoboard/services/drag_detector.py`
- Modify: `tests/unit/test_pad_strategy.py`
- Modify: `tests/unit/test_add_wire_endpoint_merge.py`
- Modify: `tests/unit/test_drag_detector.py`
- Modify: `tests/unit/test_drag_detector_classification.py`
- Modify: `tests/unit/test_find_pin_at_pinknot_reuse.py`
- Modify: `tests/unit/test_group_lock_internal_wires.py`

**Interfaces:**
- Consumes: domain `Pad`, `LayerId`, and `NetId` types.
- Produces: unchanged `Decision`, `Verdict`, `StrategyConfig`, `PadStrategy`,
  concrete strategies, and `make_strategy()` at
  `protoboard.domain.layout.pad_strategy`.

- [ ] **Step 1: Add a failing pad-policy boundary test**

Append:

```python
def test_domain_does_not_import_service_pad_strategy() -> None:
    _assert_no_imports("domain", ("protoboard.services.pad_strategy",))
```

- [ ] **Step 2: Verify the test fails before the move**

Run:

```bash
uv run pytest tests/unit/test_layer_import_contract.py::test_domain_does_not_import_service_pad_strategy -q
```

Expected: FAIL listing imports in `component_cmds.py` and `wire_utils.py`.

- [ ] **Step 3: Move the module and repoint every caller**

Run:

```bash
git mv src/protoboard/services/pad_strategy.py src/protoboard/domain/layout/pad_strategy.py
```

Replace every old import with the same symbols from:

```python
from protoboard.domain.layout.pad_strategy import (
    AutorouterPermissiveStrategy,
    Decision,
    DRCEnforcingStrategy,
    PadStrategy,
    SimplifyStrictStrategy,
    StrategyConfig,
    Verdict,
    make_strategy,
)
```

Each caller keeps only the names it already uses. Do not create
`services/pad_strategy.py` as a forwarding module.

- [ ] **Step 4: Verify the old path is gone and behaviour is unchanged**

Run:

```bash
test ! -e src/protoboard/services/pad_strategy.py
rg -n 'protoboard\.services\.pad_strategy' src tests
uv run pytest tests/unit/test_pad_strategy.py tests/unit/test_add_wire_endpoint_merge.py tests/unit/test_drag_detector.py tests/unit/test_drag_detector_classification.py tests/unit/test_find_pin_at_pinknot_reuse.py tests/unit/test_group_lock_internal_wires.py tests/unit/test_layer_import_contract.py -q
```

Expected: both searches produce no old module, and all tests pass.

- [ ] **Step 5: Run static checks for the moved policy and affected domain modules**

Run:

```bash
uv run ruff check src/protoboard/domain/layout/pad_strategy.py src/protoboard/domain/commands/component_cmds.py src/protoboard/domain/layout/wire_utils.py
uv run mypy src/protoboard/domain/layout/pad_strategy.py src/protoboard/domain/commands/component_cmds.py src/protoboard/domain/layout/wire_utils.py
```

Expected: both commands pass.

- [ ] **Step 6: Commit the pad-policy move when authorized**

```bash
git add \
  src/protoboard/services/pad_strategy.py \
  src/protoboard/domain/layout/pad_strategy.py \
  src/protoboard/domain/commands/component_cmds.py \
  src/protoboard/domain/layout/wire_utils.py \
  src/protoboard/controller/editing/drag_commit.py \
  src/protoboard/controller/editing/headless_session.py \
  src/protoboard/services/drag_detector.py \
  tests/unit/test_pad_strategy.py \
  tests/unit/test_add_wire_endpoint_merge.py \
  tests/unit/test_drag_detector.py \
  tests/unit/test_drag_detector_classification.py \
  tests/unit/test_find_pin_at_pinknot_reuse.py \
  tests/unit/test_group_lock_internal_wires.py \
  tests/unit/test_layer_import_contract.py
git commit -m "refactor(domain): own pad strategy policy"
```

---

### Task 4: Move component value decoding out of GUI

**Files:**
- Move: `src/protoboard/gui/renderers/value_codes.py` →
  `src/protoboard/domain/components/value_codes.py`
- Modify: `src/protoboard/domain/components/value_text.py`
- Modify: `library/renderers/resistor_bands.py`
- Modify: `tests/unit/test_value_codes.py`
- Modify: `tests/unit/test_layer_import_contract.py`

**Interfaces:**
- Consumes: `ComponentClass` and plain value strings.
- Produces: unchanged parsing and code functions at
  `protoboard.domain.components.value_codes`.

- [ ] **Step 1: Add and run the failing GUI-value-code boundary test**

Append:

```python
def test_domain_does_not_import_gui_value_codes() -> None:
    _assert_no_imports("domain", ("protoboard.gui.renderers.value_codes",))
```

Run:

```bash
uv run pytest tests/unit/test_layer_import_contract.py::test_domain_does_not_import_gui_value_codes -q
```

Expected: FAIL listing `domain/components/value_text.py`.

- [ ] **Step 2: Move the pure parser and update all three consumers**

Run:

```bash
git mv src/protoboard/gui/renderers/value_codes.py src/protoboard/domain/components/value_codes.py
```

Use this import path in `value_text.py`, the resistor-band plugin, and its unit
test:

```python
from protoboard.domain.components.value_codes import (
    code_digits,
    parse_capacitance,
    parse_inductance,
    parse_resistance,
    parse_tolerance,
    parse_value,
    smd_code,
)
```

Each file keeps only the functions it uses. The old GUI module is deleted and
is not replaced by a re-export.

- [ ] **Step 3: Run parser, plugin-loading, and boundary tests**

Run:

```bash
rg -n 'protoboard\.gui\.renderers\.value_codes' src tests library
QT_QPA_PLATFORM=offscreen uv run pytest tests/unit/test_value_codes.py tests/unit/test_layer_import_contract.py tests/gui/test_value_code_renderers.py -q
```

Expected: the search has no output and all tests pass, including discovery and
rendering of the resistor-band plugin.

- [ ] **Step 4: Run static checks**

Run:

```bash
uv run ruff check src/protoboard/domain/components/value_codes.py src/protoboard/domain/components/value_text.py library/renderers/resistor_bands.py tests/unit/test_value_codes.py
uv run mypy src/protoboard/domain/components/value_codes.py src/protoboard/domain/components/value_text.py
```

Expected: both commands pass.

- [ ] **Step 5: Commit the value-decoder move when authorized**

```bash
git add src/protoboard/gui/renderers/value_codes.py src/protoboard/domain/components/value_codes.py src/protoboard/domain/components/value_text.py library/renderers/resistor_bands.py tests/unit/test_value_codes.py tests/unit/test_layer_import_contract.py
git commit -m "refactor(domain): own component value decoding"
```

---

### Task 5: Separate routing configuration types from routing implementations

**Files:**
- Create: `src/protoboard/domain/routing/__init__.py`
- Move: `src/protoboard/services/routing/cost_model.py` →
  `src/protoboard/domain/routing/cost_model.py`
- Move: `src/protoboard/services/routing/cost_presets.py` →
  `src/protoboard/domain/routing/cost_presets.py`
- Create: `src/protoboard/domain/routing/simplify_mode.py`
- Create: `src/protoboard/services/routing/cost_metadata.py`
- Modify: `src/protoboard/services/routing/postprocess.py`
- Modify: `src/protoboard/services/routing/protocol.py`
- Modify: `src/protoboard/domain/routing_settings.py`
- Modify: every file returned by the three import searches in Step 6.
- Modify: `tests/unit/test_routing_settings.py`
- Modify: `tests/unit/test_cost_presets.py`
- Modify: `tests/unit/test_layer_import_contract.py`

**Interfaces:**
- Consumes: mappings of cost weights and `Side`.
- Produces:
  `RoutingCostModel` at `protoboard.domain.routing.cost_model`,
  `resolve_weights()` and built-in preset queries at
  `protoboard.domain.routing.cost_presets`, `SimplifyMode` at
  `protoboard.domain.routing.simplify_mode`, and plugin-aware merged metadata at
  `protoboard.services.routing.cost_metadata`.

- [ ] **Step 1: Add a failing routing-layer boundary test**

Append:

```python
def test_domain_does_not_import_service_routing() -> None:
    _assert_no_imports("domain", ("protoboard.services.routing",))
```

- [ ] **Step 2: Verify it reports all routing imports in the domain**

Run:

```bash
uv run pytest tests/unit/test_layer_import_contract.py::test_domain_does_not_import_service_routing -q
```

Expected: FAIL listing `domain/routing_settings.py` imports of `cost_model`,
`cost_presets`, and `postprocess`.

- [ ] **Step 3: Move pure cost modules into a new domain routing package**

Run:

```bash
mkdir -p src/protoboard/domain/routing
git mv src/protoboard/services/routing/cost_model.py src/protoboard/domain/routing/cost_model.py
git mv src/protoboard/services/routing/cost_presets.py src/protoboard/domain/routing/cost_presets.py
```

Create `src/protoboard/domain/routing/__init__.py`:

```python
# SPDX-License-Identifier: MIT
"""Routing configuration value objects and implementation-neutral policy."""
```

Do not re-export classes from the package initializer.

- [ ] **Step 4: Extract plugin-aware metadata back into services**

Remove `merged_cost_key_docs()` and `merged_user_tunable_cost_keys()` from the
moved domain `cost_model.py`. Create `services/routing/cost_metadata.py` with
their existing behaviour and these imports:

```python
# SPDX-License-Identifier: MIT
"""Merge built-in routing cost metadata with discovered router plugins."""

from protoboard.domain.routing.cost_model import (
    COST_KEY_DOCS,
    USER_TUNABLE_COST_KEYS,
)
from protoboard.services.routing.protocol import registry


def merged_cost_key_docs() -> dict[str, str]:
    merged: dict[str, str] = {}
    for name in registry.list_available():
        try:
            extra = getattr(registry.get(name), "cost_key_docs", lambda: {})()
            if isinstance(extra, dict):
                merged.update(
                    {str(key): str(value) for key, value in extra.items() if isinstance(key, str)}
                )
        except Exception:  # noqa: BLE001
            continue
    merged.update(COST_KEY_DOCS)
    return merged


def merged_user_tunable_cost_keys() -> frozenset[str]:
    keys = set(USER_TUNABLE_COST_KEYS)
    for name in registry.list_available():
        try:
            extra = getattr(
                registry.get(name), "user_tunable_cost_keys", lambda: frozenset()
            )()
            if isinstance(extra, (set, frozenset)):
                keys.update(str(key) for key in extra if isinstance(key, str))
        except Exception:  # noqa: BLE001
            continue
    return frozenset(keys)
```

Update `gui/dialogs/cost_presets_dialog.py` and the local imports in
`gui/dialogs/preferences_dialog.py` to import merged helpers from
`services.routing.cost_metadata`. They import `RoutingCostModel`,
`COST_KEY_DOCS`, or `USER_TUNABLE_COST_KEYS` directly from the domain module
when those base values are needed. Update the plugin-metadata comment in
`services/routing/protocol.py` to point to `cost_metadata`, not the deleted
service `cost_model` module.

- [ ] **Step 5: Move `SimplifyMode` into its implementation-neutral module**

Create `src/protoboard/domain/routing/simplify_mode.py`:

```python
# SPDX-License-Identifier: MIT
"""User-selectable wire-simplification policy."""

from __future__ import annotations

from enum import StrEnum


class SimplifyMode(StrEnum):
    """How aggressively to shorten a wire path.

    CHEBYSHEV replaces a subpath with the shortest grid-aligned path and may
    ignore the bare-diagonal parity rule. BARE_PARITY applies the same policy
    to insulated wires, but preserves the source pad's parity family for each
    new bare diagonal. The default therefore retains the anti-short invariant
    required by bare-diagonal routing.
    """

    CHEBYSHEV = "chebyshev"
    BARE_PARITY = "bare_parity"
```

Remove the enum definition and `StrEnum` import from `postprocess.py`, then
import it there from the domain module. Repoint all existing callers to the new
domain path; do not preserve an old-path facade.

- [ ] **Step 6: Repoint cost-model and preset imports across the repository**

Use these searches as the authoritative caller lists:

```bash
rg -l 'protoboard\.services\.routing\.cost_model' src tests library --glob '*.py'
rg -l 'protoboard\.services\.routing\.cost_presets' src tests library --glob '*.py'
rg -l 'from protoboard\.services\.routing\.postprocess import SimplifyMode' src tests library --glob '*.py'
```

Map imports as follows:

```python
from protoboard.domain.routing.cost_model import RoutingCostModel
from protoboard.domain.routing.cost_presets import resolve_weights
from protoboard.domain.routing.simplify_mode import SimplifyMode
```

`domain/routing_settings.py` keeps its public `cost_model()` method unchanged,
but all three of its imported types/functions now come from domain modules.

- [ ] **Step 7: Add an ownership assertion to routing settings tests**

Add to `tests/unit/test_routing_settings.py`:

```python
def test_routing_settings_use_domain_owned_types() -> None:
    from protoboard.domain.routing.cost_model import RoutingCostModel
    from protoboard.domain.routing.simplify_mode import SimplifyMode

    settings = RoutingSettings()
    assert isinstance(settings.cost_model(), RoutingCostModel)
    assert settings.simplify_mode is SimplifyMode.BARE_PARITY
```

- [ ] **Step 8: Run routing behaviour and import-contract tests**

Run:

```bash
rg -n 'protoboard\.services\.routing\.(cost_model|cost_presets)' src tests library
rg -n 'from protoboard\.services\.routing\.postprocess import SimplifyMode' src tests library
uv run pytest tests/unit/test_routing_settings.py tests/unit/test_cost_presets.py tests/unit/test_routing_postprocess.py tests/unit/test_simplify_optimizer.py tests/unit/test_layer_import_contract.py -q
```

Expected: both searches have no output and all tests pass.

- [ ] **Step 9: Run static checks for the routing split**

Run:

```bash
uv run ruff check src/protoboard/domain/routing src/protoboard/domain/routing_settings.py src/protoboard/services/routing/cost_metadata.py src/protoboard/services/routing/postprocess.py
uv run mypy src/protoboard/domain/routing src/protoboard/domain/routing_settings.py src/protoboard/services/routing/cost_metadata.py src/protoboard/services/routing/postprocess.py
```

Expected: both commands pass.

- [ ] **Step 10: Commit the routing ownership split when authorized**

```bash
git add \
  src/protoboard/domain/routing/__init__.py \
  src/protoboard/domain/routing/cost_model.py \
  src/protoboard/domain/routing/cost_presets.py \
  src/protoboard/domain/routing/simplify_mode.py \
  src/protoboard/domain/routing_settings.py \
  src/protoboard/services/routing/cost_model.py \
  src/protoboard/services/routing/cost_presets.py \
  src/protoboard/services/routing/cost_metadata.py \
  src/protoboard/services/routing/postprocess.py \
  src/protoboard/services/routing/protocol.py \
  src/protoboard/services/routing/astar.py \
  src/protoboard/services/routing/autorouter.py \
  src/protoboard/services/routing/cost_analyzer.py \
  src/protoboard/services/routing/hierarchical.py \
  src/protoboard/services/persistence/project_yaml.py \
  src/protoboard/controller/app_settings.py \
  src/protoboard/controller/editing/placement_conflict.py \
  src/protoboard/controller/selection/wire_edit.py \
  src/protoboard/controller/tools/route_tool.py \
  src/protoboard/controller/tools/wire_edit_tool.py \
  src/protoboard/gui/dialogs/autoroute_dialog.py \
  src/protoboard/gui/dialogs/cost_presets_dialog.py \
  src/protoboard/gui/dialogs/preferences_dialog.py \
  src/protoboard/gui/panels/net_inspector_panel.py \
  tests/gui/test_armed_subtool_lifecycle.py \
  tests/unit/test_astar_parity.py \
  tests/unit/test_astar_rust_parity.py \
  tests/unit/test_astar_segment_crossing.py \
  tests/unit/test_at_pad_penalty.py \
  tests/unit/test_cost_analyzer.py \
  tests/unit/test_cost_presets.py \
  tests/unit/test_deferred_via.py \
  tests/unit/test_layer_import_contract.py \
  tests/unit/test_mid_cell_reroute.py \
  tests/unit/test_parallel_overlap.py \
  tests/unit/test_routing.py \
  tests/unit/test_routing_settings.py \
  tests/unit/test_same_net_reuse.py \
  tests/unit/test_simplify_optimizer.py \
  tests/unit/test_wire_edit_arm.py \
  tests/unit/test_wire_edit_helpers.py
git commit -m "refactor(routing): move configuration policy into domain"
```

---

### Task 6: Enforce the complete domain boundary and update architecture evidence

**Files:**
- Modify: `tests/unit/test_layer_import_contract.py`
- Modify: `docs/architecture/modules.md`

**Interfaces:**
- Consumes: the completed module moves from Tasks 2–5.
- Produces: one generic domain import rule with zero debt exceptions and updated
  architecture documentation.

- [ ] **Step 1: Replace narrow migration tests with the final domain rule**

Remove these temporary tests from `test_layer_import_contract.py`:

```text
test_domain_does_not_import_service_instrumentation
test_domain_does_not_import_service_pad_strategy
test_domain_does_not_import_gui_value_codes
test_domain_does_not_import_service_routing
```

Replace them with:

```python
def test_domain_does_not_import_outer_layers() -> None:
    _assert_no_imports(
        "domain",
        (
            "protoboard.controller",
            "protoboard.services",
            "protoboard.gui",
        ),
    )


def test_instrumentation_is_dependency_neutral() -> None:
    _assert_no_imports(
        "instrumentation",
        (
            "protoboard.domain",
            "protoboard.controller",
            "protoboard.services",
            "protoboard.gui",
        ),
    )
```

- [ ] **Step 2: Run the final import contract and inspect any offender directly**

Run:

```bash
uv run pytest tests/unit/test_layer_import_contract.py -q
rg -n '^\s*(from|import) protoboard\.(services|controller|gui)' src/protoboard/domain --glob '*.py'
```

Expected: tests pass and the search prints no lines. Do not add exceptions to
make an unexpected offender pass; move or invert the dependency.

- [ ] **Step 3: Update the architecture document to the new authoritative state**

In `docs/architecture/modules.md`:

- add neutral `instrumentation/{call_counters,debug_trace}` to the diagram;
- remove the stale statement that `debug_trace` imports Qt;
- show domain ownership of pad strategy, value codes, routing cost model,
  presets, and simplify mode;
- state that `test_layer_import_contract.py` covers local and
  `TYPE_CHECKING` imports through AST;
- retain still-open `services → gui` and concrete service/controller edges as
  open work for the next plans rather than claiming the entire repository is
  clean.

Use this wording for the domain result:

```markdown
`domain/` has no imports from `controller`, `services`, or `gui`. The rule is
enforced from the parsed AST, including deferred and `TYPE_CHECKING` imports.
```

- [ ] **Step 4: Run the relevant complete verification suite**

Run:

```bash
uv run pytest tests/unit -q
QT_QPA_PLATFORM=offscreen uv run pytest tests/gui -q
uv run ruff check src tests library
uv run black --check src tests library
uv run mypy src
uv run pyright
git diff --check
```

Expected: every command exits zero. If a pre-existing failure is reproducible
on the unchanged base commit, record it explicitly and do not attribute it to
this workstream; otherwise fix regressions before continuing.

- [ ] **Step 5: Measure the workstream delta**

Run:

```bash
rg --files src/protoboard -g '*.py' | wc -l
rg --files src/protoboard -g '*.py' | xargs wc -l | tail -n 1
git diff --stat
```

Expected: the domain forbidden-import count is zero. Record module and LOC
deltas in the execution handoff; no minimum LOC reduction is required for this
boundary-only workstream.

- [ ] **Step 6: Commit the completed domain-boundary workstream when authorized**

```bash
git add tests/unit/test_layer_import_contract.py docs/architecture/modules.md
git commit -m "docs(architecture): record enforced domain boundary"
```

## Workstream Exit Criteria

- `domain` imports none of `controller`, `services`, or `gui`, including local
  and type-checking imports.
- Instrumentation imports no application layer.
- Old instrumentation, pad-strategy, GUI value-code, service cost-model, service
  cost-preset, and postprocess enum import paths have no callers or forwarding
  modules.
- Focused behaviour tests, full unit and GUI suites, Ruff, Black, mypy, Pyright,
  and `git diff --check` pass.
- `docs/architecture/modules.md` describes both the completed domain boundary
  and the remaining higher-layer violations accurately.
