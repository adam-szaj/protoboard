# Routing

## 1. Cost model

`RoutingCostModel` is **dict-backed** (`weights: dict[str, float]`) with
backward-compatible legacy attribute access (`cost_model.direction_change`,
etc.). Routers declare which keys they understand via
`Router.supported_cost_keys()`; the presets UI filters the visible fields to
the union of every registered router's keys.

**Core API:**

```python
cm = RoutingCostModel(weights={"direction_change": 1.0, "bend_45": 0.5, ...})
cm.get("bend_45", 0.0)             # forward-compat lookup
cm.direction_change                # legacy attribute (delegates to weights)
cm.with_overrides(bend_45=2.0)     # immutable, returns a new instance
```

### 1.1 Canonical keys (`COST_KEY_DOCS`)

| Key | Default | Meaning |
|---|---|---|
| `free_pad` | 1.0 | Base cost per orthogonal step. |
| `same_net_pad` | 0.1 | Step onto a pad of the active net (discount). |
| `occupied_by_other_net` | `inf` | Pin of another net (blocked). |
| `direction_change` | 1.0 | Legacy fallback per bend (used when `bend_*` are unset). |
| `diagonal_multiplier` | √2 | Multiplier for diagonal steps. |
| `crossing_bare_bare` | `inf` | Bare × bare = short. |
| `crossing_bare_bare_diagonal` | 2.0 | Two bare diagonals at a mesh-cell centre (parity mode). |
| `layer_change` | 2.0 | Via on a plated-through pad. |
| `under_component_top` | 3.0 | Step on the top layer beneath a component body. |
| `bare_diagonal_mode` | 0/1 | Strict parity filter for bare diagonals. |
| `max_reroute_factor` | 0.0 | Push-and-shove ceiling. |
| `same_net_wire_reuse_cost` | 0.0 | Touching an existing same-net wire (0 = free reuse). |
| `parallel_opposite_side` | 0.0 | Step overlaps an existing wire segment on the opposite side. |

**Granular crossing penalties** (insulated × bare and insulated × insulated,
each split into `{at_pad, off_pad} × {same_side, opposite_side}`) live
under the `crossing_ib_*` and `crossing_ii_*` keys. All default to 0.0; the
built-in `clean_routing` preset turns them on.

**Clean-routing knobs** (opt-in, defaults 0.0):

| Key | "clean_routing" | Meaning |
|---|---|---|
| `bend_45` | 0.5 | Penalty for a 45° bend (cos > 0.6). |
| `bend_90` | 1.5 | Penalty for a 90° bend (cos > −0.1). |
| `bend_135` | 4.0 | Penalty for a 135° bend (sharp turn). |
| `diagonal_preferred_family` | 0.0 | Bonus for a diagonal in the preferred family. |
| `diagonal_dispreferred_family` | 0.5 | Cost for a diagonal in the "wrong" family. |
| `wire_over_foreign_wire_pad` | 0.5 | Step into a cell occupied by another net's wire vertex. |
| `via_on_foreign_pad` | 3.0 | Via on a pad that already has a foreign-net via. |
| `redundant_layer_change` | 1.5 | Penalty per via that could have been avoided (drives zebra suppression). |

**Advanced tactical knobs:**

| Key | Meaning |
|---|---|
| `deferred_via_lookahead` | When non-zero, every via is held tentative until the next obstacle on the alternate side proves the via was worth it. 0 disables the mode. |
| `deferred_via_priority_mode` | 0 = real-g priority (admissible). 1 = `min(g_real, g_shadow)` (keeps the tentative branch alive but no longer admissible). |
| `congestion_weight` | Multiplier for the pre-route congestion map (per-cell vote count from trivial MST L-shapes). 0 disables it. |

`USER_TUNABLE_COST_KEYS` in `cost_model.py` lists the keys surfaced in the
UI. Keys outside that set are either physical constants, hard electrical
bans, or implementation details.

### 1.2 Bend classification by angle

A* classifies every bend using the cosine between `(prev_dx, prev_dy)` and
`(next_dx, next_dy)`:

- `cos > 0.6`  → 45° bend (e.g. E → NE)
- `cos > −0.1` → 90° bend (e.g. E → N)
- `cos ≤ −0.1` → 135° bend (e.g. E → NW, sharp turn)

When every `bend_*` key falls back to `direction_change` (the default) the
behaviour matches the legacy single-constant turn penalty exactly.

### 1.3 Diagonal parity × side × family

The preferred diagonal family per `(cell parity, side)`:

| `(x + y) mod 2` | Side | Preferred family |
|---|---|---|
| even (0) | top | NE/SW (`/`) |
| even (0) | bottom | NW/SE (`\`) |
| odd (1)  | top | NW/SE (`\`) |
| odd (1)  | bottom | NE/SW (`/`) |

Geometric goal: when two diagonals meet inside the inter-pad space of one
cell they are mutually orthogonal (an X-shaped cross) and physically cannot
collide — they can be threaded between the pads without shorting.

### 1.4 Presets

Cost keys are bundled into **presets** — named dicts stored in
`app_settings.yaml` (`cost_presets:` section).

**Built-in presets (read-only):**

- `default` — historical defaults (every new knob = 0.0).
- `clean_routing` — the values in the "clean_routing" column above.

User-defined presets are managed via **Preferences → Routing → Edit
presets…**. Built-in names cannot be shadowed (the loader ignores any user
preset whose name matches a built-in one).

### 1.5 Per-project configuration (`routing.yaml`)

```yaml
preferred_side: bottom
allow_layer_change: true
max_rip_up_passes: 3
max_optimize_iterations: 10
simplify_after_each_wire: false
simplify_all_after_autoroute: true
simplify_all_max_iterations: 50
visual_unweave_side_flip: false
cost_preset: clean_routing      # name of a built-in or user preset
cost_overrides:                 # per-project overrides
  bend_45: 0.3
cost_model:                     # legacy named-field fallback
  direction_change: 1.0
  crossing_insulated_bare: 5.0
  layer_change: 2.0
  under_component_top: 3.0
```

Resolution order (later wins): built-in `default` → preset → legacy named
fields → `cost_overrides`. Old projects without `cost_preset` or
`cost_overrides` load identically to before.

`RoutingSettings` (immutable, `domain/routing_settings.py`) carries the
resolved values. Edit through **Preferences → Routing**.

## 2. The A* algorithm

Nodes are `(x, y, side)`; edges follow 8-connectivity. A via is a
plated-through pad (cost `layer_change`).

**Multi-layer paths:** `PathStep = (Point, side)`. A* returns the path; the
autorouter splits it by side into one fragment per layer using
`split_path_by_side()`.

**`preferred_side` tie-breaker:** at equal cost, the side configured in
`RoutingSettings.preferred_side` (default `bottom`) wins.

**Greedy Steiner tree:** the seed is the pair with the minimum Manhattan
distance. Every subsequent step picks the closest `(connected,
unconnected)` pair; A* receives one concrete source pin.

**`near_pin_cost` corridor penalty:** pins of as-yet incomplete nets attract
a per-cell penalty inside `near_pin_radius` (and an extra band up to
`near_pin_extra_radius`). Pins of components already represented in the
current net are excluded — this prevents the "Panama Canal through DIP8"
pathology where the router tries to thread through its own component.

**Iterative optimisation:** the autorouter runs up to
`max_optimize_iterations` passes from a clean state and stops when the wire
set converges (fingerprint of wire ids and paths is unchanged).

**Wire occupation blocking:** `_build_wire_occupied()` is side-aware —
insulated wires block their own side only, bare wires block both.

**Simplify-all loop:** when
`RoutingSettings.simplify_all_after_autoroute=True` (the default), the
autorouter runs `SimplifyOptimizer` in a fixed-point loop after all nets are
routed (capped at `simplify_all_max_iterations`). This cleans up
trapezoidal detours caused by `near_pin_cost`. The legacy single-pass
bare-diagonal simplify still runs for back-compat.

**Visual unweave** (opt-in via `visual_unweave_side_flip=True`): a
post-cleanup pass tries to flip a wire's `side` when doing so reduces both
the 2D-shared edge count and the cross-side crossing count.

**Diagnostic logging:** the `_DEEP_DEBUG` flag in `autorouter.py` enables
per-step tracing. The `SIMPLIFY_TRACE=1` environment variable surfaces the
simplifier's per-pad item dump.

## 3. Domain helpers — `wire_utils`

`domain/layout/wire_utils.py`:

- `board_pads_for_side(board, side)` → `set[Point]`
- `plated_through_pads(board)` → `set[Point]`
- `insert_pad_nodes(path, pad_positions)` → `list[Point]` — inserts vertices
  on every pad that the path crosses

## 4. Autorouter

`services/routing/autorouter.py` exposes `autoroute()` (full board) and
`_run_one_pass()` (one greedy pass). It is reachable from **Tools →
Autoroute All Nets** (F6) and from the Net Inspector for a single net
(`Shift+F6` is unbound by default but available as an action). The result
is wrapped in a `MacroCommand` so the whole pass is one undo step.

A native Rust A* (`services/routing/astar_rs/`, PyO3 + rayon) is used when
available; otherwise the pure Python implementation runs. See
[performance.md](performance.md) for the optimisation history.

**Hierarchical autoroute** (`services/routing/hierarchical.py`) walks the
group tree bottom-up so that `ComponentGroup` organisation and
`exported_pins` shape the routing strategy. The full-board hook is in
place; the user-facing trigger is deferred until the workflow has been
agreed upon.
