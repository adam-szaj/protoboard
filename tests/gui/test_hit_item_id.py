# SPDX-License-Identifier: MIT
"""``Hit.item_id()`` — the single ``Hit`` → ``ItemId`` bridge.

The GUI splitters that used to route ``Hit.kind`` (a string) into typed id
buckets by hand now all go through this one helper: it maps each of the three
``Hit`` kinds onto its ``ItemTag`` and returns ``None`` for the non-UUID ids
test stand-ins use (the exact drop the splitters relied on).
"""

from __future__ import annotations

import uuid

from protoboard.controller.selection_fsm import Hit
from protoboard.controller.editing.item_handle import ItemId, ItemTag


def test_component_hit_maps_to_component_item_id() -> None:
    cid = uuid.uuid4()
    assert Hit("component", cid).item_id() == ItemId(ItemTag.component, cid)


def test_wire_hit_maps_to_wire_item_id() -> None:
    wid = uuid.uuid4()
    assert Hit("wire", wid).item_id() == ItemId(ItemTag.wire, wid)


def test_group_hit_maps_to_group_item_id() -> None:
    gid = uuid.uuid4()
    assert Hit("group", gid).item_id() == ItemId(ItemTag.group, gid)


def test_non_uuid_id_yields_none() -> None:
    """String stand-in ids (tests) carry no real identity → dropped as None."""
    assert Hit("component", "a1").item_id() is None


def test_pin_number_metadata_does_not_affect_item_id() -> None:
    """The pin the hit landed on is metadata, not identity — same ItemId."""
    cid = uuid.uuid4()
    assert Hit("component", cid, pin_number=3).item_id() == ItemId(
        ItemTag.component, cid
    )
