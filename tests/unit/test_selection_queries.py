# SPDX-License-Identifier: MIT
"""Tests for the pure net/connectivity selection queries (``queries.py``).

Drives :func:`relation_correlated` / :func:`source_net_ids` with fake ConnQuery
/ NetSource (no session, no Qt), then a round-trip over the real session-backed
:class:`SessionConnQuery` / :class:`SessionNetSource` adapters.  Ports the intent
of the legacy ``_resolve_relation`` parity assertions.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest

from protoboard.gui.selection.queries import (
    RelationSource,
    SessionConnQuery,
    SessionNetSource,
    relation_correlated,
    source_net_ids,
)
from protoboard.gui.selection_axes import RelationClass


@dataclass
class _FakeConn:
    comp_nets: dict[uuid.UUID, frozenset[uuid.UUID]] = field(default_factory=dict)
    pin_nets: dict[tuple[uuid.UUID, int], frozenset[uuid.UUID]] = field(
        default_factory=dict
    )
    wire_net: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)

    def nets_for_component(self, component_id: object) -> frozenset[uuid.UUID]:
        return self.comp_nets.get(component_id, frozenset())  # type: ignore[arg-type]

    def nets_for_pin(
        self, component_id: object, pin_number: int
    ) -> frozenset[uuid.UUID]:
        return self.pin_nets.get((component_id, pin_number), frozenset())  # type: ignore[arg-type]

    def net_for_wire(self, wire_id: object) -> uuid.UUID | None:
        return self.wire_net.get(wire_id)  # type: ignore[arg-type]


@dataclass
class _FakeNets:
    members: dict[uuid.UUID, frozenset[uuid.UUID]] = field(default_factory=dict)
    wires: dict[uuid.UUID, frozenset[uuid.UUID]] = field(default_factory=dict)

    def net_component_ids(self, net_id: uuid.UUID) -> frozenset[uuid.UUID]:
        return self.members.get(net_id, frozenset())

    def wire_ids_on_net(self, net_id: uuid.UUID) -> frozenset[uuid.UUID]:
        return self.wires.get(net_id, frozenset())


class TestSourceNetIds:
    def test_pin_source_one_net(self) -> None:
        c, n = uuid.uuid4(), uuid.uuid4()
        conn = _FakeConn(pin_nets={(c, 1): frozenset({n})})
        assert source_net_ids(RelationSource.pin(c, 1), conn) == frozenset({n})

    def test_wire_source_one_net(self) -> None:
        w, n = uuid.uuid4(), uuid.uuid4()
        conn = _FakeConn(wire_net={w: n})
        assert source_net_ids(RelationSource.wire(w), conn) == frozenset({n})

    def test_component_bbox_unions_all_nets(self) -> None:
        c, n1, n2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        conn = _FakeConn(comp_nets={c: frozenset({n1, n2})})
        assert source_net_ids(RelationSource.component(c), conn) == frozenset({n1, n2})

    def test_unbound_wire_yields_empty(self) -> None:
        conn = _FakeConn()
        assert source_net_ids(RelationSource.wire(uuid.uuid4()), conn) == frozenset()


class TestRelationCorrelated:
    def _two_comp_net(self):
        c1, c2, n = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        w = uuid.uuid4()
        conn = _FakeConn(
            comp_nets={c1: frozenset({n}), c2: frozenset({n})},
            pin_nets={(c1, 1): frozenset({n})},
            wire_net={w: n},
        )
        nets = _FakeNets(members={n: frozenset({c1, c2})}, wires={n: frozenset({w})})
        return c1, c2, n, w, conn, nets

    def test_none_correlates_nothing(self) -> None:
        c1, c2, n, w, conn, nets = self._two_comp_net()
        comps, wires = relation_correlated(
            RelationClass.NONE, RelationSource.component(c1), conn, nets
        )
        assert comps == frozenset() and wires == frozenset()

    def test_same_logical_net_includes_both_comps_and_wire(self) -> None:
        c1, c2, n, w, conn, nets = self._two_comp_net()
        comps, wires = relation_correlated(
            RelationClass.SAME_LOGICAL_NET, RelationSource.pin(c1, 1), conn, nets
        )
        assert {c1, c2} <= comps
        assert w in wires

    def test_clicked_object_always_seeded(self) -> None:
        # A component with no resolvable net still includes itself.
        c = uuid.uuid4()
        conn, nets = _FakeConn(), _FakeNets()
        comps, _ = relation_correlated(
            RelationClass.SAME_LOGICAL_NET, RelationSource.component(c), conn, nets
        )
        assert c in comps

    def test_clicked_wire_always_seeded(self) -> None:
        w = uuid.uuid4()
        conn, nets = _FakeConn(), _FakeNets()
        _, wires = relation_correlated(
            RelationClass.SAME_LOGICAL_NET, RelationSource.wire(w), conn, nets
        )
        assert w in wires

    def test_neighbors_expands_comps_not_wires(self) -> None:
        c1, c2, n, w, conn, nets = self._two_comp_net()
        comps, wires = relation_correlated(
            RelationClass.NEIGHBORS, RelationSource.component(c1), conn, nets
        )
        assert {c1, c2} <= comps
        assert wires == frozenset()  # NEIGHBORS never expands wires

    def test_unconnected_pins_behaves_like_logical_for_the_set(self) -> None:
        # The unconnected-vs-connected split is a render concern; the correlated
        # set matches SAME_LOGICAL_NET.
        c1, c2, n, w, conn, nets = self._two_comp_net()
        log = relation_correlated(
            RelationClass.SAME_LOGICAL_NET, RelationSource.pin(c1, 1), conn, nets
        )
        unc = relation_correlated(
            RelationClass.UNCONNECTED_PINS, RelationSource.pin(c1, 1), conn, nets
        )
        assert log == unc


class TestSessionAdapters:
    def test_session_conn_and_net_source_round_trip(self, qtbot) -> None:
        from pathlib import Path

        from protoboard.domain.board.board import Board
        from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
        from protoboard.domain.board.pads import PadLayers
        from protoboard.domain.components.definition import ComponentDefinition
        from protoboard.domain.components.footprints import SIL
        from protoboard.domain.components.instance import ComponentInstance
        from protoboard.domain.layout.placement import ComponentPlacement
        from protoboard.domain.schematic.net import Net, PinRef
        from protoboard.geometry import Point
        from protoboard.gui.session import EditorSession
        from protoboard.services.library.loader import load_library

        grid = BoardGrid(
            width_policy=SizePolicy.HUG, height_policy=SizePolicy.HUG,
            cell=PadAtom(
                width_policy=SizePolicy.HUG, height_policy=SizePolicy.HUG,
                layers=PadLayers(top=True, bottom=True, plated_through=True),
            ),
            rows=8, cols=8, row_gap=0.0, col_gap=0.0,
        )
        lib = load_library([Path(__file__).parent.parent / "fixtures" / "library"])
        sess = EditorSession(board=Board(root=grid), library=lib)

        defn = ComponentDefinition.from_footprint(name="S1", footprint=SIL(1), pin_names={1: "P"})
        sess.components["S1"] = defn
        i1 = ComponentInstance.new(definition_ref="S1", refdes="X1", value="")
        i2 = ComponentInstance.new(definition_ref="S1", refdes="X2", value="")
        for inst, x in ((i1, 1.5), (i2, 3.5)):
            sess.instances[inst.id] = inst
            sess.layout.placements[inst.id] = ComponentPlacement(
                component_id=inst.id, position=Point(x, 1.5), rotation=0, side="top",
            )
        net = Net(
            id=uuid.uuid4(), name="N1",
            pins={PinRef(i1.id, 1), PinRef(i2.id, 1)},
        )
        sess.schematic.nets[net.id] = net
        sess.invalidate_graph()

        conn = SessionConnQuery(sess)
        nets = SessionNetSource(sess)
        assert conn.nets_for_pin(i1.id, 1) == frozenset({net.id})
        assert conn.nets_for_component(i1.id) == frozenset({net.id})
        assert nets.net_component_ids(net.id) == frozenset({i1.id, i2.id})

        comps, _ = relation_correlated(
            RelationClass.SAME_LOGICAL_NET, RelationSource.pin(i1.id, 1), conn, nets
        )
        assert {i1.id, i2.id} <= comps


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
