# SPDX-License-Identifier: MIT
"""Call-count regression tests for performance fixes.

These tests don't measure wall time (flaky on CI).  They count calls
to a known-hot function and assert an upper bound that only holds
when the relevant cache is in place.  If a future refactor accidentally
removes the cache, the call count balloons and the test fails with a
clear "this used to be O(1), now it's O(N)" message.

The three counters here pair with these fixes:

1. ``Board.atoms()`` caches its DFS walk (commit fixing #3).
2. ``_get_pad_bucket_index`` identity-caches its buckets (fix #1).
3. ``find_owned_wires`` memoises ``pin_world_position`` per call (fix #2).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.group import ComponentGroup
from protoboard.domain.layout.group_ownership import find_owned_wires
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import KnotRef, Wire
from protoboard.domain.layout.wire_utils import (
    board_pads_union,
    clear_pad_bucket_cache,
    insert_pad_nodes,
)
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point


def _board(rows: int = 20, cols: int = 20) -> Board:
    return Board(root=BoardGrid(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        ),
        rows=rows, cols=cols, row_gap=0.0, col_gap=0.0,
    ))


# ---------------------------------------------------------------------------
# Fix #3 — Board.atoms() caches its DFS walk
# ---------------------------------------------------------------------------


class TestBoardAtomsCached:
    def test_walk_runs_once_across_many_atoms_calls(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``board.atoms()`` should walk the box tree once and replay.

        Pre-fix, every call to ``board.atoms()`` re-recursed through
        the layout tree.  With caching the second + subsequent calls
        only invoke the underlying walker zero times.

        ``_walk_atoms`` recurses through the box tree, so counting raw
        invocations gives a large number even for one ``atoms()``
        call.  Instead, we measure invocation *deltas* between calls
        — the first call populates the cache (large delta), the
        follow-ups should add zero.
        """
        import protoboard.domain.board.board as board_mod
        original = board_mod._walk_atoms
        call_count = 0

        def counting_walk(node):  # type: ignore[no-untyped-def]
            nonlocal call_count
            call_count += 1
            yield from original(node)

        monkeypatch.setattr(board_mod, "_walk_atoms", counting_walk)
        # Fresh board to guarantee no pre-populated cache from another test.
        b = _board()
        list(b.atoms())
        after_first = call_count
        list(b.atoms())
        list(b.atoms())
        # Subsequent calls add nothing — cache hit returns iterator over
        # the materialised tuple without going through _walk_atoms.
        assert call_count == after_first, (
            f"_walk_atoms ran {call_count - after_first}× extra after the "
            "first board.atoms() call; expected 0 (cache regression)."
        )


# ---------------------------------------------------------------------------
# Fix #1 — _get_pad_bucket_index identity-caches buckets per pad-set
# ---------------------------------------------------------------------------


class TestPadBucketCached:
    def test_buckets_rebuilt_once_per_unique_pad_set(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """For a stable pad-set identity, the bucket dicts should be
        built once and reused across all subsequent ``insert_pad_nodes``
        calls.
        """
        import protoboard.domain.layout.wire_utils as wu
        clear_pad_bucket_cache()
        # Wrap to count cache misses (i.e. actual dict rebuilds).
        original = wu._get_pad_bucket_index
        miss_count = 0

        def counting(pad_positions):  # type: ignore[no-untyped-def]
            nonlocal miss_count
            cached = wu._PAD_BUCKET_CACHE.get(id(pad_positions))
            if cached is None or cached[0] != len(pad_positions):
                miss_count += 1
            return original(pad_positions)

        monkeypatch.setattr(wu, "_get_pad_bucket_index", counting)

        b = _board()
        pads = board_pads_union(b)
        # Run insert_pad_nodes on N different paths using the SAME pad-set.
        for i in range(20):
            insert_pad_nodes(
                [Point(1.0, float(i) + 0.5), Point(10.0, float(i) + 0.5)],
                pads,
            )
        assert miss_count == 1, (
            f"_get_pad_bucket_index missed cache {miss_count}× for 20 calls "
            "with the same pad-set identity; expected exactly 1 build."
        )

    def test_different_pad_set_identities_rebuild(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two distinct pad-set objects must each trigger a build —
        identity-cache must NOT alias across set instances even when
        the contents are equal.
        """
        import protoboard.domain.layout.wire_utils as wu
        clear_pad_bucket_cache()
        miss_count = 0
        original = wu._get_pad_bucket_index

        def counting(pad_positions):  # type: ignore[no-untyped-def]
            nonlocal miss_count
            cached = wu._PAD_BUCKET_CACHE.get(id(pad_positions))
            if cached is None or cached[0] != len(pad_positions):
                miss_count += 1
            return original(pad_positions)

        monkeypatch.setattr(wu, "_get_pad_bucket_index", counting)

        # Two independent set instances with the same contents.
        pads_a = {Point(1.5, 0.5), Point(2.5, 0.5), Point(3.5, 0.5)}
        pads_b = {Point(1.5, 0.5), Point(2.5, 0.5), Point(3.5, 0.5)}
        insert_pad_nodes([Point(1.0, 0.5), Point(4.0, 0.5)], pads_a)
        insert_pad_nodes([Point(1.0, 0.5), Point(4.0, 0.5)], pads_b)
        # Both build, because identity differs.
        assert miss_count == 2

    def test_size_change_invalidates_cache(self) -> None:
        """If the pad-set's len changes between calls (in-place add/remove),
        the cached buckets must rebuild.  Guards against the test-time
        regression that motivated removing the cache originally.
        """
        clear_pad_bucket_cache()
        pads = {Point(1.5, 0.5), Point(2.5, 0.5)}
        # First call builds.
        out1 = insert_pad_nodes([Point(1.0, 0.5), Point(3.0, 0.5)], pads)
        # Mutate in-place.
        pads.add(Point(2.5, 0.5))  # no-op (already there) — length unchanged
        out2 = insert_pad_nodes([Point(1.0, 0.5), Point(3.0, 0.5)], pads)
        # Same length so cache reused; results match.
        assert out1 == out2
        # Now actually grow.
        pads.add(Point(10.5, 0.5))
        # Should still work (and rebuild buckets transparently).
        out3 = insert_pad_nodes([Point(1.0, 0.5), Point(11.0, 0.5)], pads)
        # The new pad at 10.5 should be picked up.
        assert Point(10.5, 0.5) in out3


# ---------------------------------------------------------------------------
# Fix #2 — find_owned_wires memoises pin_world_position per call
# ---------------------------------------------------------------------------


@dataclass
class _Ctx:
    layout: Layout
    instances: dict[uuid.UUID, ComponentInstance] = field(default_factory=dict)
    components: dict[str, ComponentDefinition] = field(default_factory=dict)
    schematic: Schematic = field(default_factory=lambda: Schematic(nets={}))


def _resistor_defn() -> ComponentDefinition:
    return ComponentDefinition.from_footprint(name="R", footprint=SIL(2), pin_names={1: "A", 2: "B"})


def _place(ctx: _Ctx, x: float, y: float, refdes: str) -> uuid.UUID:
    inst = ComponentInstance.new(definition_ref="R", refdes=refdes, value="10k")
    ctx.instances[inst.id] = inst
    ctx.layout.placements[inst.id] = ComponentPlacement(
        component_id=inst.id, position=Point(x, y), rotation=0, side="top",
    )
    return inst.id


def _pin_pos(ctx: _Ctx, cid: uuid.UUID, pin: int) -> Point:
    from protoboard.domain.layout.placement import pin_world_position
    return pin_world_position(
        ctx.components["R"].default_footprint(), ctx.layout.placements[cid], pin,
    )


class TestFindOwnedWiresMemoisesPinWorldPos:
    def test_pin_world_position_resolved_once_per_pin(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With N wires fan-out from one pin, ``pin_world_position``
        should be called once for that pin, not N times.

        Pre-fix the geometric sanity check inside
        ``_pin_endpoint_matches_path`` resolved the pin world position
        per wire endpoint.  Now the resolution is cached for the
        lifetime of ``find_owned_wires``.
        """
        import protoboard.domain.layout.group_ownership as go_mod
        call_args: list[tuple[uuid.UUID, int]] = []

        # Patch the import target — group_ownership re-imports inside
        # _pin_endpoint_matches_path with a local-scope from-import.
        # That means we have to monkeypatch the source module.
        import protoboard.domain.layout.placement as plc_mod
        original = plc_mod.pin_world_position

        def counting(footprint, placement, pin_num):  # type: ignore[no-untyped-def]
            call_args.append((placement.component_id, pin_num))
            return original(footprint, placement, pin_num)

        monkeypatch.setattr(plc_mod, "pin_world_position", counting)

        ctx = _Ctx(layout=Layout(), components={"R": _resistor_defn()})
        ctx.layout.ensure_root_group()
        cid_a = _place(ctx, 5.0, 5.0, "R1")
        # Fan-out: 5 wires from pin a/1 to 5 different stationary peers.
        peers: list[uuid.UUID] = []
        for i in range(5):
            peers.append(_place(ctx, 10.0 + i, 5.0, f"R{i+2}"))
        pka = ctx.layout.get_or_create_pin_knot(PinRef(cid_a, 1))
        for peer in peers:
            pkp = ctx.layout.get_or_create_pin_knot(PinRef(peer, 1))
            w = Wire(
                id=uuid.uuid4(),
                path=[_pin_pos(ctx, cid_a, 1), _pin_pos(ctx, peer, 1)],
                side="top",
                a=KnotRef(id=pka.id), b=KnotRef(id=pkp.id),
            )
            ctx.layout.add_wire(w)
        # Net + group containing only the fan-out pin's component.
        net = Net(id=uuid.uuid4(), name="N1", pins={PinRef(cid_a, 1)})
        ctx.schematic.nets[net.id] = net
        group = ComponentGroup(
            id=uuid.uuid4(), name="G",
            component_ids=frozenset({cid_a}),
        )

        # Reset counter so the test only measures the find_owned_wires call.
        call_args.clear()
        find_owned_wires(
            list(ctx.layout.wires), group,
            wire_net_map={w.id: net.id for w in ctx.layout.wires},
            knots=ctx.layout.knots,
            group_comp_ids=frozenset({cid_a}),
            schematic=ctx.schematic,
            placements=ctx.layout.placements,
            instances=ctx.instances,
            definitions=ctx.components,
        )
        # Count distinct (component, pin) keys resolved.
        unique_pins = set(call_args)
        # Without cache: 5 wires × 2 endpoints × 1 pin = up to 10 calls,
        # one per endpoint-check.  With cache: ≤ |unique_pins| calls.
        # That set is at most 6 here (cid_a/1 + each peer/1).
        assert len(call_args) <= len(unique_pins), (
            f"pin_world_position called {len(call_args)}× for "
            f"{len(unique_pins)} distinct pins; cache regression."
        )


# ---------------------------------------------------------------------------
# Sanity: the helpers themselves remain correct after caching
# ---------------------------------------------------------------------------


class TestCachesProduceCorrectResults:
    def test_atoms_cache_yields_same_items_each_call(self) -> None:
        b = _board(rows=5, cols=5)
        first = list(b.atoms())
        second = list(b.atoms())
        third = list(b.atoms())
        assert first == second == third
        # And the count is sensible (25 PadAtom centres for a 5×5 board).
        assert len(first) == 25

    def test_pad_bucket_cache_returns_same_dicts_for_same_input(self) -> None:
        clear_pad_bucket_cache()
        from protoboard.domain.layout.wire_utils import _get_pad_bucket_index
        b = _board(rows=4, cols=4)
        pads = board_pads_union(b)
        bx1, by1 = _get_pad_bucket_index(pads)
        bx2, by2 = _get_pad_bucket_index(pads)
        # Identity-cached → same dict object returned.
        assert bx1 is bx2
        assert by1 is by2
