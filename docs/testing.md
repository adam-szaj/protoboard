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
uv run pytest                                         # full suite (sequential)
uv run pytest tests/unit/                             # unit only
uv run pytest tests/benchmarks/ --benchmark-only -v   # benchmarks
```

### Parallel run (fast feedback)

The functional suite is parallel-safe (no cross-test shared state), so on a
multi-core box run it with [`pytest-xdist`](https://pytest-xdist.readthedocs.io)
for a ~6× wall-clock win (≈30 s vs ≈3 min):

```bash
uv run pytest tests/ --ignore=tests/benchmarks -p no:benchmark -n auto  # ~30 s, parallel
uv run pytest tests/benchmarks --benchmark-only                         # ~2 min, sequential
```

`-n auto` spreads tests across all cores. **Only `tests/benchmarks` is excluded
from the parallel run** — those use `pytest-benchmark` to measure *wall time*,
which needs a quiet single process to be stable. Everything else is
parallel-safe, including the call-count regression tests in
`tests/unit/test_perf_regressions.py`: they assert *number of calls* (via the
`@counted` framework), not elapsed time, so they're deterministic under
distribution. Single-file iteration stays fast without `-n` (worker spawn
outweighs a handful of tests), which is why `-n` is not in the default
`addopts`.
