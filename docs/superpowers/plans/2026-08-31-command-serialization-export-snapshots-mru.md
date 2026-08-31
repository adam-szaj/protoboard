# Command Serialization, Export Snapshots, and MRU Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consolidate command codecs, export snapshot construction, and recent-value persistence without changing recorded command or user-visible export behaviour.

**Architecture:** A typed `Codec[T]` record owns one command name and its encode/decode pair; the recording dispatcher indexes those records by command type and serialized name. A single `build_snapshot` function creates the immutable export input used by SVG, PDF, and PNG wrappers. A dependency-free generic `RecentStore[T]` owns JSON validation, ordering, deduplication, truncation, and atomic writes; footprint, kind-variant, and project histories provide typed adapters.

**Tech Stack:** Python 3.11+, dataclasses, typing generics, JSON, pytest, PySide6 export adapters.

**Spec:** `docs/superpowers/specs/2026-08-30-module-boundaries-and-deduplication-design.md`

## Global Constraints

- Preserve command recording/replay schema and observable export output.
- Keep explicit command codecs; do not use unrestricted dataclass reflection.
- Keep export snapshot state immutable and independent of GUI viewport state.
- Keep MRU persistence outside the domain layer and silently tolerate corrupt or unavailable history files.
- Use TDD and commit each completed task separately; do not merge until this workstream and its gate are complete.
- Do not perform interactive GUI smoke testing until the parent refactoring goal's final gate.

---

### Task 1: Replace parallel command registries with typed codecs

**Files:**
- Modify: `src/protoboard/services/recording/serialization.py`
- Test: `tests/unit/test_command_recording.py`

**Interfaces:**
- Produces `Codec[T]` with `name`, `encode`, and `decode` fields.
- `serialize_command` and `deserialize_command` keep their existing signatures and error types.
- The registry stores one `Codec[Any]` per command type and serialized name; no parallel serializer/deserializer maps remain.

- [x] **Step 1: Add a failing registry contract test**

Add a test that imports the typed codec registry inspection helper and asserts a registered command resolves to one codec whose name, encoder, and decoder are callable. Keep existing round-trip tests as behavioural coverage.

- [x] **Step 2: Verify the contract fails**

Run:
```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_command_recording.py -q
```
Expected: FAIL because the typed codec record/inspection helper does not exist.

- [x] **Step 3: Implement the minimal typed registry**

Define `Codec[T]` and replace `_TYPE_TO_NAME`, `_TYPE_TO_SERIALIZER`, and `_NAME_TO_DESERIALIZER` with `_CODEC_BY_TYPE` and `_CODEC_BY_NAME`. Adapt every existing registration to construct one codec record; keep all explicit field codecs and command-specific functions unchanged. Add a read-only `codec_for_command_type(command_type: type[Command[Any]]) -> Codec[Any] | None` helper for the contract test.

- [x] **Step 4: Verify command replay parity and static checks**

Run:
```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_command_recording.py -q
/home/yvdev/.local/venv/bin/uv run ruff check src/protoboard/services/recording/serialization.py tests/unit/test_command_recording.py
/home/yvdev/.local/venv/bin/uv run mypy --strict src/protoboard/services/recording/serialization.py
```
Expected: all existing round trips and the new registry contract pass.

- [x] **Step 5: Commit**

```bash
git add src/protoboard/services/recording/serialization.py tests/unit/test_command_recording.py docs/superpowers/plans/2026-08-31-command-serialization-export-snapshots-mru.md
git commit -m "refactor(recording): use typed command codecs"
```

### Task 2: Centralize export snapshot construction

**Files:**
- Modify: `src/protoboard/services/export/snapshot.py`
- Modify: `src/protoboard/services/export/svg.py`
- Modify: `src/protoboard/services/export/pdf.py`
- Modify: `src/protoboard/services/export/png.py`
- Test: `tests/unit/test_export.py`
- Test: `tests/gui/test_export_paint_smoke.py`

**Interfaces:**
- Produces `build_snapshot(board, layout, components, instances, schematic=None, theme=DEFAULT_THEME, package_families=None) -> BoardSnapshot`.
- Public `export_svg`, `export_pdf`, and `export_png` signatures remain unchanged and delegate snapshot assembly to the builder.
- `*_from_snapshot` functions remain the only snapshot-consuming rendering entry points.

- [ ] **Step 1: Add a failing builder-parity test**

Build a minimal board and assert `build_snapshot` supplies the same schematic fallback, `PadIndex`, package-family map, theme, and canonical view fields currently assembled independently by each exporter.

- [ ] **Step 2: Verify the test fails**

Run:
```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_export.py -q
```
Expected: FAIL because the shared builder does not exist.

- [ ] **Step 3: Implement and migrate the builder**

Move the repeated `Schematic`/`PadIndex`/package-family/snapshot assembly into `build_snapshot`. Replace the three wrapper copies with calls to it; preserve scale and raster-pitch handling in their respective adapters.

- [ ] **Step 4: Verify export parity and static checks**

Run:
```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_export.py tests/gui/test_export_paint_smoke.py tests/gui/test_export_svg.py tests/gui/test_export_pdf.py -q
/home/yvdev/.local/venv/bin/uv run ruff check src/protoboard/services/export tests/unit/test_export.py
/home/yvdev/.local/venv/bin/uv run mypy --strict src/protoboard/services/export/snapshot.py src/protoboard/services/export/svg.py src/protoboard/services/export/pdf.py src/protoboard/services/export/png.py
```
Expected: existing SVG/PDF/PNG outputs and the builder test pass.

- [ ] **Step 5: Commit**

```bash
git add src/protoboard/services/export/snapshot.py src/protoboard/services/export/svg.py src/protoboard/services/export/pdf.py src/protoboard/services/export/png.py tests/unit/test_export.py tests/gui/test_export_paint_smoke.py
git commit -m "refactor(export): centralize snapshot construction"
```

### Task 3: Introduce the generic MRU store

**Files:**
- Create: `src/protoboard/services/persistence/recent_store.py`
- Modify: `src/protoboard/gui/recent_footprints.py`
- Modify: `src/protoboard/gui/recent_kind_variants.py`
- Modify: `src/protoboard/gui/recent_projects.py`
- Test: `tests/unit/test_recent_store.py`
- Test: `tests/unit/test_recent_footprints.py`
- Test: `tests/gui/test_footprint_picker_dialog.py`

**Interfaces:**
- Produces `RecentStore[T](path, max_entries, decode, encode, key)` with `load() -> list[T]` and `add(value: T) -> None`.
- `decode` returns `None` for invalid JSON items; `encode` returns a JSON-safe value; `key` identifies duplicates.
- Writes use a same-directory temporary file followed by `os.replace`; all read/write failures return an empty history or leave the previous file intact.
- Existing module-level `load`/`add` functions and dataclasses remain available to GUI callers.

- [ ] **Step 1: Add failing store tests**

Cover malformed root JSON, invalid entries, stable most-recent-first ordering, key-based deduplication, max-entry truncation, non-serializable values, and preserving the old file when an atomic write fails.

- [ ] **Step 2: Verify the tests fail**

Run:
```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_recent_store.py -q
```
Expected: FAIL because `RecentStore` does not exist.

- [ ] **Step 3: Implement and migrate adapters**

Implement `RecentStore` without GUI imports. Adapt footprint, kind-variant, and project modules through typed encode/decode/key functions created from their existing dataclasses and path rules. Keep module constants dynamic so existing tests that monkeypatch configuration paths remain valid; retain project-path existence filtering after generic loading.

- [ ] **Step 4: Verify MRU behaviour and static checks**

Run:
```bash
/home/yvdev/.local/venv/bin/uv run pytest tests/unit/test_recent_store.py tests/unit/test_recent_footprints.py tests/gui/test_footprint_picker_dialog.py -q
/home/yvdev/.local/venv/bin/uv run ruff check src/protoboard/services/persistence/recent_store.py src/protoboard/gui/recent_footprints.py src/protoboard/gui/recent_kind_variants.py src/protoboard/gui/recent_projects.py tests/unit/test_recent_store.py
/home/yvdev/.local/venv/bin/uv run mypy --strict src/protoboard/services/persistence/recent_store.py src/protoboard/gui/recent_footprints.py src/protoboard/gui/recent_kind_variants.py src/protoboard/gui/recent_projects.py
```
Expected: all old and new MRU tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/protoboard/services/persistence/recent_store.py src/protoboard/gui/recent_footprints.py src/protoboard/gui/recent_kind_variants.py src/protoboard/gui/recent_projects.py tests/unit/test_recent_store.py tests/unit/test_recent_footprints.py tests/gui/test_footprint_picker_dialog.py
git commit -m "refactor(persistence): centralize recent-value storage"
```

### Task 4: Workstream 3B verification and status record

**Files:**
- Modify: `docs/superpowers/plans/2026-08-31-command-serialization-export-snapshots-mru.md`
- Modify: `docs/superpowers/specs/2026-08-30-module-boundaries-and-deduplication-design.md`

- [ ] **Step 1: Run the relevant automated gate**

Run the focused tests from Tasks 1–3, the full unit suite, the offscreen GUI suite, Ruff, Black, strict mypy, Pyright, and `git diff --check`. Run the marked property suite only if production changes affect its inputs.

- [ ] **Step 2: Audit superseded paths**

Verify there is one command codec registry, one snapshot builder used by all file exporters, and one MRU persistence implementation. Record any intentionally retained public wrappers.

- [ ] **Step 3: Update design and plan status**

Mark this plan complete, record test totals/LOC metrics, and identify Workstream 3C (snapping, tool execution, and action metadata) as next. Interactive GUI smoke testing remains deferred.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/plans/2026-08-31-command-serialization-export-snapshots-mru.md docs/superpowers/specs/2026-08-30-module-boundaries-and-deduplication-design.md
git commit -m "docs(architecture): complete serialization export and MRU"
```

## Self-Review Checklist

- [ ] Explicit command codecs remain visible and replay schema is unchanged.
- [ ] All three file exporters share one snapshot builder.
- [ ] MRU validation, deduplication, truncation, and atomic writing have one implementation.
- [ ] Domain modules do not import persistence or GUI MRU code.
- [ ] Every task has its own verified commit and no merge was performed.
