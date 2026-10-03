# Incremental ConnectivityGraph

## Stripboard rules

Every pad on an uncut strip shares an electrical node on the strip's copper
layer. Two non-plated copper layers remain independent; plated holes bond
them. A component's metal pin bonds both physical sides even on a single-sided
board. Cuts between pads create separate nodes without removing the holes.
Removing a user wire must preserve built-in strip and pin connections.

`PadIndex` propagates nets and short-circuit state across each strip segment
after adding/removing wires or components and after changing pin assignments.
Built-in copper contributes connectivity, not physical wire occupancy.
Wire-net resolution in the editor uses the connectivity graph, so a wire
attached to a pad can inherit a net from a different pad on the same strip.

Logical net assignment and visual colour are distinct: the current board
renderer derives its colour map from component pins only and paints the track
underlay with the theme's strip colour. Full-strip net colouring and selecting
the strip itself remain GUI gaps.

## 1. Problem and motivation

The router asks one question over and over: **are pads A and B already
electrically connected?** In the naïve `networkx`-backed implementation every
new wire required rebuilding the whole graph from scratch — O(V + E) where
V is every board pad (600+ on micek) and E covers every wire, strip and
plated-through edge. On the micek project (143 components, 100 nets) the
greedy autorouter triggered that rebuild ~600 times, for a total of about
2.5 minutes.

The incremental structure replaces the full rebuild with O(path) operations:

- `add_wire(wire)` — union the affected sets, O(|path|).
- `remove_wire(wire)` — split the sets in LIFO order; replay surviving
  topology for out-of-order removal.
- `are_connected(a, b)` — connectivity check, O(1).

Effect: the autorouter greedy pass goes from ~2.5 minutes to ~5 seconds (30×).

---

## 2. Data model

### `ElectricSet` — a set of electrically connected points

Each group of electrically connected pads is one `ElectricSet` containing:

```
ElectricSet:
  id:            int                  # unique identifier
  _items:        list[NodeEntry]      # every pad in the set, in join order
  canonical_key: (x, y, side)         # lex-min of every pad's key — the stable representative
```

`canonical_key` is the lexicographic minimum across every `(x, y, side)` key
in the set. Two pads return the *same* canonical key if and only if they are
connected — so equality is O(1):

```python
node_for_pin(ref_a) == node_for_pin(ref_b)   # True iff connected
```

### `NodeEntry` — one electrical point

```
NodeEntry:
  key:           (x, y, side)        # grid position and board side
  electric_set:  ElectricSet         # the owning set (mutable on merge / split)
```

Every board pad (every hole on every side) maps to one `NodeEntry`. A
component pin with no incident wire owns its own singleton `ElectricSet` — a
zero-length "wire stub".

### Indices

```
PadIndex:  (x, y, side) → NodeEntry      # O(1) lookup by position
PinIndex:  PinRef → NodeEntry             # O(1) lookup by component pin
WireIndex: wire.id → WireRecord           # rollback history for remove_wire
```

### `WireRecord` — merge history

Every `add_wire()` produces a `WireRecord` listing every merge that took
place:

```
WireRecord:
  wire_id:           UUID
  merges:            list[MergeRecord]   # in execution order (LIFO on remove)
  is_loop:           bool                # True when neither endpoint joined any set
  intermediate_keys: list[...]           # nodes created exclusively for this wire
  node_paths:        list[list[NodeKey]]  # all contact/private nodes per fragment

MergeRecord:
  merged:    ElectricSet   # the survivor (the larger set)
  evicted:   ElectricSet   # the swallowed set
  split_idx: int           # merged._items[split_idx:] originated in evicted
```

---

## 3. Merge (`add_wire`)

When a wire connects two points from different sets, **union by size** is
performed — the smaller set is absorbed into the larger:

```
Before merge:
  SetA._items = [P1, P2, P3]   canonical = (1.5, 0.5, "top")
  SetB._items = [P4, P5]       canonical = (3.5, 0.5, "top")

The wire connects P3 (in SetA) to P4 (in SetB).
SetA is larger → SetB is absorbed.

After merge:
  SetA._items = [P1, P2, P3, P4, P5]    split_idx = 3
                  ←─── old SetA ───→ ←─SetB─→
  SetA.canonical = min((1.5,…), (3.5,…)) = (1.5, 0.5, "top")

  Every NodeEntry that pointed at SetB now points at SetA.
  SetB is removed from _all_sets but kept alive inside MergeRecord
  for the rollback path.
```

For a wire with N intermediate points the merge is sequential: pairs
`(path[0], path[1])`, `(path[1], path[2])`, etc. Each pair may merge a
different set. Every merge is appended to `WireRecord.merges`.

**Loop detection:** before each merge the implementation tests
`a.electric_set is b.electric_set` (identity, O(1)). If they are already the
same the wire is a redundant loop and no merge is performed:

```
Wire connects P1 and P3 — both already in SetA.
WireRecord.is_loop = True, merges = []
```

---

## 4. Split (`remove_wire`)

Undoing a merge restores the original split of `_items`:

```
Before split (after add_wire):
  SetA._items = [P1, P2, P3, P4, P5]    split_idx = 3

Split:
  items_merged  = SetA._items[:3] = [P1, P2, P3]
  items_evicted = SetA._items[3:] = [P4, P5]

  SetA._items = [P1, P2, P3]
  SetB._items = [P4, P5]                    (SetB resurrected from MergeRecord)

  For each of P4, P5: entry.electric_set = SetB
  SetB returns to _all_sets
  canonical for both sets is recomputed via min()
```

`WireRecord.merges` is undone in reverse (LIFO): the last merge is reverted
first. That keeps it correct when one wire produced several merges.

**Out-of-order case:** removing any wire except the most recently registered
one replays the surviving topology. Nodes are reset to singletons; built-in
board copper and pin bonds are restored, then surviving `node_paths` are
merged again. Each surviving record receives a fresh merge history. This
matters for parallel bridges: a formerly redundant wire may become the only
connection between two strips. The fallback traverses the full graph and
wire paths; LIFO removal retains the incremental fast path.

---

## 5. Canonical key and backward compatibility

The old `ConnectivityGraph` returned an `(x, y, side)` tuple — a
`networkx` node — from `node_for_pin()`. The new implementation returns
`ElectricSet.canonical_key`, which is the lexicographic minimum across the
set's pads.

Semantics are identical: two calls return the same value if and only if the
two pads are connected. Callers that only compare with `==` keep working
without modification.

`net_status.py` previously used `nx.has_path(graph._g, a, b)` — that became
`a == b` (canonical-key equality), O(1) instead of O(V+E).

`astar.py` previously used `graph._g.nodes`; it now uses
`graph.all_node_keys()` (an iterator over `PadIndex`).

---

## 6. Use in the autorouter

```
Before the greedy pass:
  graph = ConnectivityGraph(board, layout_without_wires, components, instances)
  for wire in user_wires:
      graph.add_wire(wire)         ← O(|path|) each

For each segment of each net:
  ┌─ check connectivity: graph.node_for_pin(ref_a) == graph.node_for_pin(ref_b)
  │
  ├─ if not connected → run A* → routed_wire
  │   graph.add_wire(routed_wire)  ← O(|path|), NOT O(full graph)
  │
  └─ rip-up when no path exists:
      graph.remove_wire(ripped)    ← O(|evicted_set|)
      working_wires.remove(ripped)

Old loop: ConnectivityGraph(board, layout_with_all_wires, …)   ← O(V+E) × 600
New loop: add_wire() × 600                                      ← O(path_len) × 600
```

---

## 7. Operation complexity

| Operation | Complexity | Notes |
|---|---|---|
| `ConnectivityGraph.__init__()` | O(V + E) | One-off build from Board + Layout |
| `add_wire(wire)` | O(\|path\|) | Sequential pairwise merges |
| `remove_wire(wire)` — LIFO | O(K) | K = size of the evicted set |
| `remove_wire(wire)` — out-of-order | O(V + E) | Replay permanent connections and surviving paths |
| `are_connected(a, b)` | O(1) | `is` comparison on `ElectricSet` |
| `node_for_pin(ref)` | O(1) | `PinIndex` lookup + canonical_key |
| `node_for_pad(pos, side)` | O(1) | `PadIndex` lookup + canonical_key |
| `electrical_nets()` | O(P) | P = number of pins in `PinIndex` |

---

## 8. Caveats

**Out-of-order rip-up** — the autorouter may remove any wire, not just the
most recent one. When the removed wire isn't on top of the merge history
the structure replays the full surviving topology. This also handles loops
that did not originally produce any merges. Correct but costlier than LIFO.

**Multi-hop wires** — a wire crossing N points performs N−1 merges. Each is
recorded separately in `WireRecord.merges`. Rollback reverses them LIFO —
correct as long as none of the N−1 merged sets has been absorbed by another
wire in the meantime.

**Threading** — the structure is *not* thread-safe. `add_wire` / `remove_wire`
must run sequentially. Read access (`node_for_pin`, `are_connected`) is
safe as long as no thread is concurrently mutating. The autorouter respects
this: A* (which can run in parallel under the Rust backend) only reads
`all_node_keys()`, and graph mutations happen sequentially in the greedy
main loop.
