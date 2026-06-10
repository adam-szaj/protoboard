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
uv run pytest                                         # full suite
uv run pytest tests/unit/                             # unit only
uv run pytest tests/benchmarks/ --benchmark-only -v   # benchmarks
```
