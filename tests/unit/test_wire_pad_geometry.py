# SPDX-License-Identifier: MIT
"""Compatibility of cached pad geometry and wire enrichment."""

from __future__ import annotations

import uuid
import weakref
from collections.abc import Iterator

import pytest

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.layout import wire_pad_geometry, wire_utils
from protoboard.domain.layout.wire import KnotRef, Wire
from protoboard.geometry import Point
from protoboard.instrumentation.call_counters import fresh_counters


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    wire_utils.clear_pad_bucket_cache()
    yield
    wire_utils.clear_pad_bucket_cache()


@pytest.mark.parametrize(
    "name",
    [
        "_PAD_BUCKET_CACHE",
        "_build_pad_bucket_index",
        "_get_pad_bucket_index",
        "board_pads_for_side",
        "board_pads_union",
        "clear_pad_bucket_cache",
        "enrich_insulated_fragments_with_pad_nodes",
        "enrich_insulated_wire_with_pad_nodes",
        "insert_pad_nodes",
        "plated_through_pads",
        "point_on_pads",
    ],
)
def test_compatibility_exports_share_implementation_and_cache(name: str) -> None:
    assert getattr(wire_utils, name) is getattr(wire_pad_geometry, name)


def test_pad_bucket_cache_reuses_identity_and_rebuilds_after_size_change() -> None:
    pads = {Point(1.5, 0.5)}
    first = wire_utils._get_pad_bucket_index(pads)
    cached = wire_utils._get_pad_bucket_index(pads)
    assert cached[0] is first[0]
    assert cached[1] is first[1]

    pads.add(Point(2.5, 0.5))
    rebuilt = wire_utils._get_pad_bucket_index(pads)
    assert rebuilt[0] is not first[0]
    assert set(rebuilt[1][round(0.5)]) == pads

    wire_utils.clear_pad_bucket_cache()
    assert not wire_utils._PAD_BUCKET_CACHE
    assert wire_utils._get_pad_bucket_index(pads)[0] is not rebuilt[0]


def test_pad_bucket_cache_rejects_entry_for_another_set() -> None:
    pads = {Point(1.5, 0.5)}
    old_pads = {Point(9.5, 9.5)}
    stale = wire_utils._get_pad_bucket_index(old_pads)
    # Simulate the same address being reused for a different set of equal size.
    wire_utils._PAD_BUCKET_CACHE[id(pads)] = (
        len(pads),
        weakref.ref(old_pads),
        stale,
    )
    rebuilt = wire_utils._get_pad_bucket_index(pads)
    assert rebuilt is not stale
    assert rebuilt[0] == {round(1.5): [Point(1.5, 0.5)]}


def test_pad_insertion_retains_counter_labels_and_cache_hit_counts() -> None:
    pads = {Point(1.5, 0.5)}
    path = [Point(0.5, 0.5), Point(2.5, 0.5)]
    with fresh_counters() as counts:
        wire_utils.insert_pad_nodes(path, pads)
        wire_utils.insert_pad_nodes(path, pads)
        assert counts == {
            "insert_pad_nodes": 2,
            "insert_pad_nodes._get_pad_bucket_index": 2,
            "insert_pad_nodes._get_pad_bucket_index._build_pad_bucket_index": 1,
        }


def _board() -> Board:
    return Board(
        root=BoardGrid(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            cell=PadAtom(
                width_policy=SizePolicy.HUG,
                height_policy=SizePolicy.HUG,
                layers=PadLayers(top=True, bottom=True, plated_through=True),
            ),
            rows=1,
            cols=3,
            row_gap=0,
            col_gap=0,
        )
    )


def test_board_pad_enumeration_keeps_cached_set_identity() -> None:
    board = _board()
    assert wire_utils.board_pads_union(board) is board.pad_positions_union()
    assert wire_utils.board_pads_for_side(board, "top") == board.pad_positions_for_side(
        "top"
    )
    assert wire_utils.plated_through_pads(board) == board.plated_through_positions()


def test_insulated_enrichment_preserves_metadata_and_is_idempotent() -> None:
    board = _board()
    wire = Wire(
        id=uuid.uuid4(),
        path=[Point(0.5, 0.5), Point(2.5, 0.5)],
        insulated=True,
        side="bottom",
        color="#123456",
        a=KnotRef(uuid.uuid4()),
        b=KnotRef(uuid.uuid4()),
    )
    enriched = wire_utils.enrich_insulated_wire_with_pad_nodes(wire, board)
    assert enriched is not wire
    assert enriched.path == [Point(0.5, 0.5), Point(1.5, 0.5), Point(2.5, 0.5)]
    assert wire.path == [Point(0.5, 0.5), Point(2.5, 0.5)]
    assert (enriched.id, enriched.insulated, enriched.side, enriched.color) == (
        wire.id,
        wire.insulated,
        wire.side,
        wire.color,
    )
    assert enriched.a == wire.a
    assert enriched.b == wire.b
    assert wire_utils.enrich_insulated_wire_with_pad_nodes(enriched, board) is enriched


def test_bare_wire_enrichment_preserves_input_identity() -> None:
    wire = Wire(
        id=uuid.uuid4(),
        path=[Point(0.5, 0.5), Point(2.5, 0.5)],
        insulated=False,
        side="top",
    )
    assert wire_utils.enrich_insulated_wire_with_pad_nodes(wire, _board()) is wire
