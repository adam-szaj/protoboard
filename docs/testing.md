# Testing strategy

## Test kinds

- **Unit** — `domain/`, `services/`. Pure Python, no Qt, fast.
- **Integration** — end-to-end: build a project programmatically → save → load → DRC.
- **GUI** — `pytest-qt` smoke tests: open the window, click a toolbar, don't crash.
- **Property-based** — `hypothesis` for the layout engine (random box trees produce monotonic atom positions and no unexpected overlaps).
- **Snapshot** — YAML renders of test projects compared against a baseline.
- **Performance** — `pytest-benchmark` in `tests/benchmarks/` — see [benchmarks.md](benchmarks.md).

## Four-layer capability matrix

| Capability | Domain invariant | Application contract / GUI smoke |
|---|---|---|
| Connectivity and DRC | `tests/unit/test_connectivity*.py`, `tests/unit/test_drc*.py` | `tests/gui/test_edit_cli_acceptance.py` |
| Legacy project loading | `tests/unit/test_wire_migration.py` | `tests/unit/test_wire_side_persistence.py` |
| Routing and cancellation | `tests/unit/test_autorouter.py`, `tests/unit/test_hierarchical.py` | `tests/gui/test_autoroute_worker_pipeline.py` |
| Edit and undo/redo | `tests/unit/test_*command*.py` | `tests/gui/test_edit_cli_acceptance.py` |
| GUI actions and settings | domain settings round-trip tests | `tests/gui/test_main_window_shortcuts.py`, `tests/gui/test_preferences_dialog_drag.py` |
| Export artifacts | exporter unit contracts | `tests/gui/test_edit_cli_acceptance.py`, `tests/gui/test_export_*.py` |

## Test quality rules

Prefer assertions on results observable at a module boundary: layout,
schematic, connectivity, DRC issues, persisted files, exported artifacts,
notifications, and undo/redo outcomes. Avoid pinning private fields, widget
hierarchies, cache objects, call counts, or tuning constants unless that detail
is an intentional public contract. The rationale and current migration
inventory are in [testing-audit.md](testing-audit.md).

## Coverage targets

80 % for `domain/` and `services/`. GUI is not counted.

The four layers map to the repository as follows:

| Layer | Scope | Primary command |
|---|---|---|
| Domain invariants | Pure geometry, connectivity, DRC, migration and routing | `uv run pytest tests/unit -q` |
| Application contracts | Headless session, controller, persistence and exports | `uv run pytest tests/gui/test_edit_cli_acceptance.py tests/unit/test_edit_controller_autoroute_drc.py -q` |
| GUI smoke | Public actions and visible user outcomes under offscreen Qt | `QT_QPA_PLATFORM=offscreen uv run pytest tests/gui -q` |
| Compatibility/performance | Legacy fixtures, properties and benchmarks | `uv run pytest tests/property -m property`; `uv run pytest tests/benchmarks --benchmark-only` |

## How to run

```bash
uv run pytest                                            # default suite (sequential, no slow/property)
uv run pytest tests/unit/                                # unit only
uv run pytest tests/benchmarks/ --benchmark-only -m "not property"  # all benchmarks incl. slow
uv run pytest -m slow                                    # the slow benchmark(s) on their own
```

The default run **excludes** `slow` (and `property`) tests — see `addopts` in
`pyproject.toml`. `slow` is currently one benchmark, `test_bench_large_greedy`
(~100 s, large-board greedy autoroute — by far the slowest test in the repo).
Pass `-m slow` to run it; CI runs it as part of the sequential benchmark step.

### Parallel run (fast feedback)

The functional suite is parallel-safe (no cross-test shared state), so on a
multi-core box run it with [`pytest-xdist`](https://pytest-xdist.readthedocs.io)
for a ~6× wall-clock win (≈30 s vs ≈3 min):

```bash
uv run pytest tests/ --ignore=tests/benchmarks -p no:benchmark \
    -m "not serial and not property and not slow" -n auto   # ~30 s, parallel
uv run pytest tests/benchmarks --benchmark-only -m "not property"  # ~2 min, sequential
uv run pytest -m serial                                     # the few wall-time unit tests
```

`-n auto` spreads tests across all cores. **Two things stay out of the parallel
run, both because they measure *wall time*** (unstable under a saturated box):

- `tests/benchmarks` — `pytest-benchmark` timing; run sequentially with
  `--benchmark-only`.
- Tests marked `@pytest.mark.serial` — a handful of unit-level timing asserts
  (e.g. `tests/unit/test_call_counters.py` checking the disabled-`@counted`
  wrapper isn't dramatically slower than a bare call). Under 16 parallel workers the ratio
  blows past its noise headroom; run them with `-m serial`.

Everything else is parallel-safe, including the functional call-counter tests
in `tests/unit/test_call_counters.py`: they assert instrumentation behavior,
not elapsed time, so they're deterministic under distribution. Single-file iteration stays fast without `-n` (worker spawn
outweighs a handful of tests), which is why `-n` is not in the default
`addopts`.
