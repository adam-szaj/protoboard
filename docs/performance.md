# Performance optimisations (history)

This file is the historical narrative of how the routing and GUI hot paths
were tuned. The current architecture overview is in
[benchmarks.md](benchmarks.md) and the connectivity data structure is in
[connectivity.md](connectivity.md).

## Background

The "micek" project (143 components, 100 nets) was the original benchmark
target. Starting point:

- Autoroute All Nets: **~2.5 minutes**, the UI was frozen the whole time.
- Component drag: stutters every ~600 ms (DRC blocked the UI thread).
- After mouse-up: ~150 ms freeze (synchronous rebuild of four side panels).

After four stages of optimisation:

- Autoroute All Nets: **~15 seconds**, UI responsive (worker on a `QThread`).
- Component drag: no perceptible stutter.
- DRC: 7.8 ms → 1.5 ms.

---

## Stage 1 — Incremental ConnectivityGraph

**Problem:** the autorouter called `ConnectivityGraph(board, layout, ...)` for
every A* segment. With ~600 segments per project that meant ~600 full
`networkx` rebuilds — O(V+E) each — adding up to roughly 2.5 minutes.

**Solution:** `domain/connectivity/incremental.py` replaces `networkx` with a
union-find structure that supports rollback.

| Operation | After | Before |
|---|---|---|
| `add_wire(wire)` | O(\|path\|) | O(V+E) — full rebuild |
| `remove_wire(wire)` | O(K) — size of the evicted set | O(V+E) |
| `are_connected(a, b)` | O(1) — `canonical_key` comparison | O(V+E) — `nx.has_path` |

See [connectivity.md](connectivity.md) for the implementation details.

**Effect:** autorouter greedy pass: 2.5 minutes → **5 seconds** (30×).

---

## Stage 2 — Native A* in Rust (PyO3 + rayon)

**Problem:** the CPython GIL prevented real parallelism. A
`ThreadPoolExecutor` with 16 threads gave exactly one active thread at a time.

**Solution:** native A* in Rust (`services/routing/astar_rs/`):

- `pyo3` for Python ↔ Rust bindings; `py.allow_threads()` releases the GIL.
- `rayon` work-stealing thread pool; each A* "universe" becomes one rayon task.
- `f64 → i32` rescale (×1000) — avoids hashing floats in `HashMap`.
- `Arc<Vec<PathStep>>` for paths — branching is just an atomic refcount bump.

The pure Python implementation stays as a fallback when the Rust extension
isn't available (`try: import protoboard_astar_rs`).

The multi-pass optimiser launches 10 parallel autorouter instances in
separate processes (`ProcessPoolExecutor`), each with a different net
ordering (process isolation sidesteps the GIL completely).

**Effect:** total autoroute time on micek: 2.5 min → **~15 seconds**.

---

## Stage 3 — Side-panel debounce

**Problem:** `layout_changed` was connected directly to `refresh()` on four
side panels (netlist, groups, nets editor, components). Each panel did
`clear()` + a `QTreeWidget` rebuild — about 150 ms of synchronous blocking
on every mouse-release.

**Solution:** `_panels_timer` (`QTimer`, singleShot, 100 ms) in `MainWindow`.

```
layout_changed → _panels_timer.start()   # instant, ~0 ms
100 ms later   → _refresh_panels()       # one rebuild, amortised
```

If another `layout_changed` arrives inside the 100 ms window (rapid drags,
keyboard arrow runs) the timer restarts — panels only refresh after the
burst settles.

**Effect:** no perceptible mouse-up freeze. Bursts of moves collapse from N
rebuilds to one.

---

## Stage 4 — Cached ConnectivityGraph in the session

**Problem:** `DRCEngine.check()` built a fresh `ConnectivityGraph` on every
run. On micek the graph build alone was 6.3 ms of the 7.8 ms total.

**Solution:** keep the graph as `_graph: ConnectivityGraph | None` on
`EditorSession`:

```python
# On layout_changed — synchronous, ~0 ms:
session.invalidate_graph()   # _graph = None

# On DRC run — at most one build per DRC dialog open:
session.connectivity_graph() # builds if None, otherwise returns the cache
```

`DRCEngine.check_with_graph()` accepts a prebuilt graph and skips the
internal build. The old `check()` stays for back-compat (builds internally
and delegates).

**Effect:**

| | Before | After |
|---|---|---|
| DRC (cold) | 7.8 ms | 7.8 ms (first call after a change) |
| DRC (warm, no change) | 7.8 ms | **1.5 ms** |
| Move burst | freeze every 600 ms | one build at the end of the burst |

---

## Stage 5 — Paint perf refactor

A multi-phase paint refactor (2026-05-24, archived under
[selection-fsm-refactor-2026-06-08]] in the memory index) brought the paint
budget from 700–900 ms down to ~85 ms by switching to back-cache + Z-banding
and reusing `QGraphicsItemGroup`s. The current architecture is summarised in
[benchmarks.md](benchmarks.md).

---

## Summary

| Metric | Before | After | Change |
|---|---|---|---|
| Autoroute micek | ~2.5 min | ~15 s | **10×** |
| DRC (cold build) | 7.8 ms | 1.5 ms | **5×** |
| Mouse-up freeze | ~150 ms | ~0 ms | — |
| Move-burst stutter | every 600 ms | none | — |
| Full repaint | 700–900 ms | ~85 ms | **~10×** |
