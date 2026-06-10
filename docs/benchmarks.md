# Automated benchmarks

The legacy `m4/` reference project was retired in 2026-06-09 after its
fixtures became unloadable. The benchmark suite now uses a five-fixture
**synthetic generator** built in-memory at test time:

| Fixture | Size |
|---|---|
| `SMALL` | tens of components, single-sided perfboard |
| `MEDIUM` | ~100 components, mixed top/bottom routing |
| `LARGE` | hundreds of components, dense routing |
| `PLATED_THROUGH` | dual-sided board, every pad plated-through |
| `STRIPBOARD_MIXED` | stripboard with selective cuts and a few perfboard inserts |

Generation lives in `tests/benchmarks/fixtures.py` — each fixture is
routable-by-construction so a regression in the autorouter doesn't make the
benchmark crash before it can report a slowdown.

## How to run

```bash
uv run pytest tests/benchmarks/ --benchmark-only -v

# Save a new baseline after a deliberate optimisation:
uv run pytest tests/benchmarks/ --benchmark-only --benchmark-save=baseline

# Verify against the baseline (fail on >20 % slowdown):
uv run pytest tests/benchmarks/ --benchmark-only \
    --benchmark-compare=baseline \
    --benchmark-compare-fail=mean:20%
```

The baseline lives in `.benchmarks/Linux-CPython-3.14-64bit/0001_baseline.json`.

## Hot paths covered

| Test | Expected order | What it measures |
|---|---|---|
| `test_bench_hit_test_component` | < 50 µs | Spatial index — click → component |
| `test_bench_hit_test_pin_for_comp` | < 20 µs | Scan pins of one component |
| `test_bench_hit_test_wire` | < 200 µs | Spatial index — click → wire |
| `test_bench_set_highlighted_net` | < 5 ms | `setOpacity` on existing groups, no rebuild |
| `test_bench_spatial_index_build` | < 10 ms | Rebuild after `layout_changed` |
| `test_bench_connectivity_graph_build` | < 200 ms | Cold rebuild |
| `test_bench_refresh_warm_nothing_changed` | < 150 ms | Warm refresh, groups reused |
| `test_bench_refresh_warm_one_comp_moved` | < 200 ms | Warm refresh, one group rebuilt |

## Live profiling

**Py-Spy** — attach to a running process, does not block the Qt event loop:

```bash
uv run python -m protoboard &
py-spy record -o profile.svg --pid $(pgrep -f protoboard) --duration 30
py-spy top --pid $(pgrep -f protoboard)
```

**cProfile + SnakeViz:**

```bash
uv run python -m cProfile -o profile.out -m protoboard
uv run snakeviz profile.out
```

## GUI performance architecture

The optimisations below must survive any refactor:

**Spatial index** (`session.comp_spatial_index()`,
`session.wire_spatial_index()`): rebuilt lazily after `layout_changed`.
`_hit_test()` only scans the ±1 buckets around the cursor.

**Group reuse** (`update_component_groups`, `update_wire_groups`): one
`QGraphicsItemGroup` per component / wire survives across refreshes. The
fingerprint is `(position, rotation, net, layer)`. Highlight changes use
`setOpacity()` on the existing groups — O(N), zero new items.

**Back-cache and Z-banding**: items with Z < 3.0 are part of the bottom-side
"back band" and may be marked invisible to keep paint cost down. Anything
that needs to be hit-testable from the back band must be excluded from the
back-cache filter (see `_ensure_back_cache`).

**Board layer caching** (`_board_layer_dirty`): `render_board()` only runs
when the theme, camera side, transform, or board alpha changes.

**Refresh debounce** (`_refresh_timer`, 16 ms): `layout_changed` →
`_schedule_refresh()`.

**Cached ConnectivityGraph** (`session.connectivity_graph()`): invalidated by
`invalidate_graph()` (wired to `layout_changed`); built on demand.

**`user_data_map` on `ConnectivityGraph`** (`dict[int, object]`, keyed by
`ElectricSet.id`): scaffolding for future highlight optimisations, currently
unused.

For the history of the optimisation work see [performance.md](performance.md).
