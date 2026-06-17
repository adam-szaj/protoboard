# SPDX-License-Identifier: MIT
"""Tests for HighlightController — publishing Selectables to the dimming stack.

Phase 1 covers the auto-tier dimming side only (no QGraphicsItem overlay yet).
``HighlightStack`` is a ``QObject`` but constructs and mutates without a running
QApplication, so these stay light.
"""

from __future__ import annotations

import uuid

from protoboard.domain.layout.group import ComponentGroup
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import Wire
from protoboard.geometry import Point
from protoboard.gui.highlight import HighlightController
from protoboard.gui.highlight_state import HighlightStack
from protoboard.gui.interaction_mode import HighlightProfile
from protoboard.gui.selection import (
    ComponentSelectable,
    GroupSelectable,
    WireSelectable,
)


def _comp(cid: uuid.UUID | None = None) -> ComponentSelectable:
    cid = cid or uuid.uuid4()
    pl = ComponentPlacement(
        component_id=cid, position=Point(0.0, 0.0), rotation=0, side="top"
    )
    return ComponentSelectable(component_id=cid, placement=pl)


def _wire(wid: uuid.UUID | None = None) -> WireSelectable:
    wid = wid or uuid.uuid4()
    return WireSelectable(wire=Wire(id=wid, path=[Point(0.0, 0.0), Point(1.0, 0.0)]))


def _group(gid: uuid.UUID | None = None) -> GroupSelectable:
    gid = gid or uuid.uuid4()
    return GroupSelectable(group=ComponentGroup(id=gid, name="G"))


class TestHighlightController:
    def test_show_publishes_auto_entry_split_by_kind(self) -> None:
        stack = HighlightStack()
        hc = HighlightController(stack, source_id="select_tool")
        c = _comp()
        w = _wire()
        hc.show([c, w])
        entry = stack.active()
        assert entry is not None
        assert entry.auto is True
        assert entry.source_id == "select_tool"
        assert c.component_id in entry.comp_ids
        assert w.wire.id in entry.wire_ids
        assert len(entry.comp_ids) == 1
        assert len(entry.wire_ids) == 1

    def test_clear_removes_the_entry(self) -> None:
        stack = HighlightStack()
        hc = HighlightController(stack)
        hc.show([_comp()])
        assert stack.active() is not None
        hc.clear()
        assert stack.active() is None

    def test_empty_show_clears_instead_of_dimming_to_nothing(self) -> None:
        stack = HighlightStack()
        hc = HighlightController(stack)
        hc.show([_comp()])
        hc.show([])  # deselect
        assert stack.active() is None

    def test_show_replaces_previous_entry_same_source(self) -> None:
        stack = HighlightStack()
        hc = HighlightController(stack)
        c1, c2 = _comp(), _comp()
        hc.show([c1])
        hc.show([c2])
        entry = stack.active()
        assert entry is not None
        # Auto tier keeps at most one entry per source — c1 replaced by c2.
        assert entry.comp_ids == frozenset({c2.component_id})

    def test_group_selectable_contributes_no_own_id(self) -> None:
        stack = HighlightStack()
        hc = HighlightController(stack)
        # A bare group hit carries no comp/wire id; dimming comes from its
        # member components supplied separately.  With only a group + net,
        # net_id keeps the entry alive.
        gid = uuid.uuid4()
        hc.show([_group(gid)], net_id=uuid.uuid4())
        entry = stack.active()
        assert entry is not None
        assert entry.comp_ids == frozenset()
        assert entry.wire_ids == frozenset()
        assert entry.net_id is not None

    def test_net_id_kept_even_with_no_entities(self) -> None:
        stack = HighlightStack()
        hc = HighlightController(stack)
        net = uuid.uuid4()
        hc.show([], net_id=net)
        entry = stack.active()
        assert entry is not None
        assert entry.net_id == net

    def test_custom_profile_is_carried(self) -> None:
        stack = HighlightStack()
        hc = HighlightController(stack)
        prof = HighlightProfile(dim_components=0.1, dim_wires=0.1)
        hc.show([_comp()], profile=prof)
        entry = stack.active()
        assert entry is not None
        assert entry.profile.dim_components == 0.1

    def test_distinct_sources_coexist(self) -> None:
        stack = HighlightStack()
        a = HighlightController(stack, source_id="select_tool")
        b = HighlightController(stack, source_id="route_tool")
        a.show([_comp()])
        b.show([_comp()])
        # Both auto entries live; clearing one leaves the other.
        a.clear()
        assert stack.active() is not None  # b's entry survives
        b.clear()
        assert stack.active() is None

    def test_changed_signal_fires_on_show(self) -> None:
        stack = HighlightStack()
        hc = HighlightController(stack)
        fired: list[int] = []
        stack.changed.connect(lambda: fired.append(1))
        hc.show([_comp()])
        assert fired  # BoardView listens to this to re-apply opacity
