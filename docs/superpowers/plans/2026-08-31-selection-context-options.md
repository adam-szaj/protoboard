# Selection Context Options Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce `SelectTool` responsibility by moving pure context-menu option construction into a tested selection module.

**Architecture:** `SelectTool` remains responsible for hit testing, session queries, and command dispatch. A new Qt-free module receives already-resolved identifiers and labels, then returns `ActionOption` values without reading session state. This preserves the existing action ids and payload schema while making the menu construction independently testable.

**Tech Stack:** Python 3.11+, dataclasses, Protocols, pytest, Ruff, Black, mypy, Pyright.

**Spec:** `docs/superpowers/specs/2026-08-30-module-boundaries-and-deduplication-design.md`

## Global Constraints

- Preserve every context-menu action id, label, group, submenu, and payload.
- Keep Qt and GUI imports out of the extracted module.
- Keep candidate/session authorization in `SelectTool`.
- Add focused regression tests before changing production callers.
- Commit each completed task; do not merge the workstream until its final gate.

---

### Task 1: Extract net-selection and highlight option builders

**Files:**
- Create: `src/protoboard/controller/selection/context_options.py`
- Modify: `src/protoboard/controller/tools/select_tool.py`
- Test: `tests/unit/test_selection_context_options.py`

**Interfaces:**
- `build_net_selection_options(net_ids: Sequence[UUID], net_name: Callable[[UUID], str]) -> list[ActionOption]` returns the current select-net options and per-net submenu entries.
- `build_highlight_options(has_persistent: bool) -> list[ActionOption]` returns the two persistent-highlight options.

- [x] **Step 1: Add failing pure-builder tests**

Cover one net, multiple nets, no nets, and both values of `has_persistent`; assert exact action ids and payloads.

- [x] **Step 2: Verify the tests fail**

Run:
```bash
uv run pytest tests/unit/test_selection_context_options.py -q
```
Expected: FAIL because the module and builders do not exist.

- [x] **Step 3: Implement builders and migrate `SelectTool`**

Move only option construction. Keep `SessionConnQuery`, `SessionNetSource`, and highlight-stack access in `SelectTool`; pass resolved net ids/names and the persistence flag to the builders.

- [x] **Step 4: Verify parity and static checks**

Run the new unit tests plus the existing select-tool GUI tests, then Ruff, Black, strict mypy, and Pyright on changed files.

- [x] **Step 5: Commit**

```bash
git add src/protoboard/controller/selection/context_options.py src/protoboard/controller/tools/select_tool.py tests/unit/test_selection_context_options.py
git commit -m "refactor(selection): extract context option builders"
```

### Task 2: Workstream 4 checkpoint

**Files:**
- Modify: `docs/superpowers/plans/2026-08-31-selection-context-options.md`
- Modify: `docs/superpowers/specs/2026-08-30-module-boundaries-and-deduplication-design.md`

- [ ] **Step 1: Run the focused gate and audit the diff**
- [ ] **Step 2: Record source metrics and preserve manual-GUI deferral**
- [ ] **Step 3: Commit the checkpoint documentation**

```bash
git add docs/superpowers/plans/2026-08-31-selection-context-options.md docs/superpowers/specs/2026-08-30-module-boundaries-and-deduplication-design.md
git commit -m "docs(architecture): record selection context decomposition"
```

## Self-Review Checklist

- [ ] Context options are pure and Qt-free.
- [ ] SelectTool still owns session queries and authorization.
- [ ] Existing action ids and payloads are unchanged.
- [ ] Every task has a verified commit and no merge was performed.
