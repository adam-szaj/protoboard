# Testing strategy

## Test kinds

- **Unit** — `domain/`, `services/`. Pure Python, no Qt, fast.
- **Integration** — end-to-end: build a project programmatically → save → load → DRC.
- **GUI** — `pytest-qt` smoke tests: open the window, click a toolbar, don't crash.
- **Property-based** — `hypothesis` for the layout engine (random box trees produce monotonic atom positions and no unexpected overlaps).
- **Snapshot** — YAML renders of test projects compared against a baseline.
- **Performance** — `pytest-benchmark` in `tests/benchmarks/` — see [benchmarks.md](benchmarks.md).

## Coverage targets

80 % for `domain/` and `services/`. GUI is not counted.

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
  (e.g. `test_call_counters` checking the disabled-`@counted` wrapper isn't
  dramatically slower than a bare call). Under 16 parallel workers the ratio
  blows past its noise headroom; run them with `-m serial`.

Everything else is parallel-safe, including the call-count regression tests in
`tests/unit/test_perf_regressions.py`: they assert *number of calls* (via the
`@counted` framework), not elapsed time, so they're deterministic under
distribution. Single-file iteration stays fast without `-n` (worker spawn
outweighs a handful of tests), which is why `-n` is not in the default
`addopts`.
