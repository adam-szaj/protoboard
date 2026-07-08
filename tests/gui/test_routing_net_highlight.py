# SPDX-License-Identifier: MIT
"""Routing net highlight — pads/wires of the in-progress net stand out.

When RouteTool calls ``session.set_routing_net(net.id)``, the board view
must treat that net as the highlight target so glow rings on pads and
wires are rebuilt.  Previously only the Net Inspector's
``_highlighted_net_id`` drove the glow ring; the routing net only
affected opacity, which can't add or remove a glow item.
"""

from __future__ import annotations

import uuid

from protoboard.domain.schematic.net import Net
from protoboard.gui.board_view import BoardView
from protoboard.gui.session import EditorSession


class TestEffectiveHighlightedNet:
    def test_routing_net_wins_over_legacy(self, board_view: BoardView, session: EditorSession) -> None:
        legacy = uuid.uuid4()
        routing = uuid.uuid4()
        board_view.highlight._highlighted_net_id = legacy
        session.set_routing_net(routing)
        assert board_view.highlight.effective_highlighted_net() == routing

    def test_legacy_used_when_no_routing(self, board_view: BoardView, session: EditorSession) -> None:
        legacy = uuid.uuid4()
        board_view.highlight._highlighted_net_id = legacy
        # Sanity — no routing net set.
        assert session.routing_net_id is None
        assert board_view.highlight.effective_highlighted_net() == legacy

    def test_none_when_neither_set(self, board_view: BoardView, session: EditorSession) -> None:
        assert session.routing_net_id is None
        assert board_view.highlight._highlighted_net_id is None
        assert board_view.highlight.effective_highlighted_net() is None


class TestRoutingNetTriggersRefresh:
    def test_routing_net_change_clears_fingerprints(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """A change in routing_net_id must invalidate the cached fingerprints
        so the next refresh rebuilds component/wire groups (and their glow
        rings).  The fingerprint-cache convention deliberately excludes
        highlight state, so the slot has to clear the cache itself."""
        # Pre-populate fingerprint caches with arbitrary entries.
        cid = uuid.uuid4()
        wid = uuid.uuid4()
        board_view.composer._comp_fingerprints[cid] = ("stale",)
        board_view.composer._wire_fingerprints[wid] = ("stale",)

        nid = uuid.uuid4()
        session.schematic.nets[nid] = Net(id=nid, name="NET1")
        session.set_routing_net(nid)

        # Both caches must be empty so the next refresh rebuilds groups.
        assert board_view.composer._comp_fingerprints == {}
        assert board_view.composer._wire_fingerprints == {}

    def test_clearing_routing_net_also_clears_caches(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        nid = uuid.uuid4()
        session.schematic.nets[nid] = Net(id=nid, name="NET1")
        session.set_routing_net(nid)

        # Repopulate after the first event.
        cid = uuid.uuid4()
        board_view.composer._comp_fingerprints[cid] = ("stale",)

        session.set_routing_net(None)
        assert board_view.composer._comp_fingerprints == {}
