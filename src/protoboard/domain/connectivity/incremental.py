# SPDX-License-Identifier: MIT
"""Incremental connectivity graph — O(1) merge/split instead of full networkx rebuild.

Replaces ConnectivityGraph with a structure that supports add_wire() and
remove_wire() without rebuilding the entire graph from scratch.

Data model
----------
Every electrically distinct point (pad) is a NodeEntry.  All NodeEntries that
are currently connected form an ElectricSet.  Each ElectricSet carries a
``canonical_key`` — the lexicographic minimum (x, y, side) of its members —
so that ``node_for_pin(a) == node_for_pin(b)`` iff a and b are connected.

When a wire is added it may merge several ElectricSets.  Each merge is
recorded as a MergeRecord (merged set, evicted set, split index) so it can be
reversed in O(K) where K is the size of the evicted set.

When a wire is removed the merge records for that wire are replayed in reverse
order (LIFO).  If the wire is not the most-recent merger of the two sets
(i.e. another wire created a loop before removal), a local BFS rebuild of the
affected set is performed instead — still much cheaper than a full graph
rebuild.
"""

from __future__ import annotations

import itertools
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterator

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import PadAtom, StripAtom
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import pin_world_position
from protoboard.domain.layout.wire import Wire, Wire
from protoboard.domain.schematic.net import PinRef
from protoboard.geometry import Point

# Quantisation helpers live in pad_geometry — shared with PadIndex so both
# indexes agree on "is this the same pad" tolerance.
from protoboard.domain.board.pad_geometry import (
    SNAP_TOL as _SNAP_TOL,
    NodeKey as _NodeKey,
    q as _q,
    qkey as _qkey,
)


# ---------------------------------------------------------------------------
# Core data structures
# ---------------------------------------------------------------------------

class ElectricSet:
    """A set of electrically connected NodeEntries."""

    _id_counter = itertools.count()

    def __init__(self) -> None:
        self.id: int = next(ElectricSet._id_counter)
        self._items: list[NodeEntry] = []
        self.canonical_key: _NodeKey = (float("inf"), float("inf"), "z")

    def _recompute_canonical(self) -> None:
        if self._items:
            self.canonical_key = min(e.key for e in self._items)
        else:
            self.canonical_key = (float("inf"), float("inf"), "z")

    def __repr__(self) -> str:
        return f"ElectricSet(id={self.id}, size={len(self._items)}, canonical={self.canonical_key})"


class NodeEntry:
    """One electrically distinct point on the board."""

    __slots__ = ("key", "electric_set")

    def __init__(self, key: _NodeKey, eset: ElectricSet) -> None:
        self.key: _NodeKey = key
        self.electric_set: ElectricSet = eset

    def __repr__(self) -> str:
        return f"NodeEntry({self.key})"


@dataclass
class MergeRecord:
    """Records one merge operation so it can be reversed."""
    merged: ElectricSet   # the set that absorbed the other (survived)
    evicted: ElectricSet  # the set that was absorbed (now "dead")
    split_idx: int        # merged._items[split_idx:] were from evicted


@dataclass
class WireRecord:
    """All merge records produced by one add_wire() call, in order."""
    wire_id: uuid.UUID
    merges: list[MergeRecord]       # in order of execution
    is_loop: bool = False           # True when wire connected already-same set
    intermediate_keys: list[_NodeKey] = field(default_factory=list)  # wire-only nodes


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class ConnectivityGraph:
    """Incremental electrical graph — drop-in replacement for the networkx version.

    Supports the same public API as the old ConnectivityGraph plus two new
    incremental methods: ``add_wire`` and ``remove_wire``.

    ``user_data_map`` is a free-form dict keyed by ElectricSet.id that callers
    (e.g. the GUI renderer) can use to attach arbitrary data — for example a
    reference to the QGraphicsItem representing that net's highlight overlay.
    The graph never reads or writes this dict itself.
    """

    def __init__(
        self,
        board: Board,
        layout: Layout,
        components: dict[str, ComponentDefinition],
        instances: dict[uuid.UUID, ComponentInstance] | None = None,
    ) -> None:
        # pad (x,y,side) → NodeEntry
        self._pad_index: dict[_NodeKey, NodeEntry] = {}
        # PinRef → NodeEntry (the pad the pin sits on)
        self._pin_index: dict[PinRef, NodeEntry] = {}
        # wire uuid → WireRecord (for incremental remove)
        self._wire_records: dict[uuid.UUID, WireRecord] = {}
        # living ElectricSets
        self._all_sets: set[ElectricSet] = set()
        # board pad keys (keys that must NOT be removed when a wire is removed)
        self._board_pad_keys: set[_NodeKey] = set()
        # Free-form per-ElectricSet data for external use (e.g. GUI item refs).
        self.user_data_map: dict[int, object] = {}
        # Cache for valid_nodes_by_side() — None means stale, rebuilt lazily.
        self._valid_nodes_cache: dict[str, set["Point"]] | None = None
        # Cache for board pad positions per side — lazy, immutable after build
        # (board geometry does not change during a graph's lifetime).
        self._board_pads_cache: dict[str, set["Point"]] = {}

        self._components = components
        self._instances = instances or {}
        # Kept as references so callers that need to rebuild a graph from a
        # modified layout (e.g. unweave's short-circuit check) don't have to
        # re-thread board / components / instances through every call site.
        self._board = board
        self._layout = layout

        self._build(board, layout)

    # Read-only accessors for the construction arguments.  Kept narrow on
    # purpose so the rest of the codebase doesn't start mutating internals
    # through them.
    @property
    def board(self) -> "Board":
        return self._board

    @property
    def layout(self) -> "Layout":
        return self._layout

    @property
    def components(self) -> dict[str, "ComponentDefinition"]:
        return self._components

    @property
    def instances(self) -> dict[uuid.UUID, "ComponentInstance"]:
        return self._instances

    # ------------------------------------------------------------------
    # Public API  (compatible with old ConnectivityGraph)
    # ------------------------------------------------------------------

    def node_count(self) -> int:
        return len(self._pad_index)

    def component_count(self) -> int:
        return len(self._all_sets)

    def electrical_nets(self) -> list[set[PinRef]]:
        """One set of PinRefs per connected component (ElectricSet)."""
        eset_to_pins: dict[int, set[PinRef]] = defaultdict(set)
        for ref, entry in self._pin_index.items():
            eset_to_pins[entry.electric_set.id].add(ref)
        result: list[set[PinRef]] = []
        # Include sets that have no pins (board-only connectivity)
        for eset in self._all_sets:
            pins = eset_to_pins.get(eset.id, set())
            result.append(pins)
        return result

    def node_for_pin(self, ref: PinRef) -> _NodeKey | None:
        entry = self._pin_index.get(ref)
        if entry is None:
            return None
        return entry.electric_set.canonical_key

    def node_for_pad(self, position: Point, side: str) -> _NodeKey | None:
        key: _NodeKey = _qkey(position, side)
        entry = self._pad_index.get(key)
        if entry is None:
            return None
        return entry.electric_set.canonical_key

    def all_node_keys(self) -> Iterator[_NodeKey]:
        """Iterate over all pad keys — replaces ``graph._g.nodes`` in astar.py."""
        return iter(self._pad_index)

    def valid_nodes_by_side(self) -> dict[str, set[Point]]:
        """Return {side: set[Point]} of all pads, cached between wire mutations."""
        if self._valid_nodes_cache is None:
            vn: dict[str, set[Point]] = {"top": set(), "bottom": set()}
            for nx, ny, ns in self._pad_index:
                if ns in vn:
                    vn[ns].add(Point(nx, ny))
                # insulated-wire transient keys have side like "top_ins_…" — skip them
            self._valid_nodes_cache = vn
        return self._valid_nodes_cache

    def pads_in_same_electric_set(self, position: Point, side: str) -> list[Point]:
        """All pad positions (same side) in the same ElectricSet as (position, side).

        Returns [position] if the pad is isolated or not in the graph, so the
        caller always gets a non-empty target list for A*.

        Used by the autorouter: instead of routing to a single target pin, A*
        can stop at any pad already electrically connected to that pin — i.e.
        any existing wire of the same net is a valid endpoint.
        """
        key: _NodeKey = _qkey(position, side)
        entry = self._pad_index.get(key)
        if entry is None:
            return [position]
        return [
            Point(k[0], k[1])
            for k in (e.key for e in entry.electric_set._items)
            if k[2] == side
        ]

    # ------------------------------------------------------------------
    # Incremental operations
    # ------------------------------------------------------------------

    def _board_pads_for_side(self, side: str) -> set["Point"]:
        """Return cached set of pad positions on *side* (lazy, immutable)."""
        cached = self._board_pads_cache.get(side)
        if cached is not None:
            return cached
        from protoboard.domain.layout.wire_utils import board_pads_for_side
        pads = board_pads_for_side(self._board, side)
        self._board_pads_cache[side] = pads
        return pads

    def _enrich_bare_path(self, path: list["Point"], side: str) -> list["Point"]:
        """Insert board-pad positions that lie on segments of *path*.

        Returned for bare wires only — they physically contact every pad they
        cross, so connectivity must see those pads as vertices even when the
        recorded path took a shortcut.  Idempotent: already-present pads are
        not duplicated.
        """
        if len(path) < 2:
            return path
        from protoboard.domain.layout.wire_utils import insert_pad_nodes
        return insert_pad_nodes(path, self._board_pads_for_side(side))

    def add_wire(self, wire: Wire) -> None:
        """Incorporate *wire* into the graph, merging sets as needed."""
        self._valid_nodes_cache = None
        side = wire.side
        if not wire.path:
            return
        # For bare wires the physical conductor rests on every board pad it
        # crosses, regardless of how many vertices the recorded path contains.
        # Enrich the path in-memory (without mutating *wire*) so that every
        # crossed pad becomes a graph node and ends up in the same ElectricSet.
        # Insulated wires keep the original path — they float above the board.
        if not wire.insulated:
            path = self._enrich_bare_path(list(wire.path), side)
        else:
            path = list(wire.path)

        intermediate: list[_NodeKey] = []
        path_entries: list[NodeEntry] = []
        n_pts = len(path)
        for i, pt in enumerate(path):
            key: _NodeKey = _qkey(pt, side)
            # An insulated wire only makes electrical contact at its two
            # endpoints (where it is soldered to a pad).  Intermediate points
            # float above the board and must not merge with pads they cross.
            is_endpoint = (i == 0 or i == n_pts - 1)
            entry = self._pad_index.get(key)
            if wire.insulated and not is_endpoint:
                # Intermediate point of an insulated wire: the wire floats
                # above the board and makes no electrical contact here.
                # Create a private node (not reachable via node_for_pad) so
                # no other wire can accidentally share it.
                private_key: _NodeKey = _qkey(pt, side + f"_ins_{wire.id.int}")
                private_entry = self._make_node(private_key)
                intermediate.append(private_key)
                entry = private_entry
            elif entry is None:
                # Point not on a board pad (e.g. diagonal mid-step between
                # pads).  Create a private transient node per wire so that two
                # bare wires crossing at the same off-pad point do NOT get
                # merged — those wires don't actually share an electrical
                # contact unless they both land on the same board pad.
                transient_key: _NodeKey = _qkey(pt, side + f"_tr_{wire.id.int}")
                entry = self._make_node(transient_key)
                intermediate.append(transient_key)
            path_entries.append(entry)

        merges: list[MergeRecord] = []
        is_loop = False

        # Walk consecutive pairs and merge their sets.
        for i in range(len(path_entries) - 1):
            a = path_entries[i]
            b = path_entries[i + 1]
            if a.electric_set is b.electric_set:
                is_loop = True
                continue  # already connected — no merge needed
            merged, evicted, split_idx = self._merge(a.electric_set, b.electric_set)
            merges.append(MergeRecord(merged=merged, evicted=evicted, split_idx=split_idx))

        self._wire_records[wire.id] = WireRecord(
            wire_id=wire.id,
            merges=merges,
            is_loop=is_loop,
            intermediate_keys=intermediate,
        )

    def remove_wire(self, wire: Wire) -> None:
        """Remove *wire* from the graph, splitting sets as needed.

        Uses recorded merge history (LIFO) when possible; falls back to a
        local BFS rebuild when the wire is not the most recent merger.
        """
        self._valid_nodes_cache = None
        record = self._wire_records.pop(wire.id, None)
        if record is None:
            return

        if record.is_loop and not record.merges:
            # Wire created a loop but performed no merge — nothing to undo.
            self._remove_intermediate_nodes(record)
            return

        # Replay merges in reverse order.
        for mr in reversed(record.merges):
            # Verify that the split is still valid: the merged set must still
            # be "live" (not further merged into another set by a later wire).
            if mr.merged not in self._all_sets:
                # The merged set was later absorbed by another wire — we need
                # a local rebuild of the affected subgraph instead.
                self._local_rebuild_after_remove(wire, record)
                return
            self._split(mr)

        self._remove_intermediate_nodes(record)

    # ------------------------------------------------------------------
    # v2 (Wire / Fragment / Knot) entry points
    # ------------------------------------------------------------------

    def add_wire(self, wire: Wire) -> None:
        """Incorporate a Wire into the graph by adding each fragment in turn.

        All resulting merges aggregate into a single ``WireRecord`` keyed by
        ``wire.id`` so that ``remove_wire`` can reverse them as a whole.
        This is the v2 entry point — it does not depend on the legacy
        ``Wire`` storage.
        """
        if wire.id in self._wire_records:
            # Idempotent: a Wire is either present or absent.
            return
        self._valid_nodes_cache = None
        merges_all: list[MergeRecord] = []
        intermediate_all: list[_NodeKey] = []
        is_loop_any = False
        for frag in wire.fragments:
            side = frag.side
            path = list(frag.path)
            if not wire.insulated:
                path = self._enrich_bare_path(path, side)
            n_pts = len(path)
            path_entries: list[NodeEntry] = []
            for i, pt in enumerate(path):
                key: _NodeKey = _qkey(pt, side)
                is_endpoint = (i == 0 or i == n_pts - 1)
                entry = self._pad_index.get(key)
                if wire.insulated and not is_endpoint:
                    private_key: _NodeKey = _qkey(
                        pt, side + f"_ins_{wire.id.int}_{id(frag)}"
                    )
                    private_entry = self._make_node(private_key)
                    intermediate_all.append(private_key)
                    entry = private_entry
                elif entry is None:
                    transient_key: _NodeKey = _qkey(
                        pt, side + f"_tr_{wire.id.int}_{id(frag)}"
                    )
                    entry = self._make_node(transient_key)
                    intermediate_all.append(transient_key)
                path_entries.append(entry)
            for i in range(len(path_entries) - 1):
                a = path_entries[i]
                b = path_entries[i + 1]
                if a.electric_set is b.electric_set:
                    is_loop_any = True
                    continue
                merged, evicted, split_idx = self._merge(a.electric_set, b.electric_set)
                merges_all.append(
                    MergeRecord(merged=merged, evicted=evicted, split_idx=split_idx)
                )
        self._wire_records[wire.id] = WireRecord(
            wire_id=wire.id,
            merges=merges_all,
            is_loop=is_loop_any,
            intermediate_keys=intermediate_all,
        )

    def remove_wire(self, wire: Wire) -> None:
        """Remove a Wire from the graph, replaying merges across all fragments."""
        self._valid_nodes_cache = None
        record = self._wire_records.pop(wire.id, None)
        if record is None:
            return
        if record.is_loop and not record.merges:
            self._remove_intermediate_nodes(record)
            return
        for mr in reversed(record.merges):
            if mr.merged not in self._all_sets:
                self._local_rebuild_after_remove_v2(wire, record)
                return
            self._split(mr)
        self._remove_intermediate_nodes(record)

    def _local_rebuild_after_remove_v2(
        self, wire: Wire, record: WireRecord
    ) -> None:
        """Wire-level fallback rebuild — collects touched keys across fragments."""
        touched: set[_NodeKey] = set()
        for frag in wire.fragments:
            side = frag.side
            for pt in frag.path:
                touched.add(_qkey(pt, side))
        affected_entries: list[NodeEntry] = [
            self._pad_index[k] for k in touched if k in self._pad_index
        ]
        if not affected_entries:
            self._remove_intermediate_nodes(record)
            return
        affected_sets: set[int] = {e.electric_set.id for e in affected_entries}
        for eset_id in affected_sets:
            eset_entries = [
                e for e in self._pad_index.values()
                if e.electric_set.id == eset_id
            ]
            self._rebuild_set(eset_entries)
        self._remove_intermediate_nodes(record)

    def are_connected(
        self, position_a: Point, side_a: str,
        position_b: Point, side_b: str,
    ) -> bool:
        """O(1) connectivity check."""
        ea = self._pad_index.get(_qkey(position_a, side_a))
        eb = self._pad_index.get(_qkey(position_b, side_b))
        if ea is None or eb is None:
            return False
        return ea.electric_set is eb.electric_set

    # ------------------------------------------------------------------
    # Internal — node creation
    # ------------------------------------------------------------------

    def _make_node(self, key: _NodeKey) -> NodeEntry:
        eset = ElectricSet()
        entry = NodeEntry(key=key, eset=eset)
        eset._items.append(entry)
        eset.canonical_key = key
        self._pad_index[key] = entry
        self._all_sets.add(eset)
        return entry

    # ------------------------------------------------------------------
    # Internal — merge / split
    # ------------------------------------------------------------------

    def _merge(
        self, set_a: ElectricSet, set_b: ElectricSet
    ) -> tuple[ElectricSet, ElectricSet, int]:
        """Union by size. Returns (merged, evicted, split_idx)."""
        if len(set_a._items) >= len(set_b._items):
            merged, evicted = set_a, set_b
        else:
            merged, evicted = set_b, set_a

        split_idx = len(merged._items)
        merged._items.extend(evicted._items)

        for entry in evicted._items:
            entry.electric_set = merged

        # Update canonical — evicted's canonical may be smaller.
        if evicted.canonical_key < merged.canonical_key:
            merged.canonical_key = evicted.canonical_key

        self._all_sets.discard(evicted)
        return merged, evicted, split_idx

    def _split(self, mr: MergeRecord) -> None:
        """Reverse one MergeRecord."""
        merged = mr.merged
        evicted = mr.evicted
        idx = mr.split_idx

        # Partition items back.
        items_merged = merged._items[:idx]
        items_evicted = merged._items[idx:]

        merged._items = items_merged
        evicted._items = items_evicted

        # Restore back-references.
        for entry in items_evicted:
            entry.electric_set = evicted

        # Recompute canonicals — O(K) but K is small for the evicted side.
        merged._recompute_canonical()
        evicted._recompute_canonical()

        self._all_sets.add(evicted)

    # ------------------------------------------------------------------
    # Internal — local rebuild after out-of-order removal
    # ------------------------------------------------------------------

    def _local_rebuild_after_remove(
        self, wire: Wire, record: WireRecord
    ) -> None:
        """Fallback: rebuild connectivity for the nodes touched by *wire*.

        This is O(N_affected + E_affected) — much cheaper than a full graph
        rebuild when only a small portion of the board is affected.
        """
        # Collect all nodes that were on the wire's path.
        side = wire.side
        touched: set[_NodeKey] = {
            _qkey(pt, side) for pt in wire.path
        }

        # Find the ElectricSet that currently contains these nodes.
        # They may all be in one big set now (due to subsequent merges).
        affected_entries: list[NodeEntry] = [
            self._pad_index[k] for k in touched if k in self._pad_index
        ]
        if not affected_entries:
            self._remove_intermediate_nodes(record)
            return

        # Determine which set(s) to rebuild — may be one merged super-set.
        affected_sets: set[int] = {e.electric_set.id for e in affected_entries}

        # For each affected set, rebuild its connectivity from scratch using
        # only the wires that are still registered (excluding the removed wire).
        for eset_id in affected_sets:
            # Find all entries currently in this set.
            eset_entries = [
                e for e in self._pad_index.values()
                if e.electric_set.id == eset_id
            ]
            self._rebuild_set(eset_entries)

        self._remove_intermediate_nodes(record)

    def _rebuild_set(self, entries: list[NodeEntry]) -> None:
        """BFS re-cluster a list of entries using surviving wire records."""
        # Reset: each entry gets its own singleton set.
        old_set = entries[0].electric_set if entries else None

        # Remove old set from live sets.
        if old_set is not None:
            self._all_sets.discard(old_set)

        # Give each node its own fresh set.
        for entry in entries:
            eset = ElectricSet()
            eset._items.append(entry)
            eset.canonical_key = entry.key
            entry.electric_set = eset
            self._all_sets.add(eset)

        # Re-apply all surviving wire records that touch these nodes.
        keys_in_rebuild: set[_NodeKey] = {e.key for e in entries}
        for wr in self._wire_records.values():
            # We don't have direct wire→path access here; wire records only
            # store merge history.  Re-merge via the recorded merges is
            # unreliable after the reset above.  Instead, collect keys per
            # wire from the pad index and re-run connectivity.
            pass  # handled below via structural re-wiring

        # Re-wire using surviving WireRecords: for each record, find the
        # endpoints still in our key set and merge their sets.
        # This is the simplest correct approach.
        self._rewire_from_records(keys_in_rebuild)

    def _rewire_from_records(self, key_set: set[_NodeKey]) -> None:
        """Re-apply surviving wire records for nodes in key_set."""
        # For each wire record, we stored the intermediate keys.
        # Walk them and reconnect adjacent nodes if both are in key_set.
        for wr in self._wire_records.values():
            # Re-merge intermediate keys of this wire that are in key_set.
            prev_entry: NodeEntry | None = None
            for key in wr.intermediate_keys:
                if key not in key_set:
                    continue
                entry = self._pad_index.get(key)
                if entry is None:
                    continue
                if prev_entry is not None and prev_entry.electric_set is not entry.electric_set:
                    self._merge(prev_entry.electric_set, entry.electric_set)
                prev_entry = entry

    def _remove_intermediate_nodes(self, record: WireRecord) -> None:
        """Remove wire-only (transient) nodes created during add_wire."""
        for key in record.intermediate_keys:
            entry = self._pad_index.pop(key, None)
            if entry is not None:
                eset = entry.electric_set
                eset._items = [e for e in eset._items if e is not entry]
                if not eset._items:
                    self._all_sets.discard(eset)
                else:
                    eset._recompute_canonical()

    # ------------------------------------------------------------------
    # Build from scratch
    # ------------------------------------------------------------------

    def _build(self, board: Board, layout: Layout) -> None:
        self._build_board_nodes(board)
        self._build_wire_edges(layout)
        self._build_component_internal_nets(layout)
        self._build_component_pin_map(layout)

    def _build_component_internal_nets(self, layout: Layout) -> None:
        """Merge electric sets of a component's same-name pins.

        Pins of one component that share a name (several ``GND``) are one node
        inside the part — a pre-routed internal net. We union their electric
        sets here so connecting one of them connects all, with no drawn wire.

        This is a *structural* merge (no WireRecord), exactly like the
        plated-through / strip merges in ``_build_board_nodes``: it is re-applied
        on every full rebuild (the GUI discards and rebuilds the graph on every
        layout change) and is never torn down by ``remove_wire`` (which only
        reverses wire merges).
        """
        for comp_id, placement in layout.placements.items():
            inst = self._instances.get(comp_id)
            if inst is None:
                continue
            defn = self._components.get(inst.definition_ref)
            if defn is None:
                continue
            fp = defn.footprint
            for pins in defn.pins_grouped_by_name(inst.pin_variant).values():
                anchor: NodeEntry | None = None
                for pin_num in pins:
                    if pin_num < 1 or pin_num > fp.pin_count():
                        continue
                    world_pos = pin_world_position(fp, placement, pin_num)
                    key: _NodeKey = _qkey(world_pos, placement.side)
                    entry = self._pad_index.get(key)
                    if entry is None:
                        entry = self._make_node(key)
                        self._board_pad_keys.add(key)
                    if anchor is None:
                        anchor = entry
                    elif anchor.electric_set is not entry.electric_set:
                        self._merge(anchor.electric_set, entry.electric_set)

    def _build_board_nodes(self, board: Board) -> None:
        for atom, pos in board.atoms():
            if isinstance(atom, PadAtom):
                layers = atom.layers
                if layers.plated_through:
                    top_key: _NodeKey = _qkey(pos, "top")
                    bot_key: _NodeKey = _qkey(pos, "bottom")
                    top_e = self._ensure_node(top_key)
                    bot_e = self._ensure_node(bot_key)
                    self._board_pad_keys.add(top_key)
                    self._board_pad_keys.add(bot_key)
                    if top_e.electric_set is not bot_e.electric_set:
                        self._merge(top_e.electric_set, bot_e.electric_set)
                else:
                    if layers.top:
                        k: _NodeKey = _qkey(pos, "top")
                        self._ensure_node(k)
                        self._board_pad_keys.add(k)
                    if layers.bottom:
                        k2: _NodeKey = _qkey(pos, "bottom")
                        self._ensure_node(k2)
                        self._board_pad_keys.add(k2)
            elif isinstance(atom, StripAtom):
                if atom.isolated:
                    continue
                self._build_strip_nodes(atom, pos)

    def _ensure_node(self, key: _NodeKey) -> NodeEntry:
        entry = self._pad_index.get(key)
        if entry is None:
            entry = self._make_node(key)
        return entry

    def _build_strip_nodes(self, atom: StripAtom, center: Point) -> None:
        layers = atom.layers
        n = int(atom.length) if isinstance(atom.length, int) else 1
        side: str = "top" if (layers.plated_through or layers.top) else "bottom"
        if atom.orientation == "horizontal":
            start_x = center.x - n / 2.0 + 0.5
            positions = [Point(start_x + i, center.y) for i in range(n)]
        else:
            start_y = center.y - n / 2.0 + 0.5
            positions = [Point(center.x, start_y + i) for i in range(n)]

        prev_entry: NodeEntry | None = None
        for pt in positions:
            key: _NodeKey = _qkey(pt, side)
            self._board_pad_keys.add(key)
            entry = self._ensure_node(key)
            if prev_entry is not None and prev_entry.electric_set is not entry.electric_set:
                self._merge(prev_entry.electric_set, entry.electric_set)
            prev_entry = entry

    def _build_wire_edges(self, layout: Layout) -> None:
        for wire in layout.wires:
            self.add_wire(wire)

    def _build_component_pin_map(self, layout: Layout) -> None:
        for comp_id, placement in layout.placements.items():
            inst = self._instances.get(comp_id)
            if inst is None:
                continue
            defn = self._components.get(inst.definition_ref)
            if defn is None:
                continue
            fp = defn.footprint
            for pin_num in range(1, fp.pin_count() + 1):
                world_pos = pin_world_position(fp, placement, pin_num)
                side = placement.side
                key: _NodeKey = _qkey(world_pos, side)
                entry = self._pad_index.get(key)
                if entry is None:
                    # Pin on a pad not in the board layout — create a stub.
                    entry = self._make_node(key)
                    self._board_pad_keys.add(key)
                ref = PinRef(component_id=comp_id, pin_number=pin_num)
                self._pin_index[ref] = entry
