# SPDX-License-Identifier: MIT
"""Tests for LayoutSnapshot / SchematicSnapshot / RouteRequest."""

from __future__ import annotations

import uuid

import pytest

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import Wire
from protoboard.domain.routing_settings import RoutingSettings
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from protoboard.services.routing.snapshot import (
    LayoutSnapshot,
    RouteRequest,
    SchematicSnapshot,
)


def _perfboard(rows: int = 4, cols: int = 4) -> Board:
    grid = BoardGrid(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        ),
        rows=rows,
        cols=cols,
        row_gap=0.0,
        col_gap=0.0,
    )
    return Board(root=grid)


def _wire(p0: Point, p1: Point, side: str = "top") -> Wire:
    return Wire(
        id=uuid.uuid4(),
        path=[p0, p1],
        insulated=True,
        side=side,  # type: ignore[arg-type]
    )


class TestLayoutSnapshot:
    def test_freeze_captures_wires(self) -> None:
        layout = Layout()
        w = _wire(Point(0.5, 0.5), Point(1.5, 0.5))
        layout.wires.append(w)
        snap = LayoutSnapshot.freeze(layout)
        assert snap.wires == (w,)

    def test_freeze_disconnects_from_live_layout_wires(self) -> None:
        """Mutating layout.wires after freeze must not affect the snapshot."""
        layout = Layout()
        w1 = _wire(Point(0.5, 0.5), Point(1.5, 0.5))
        layout.wires.append(w1)
        snap = LayoutSnapshot.freeze(layout)

        # Live mutation should be invisible to the snapshot.
        w2 = _wire(Point(2.5, 0.5), Point(3.5, 0.5))
        layout.wires.append(w2)
        layout.wires.remove(w1)

        assert snap.wires == (w1,), "snapshot must remain stable across live edits"

    def test_freeze_disconnects_from_live_placements(self) -> None:
        layout = Layout()
        cid = uuid.uuid4()
        snap = LayoutSnapshot.freeze(layout)
        layout.placements[cid] = ComponentPlacement(
            component_id=cid, position=Point(1, 1), rotation=0, side="top",
        )
        assert cid not in snap.placements

    def test_placements_mapping_rejects_mutation(self) -> None:
        layout = Layout()
        snap = LayoutSnapshot.freeze(layout)
        cid = uuid.uuid4()
        with pytest.raises(TypeError):
            snap.placements[cid] = ComponentPlacement(  # type: ignore[index]
                component_id=cid, position=Point(0, 0), rotation=0, side="top",
            )

    def test_to_layout_returns_independent_copy(self) -> None:
        layout = Layout()
        layout.wires.append(_wire(Point(0.5, 0.5), Point(1.5, 0.5)))
        snap = LayoutSnapshot.freeze(layout)
        derived = snap.to_layout()

        derived.wires.clear()
        assert len(snap.wires) == 1, "snapshot survives mutation of the derived copy"

    def test_to_layout_isolated_from_session_layout(self) -> None:
        layout = Layout()
        layout.wires.append(_wire(Point(0.5, 0.5), Point(1.5, 0.5)))
        snap = LayoutSnapshot.freeze(layout)
        derived = snap.to_layout()
        derived.wires.append(_wire(Point(2.5, 0.5), Point(3.5, 0.5)))

        # The original session layout must not have grown.
        assert len(layout.wires) == 1


class TestSchematicSnapshot:
    def test_freeze_disconnects_net_pins(self) -> None:
        cid = uuid.uuid4()
        net = Net(id=uuid.uuid4(), name="N1", pins={PinRef(cid, 1)})
        sch = Schematic(nets={net.id: net})

        snap = SchematicSnapshot.freeze(sch)

        # Mutating the live net's pin set must not be visible.
        net.pins.add(PinRef(cid, 2))

        snap_net = snap.nets[net.id]
        assert snap_net.pins == {PinRef(cid, 1)}

    def test_freeze_disconnects_nets_dict(self) -> None:
        sch = Schematic(nets={})
        snap = SchematicSnapshot.freeze(sch)
        nid = uuid.uuid4()
        sch.nets[nid] = Net(id=nid, name="new", pins=set())
        assert nid not in snap.nets

    def test_nets_mapping_rejects_mutation(self) -> None:
        snap = SchematicSnapshot.freeze(Schematic(nets={}))
        nid = uuid.uuid4()
        with pytest.raises(TypeError):
            snap.nets[nid] = Net(id=nid, name="x", pins=set())  # type: ignore[index]

    def test_to_schematic_returns_independent_copy(self) -> None:
        sch = Schematic(nets={
            (nid := uuid.uuid4()): Net(id=nid, name="N1", pins=set()),
        })
        snap = SchematicSnapshot.freeze(sch)
        derived = snap.to_schematic()
        derived.nets.clear()
        assert len(snap.nets) == 1


class TestRouteRequest:
    def test_from_session_freezes_all_state(self) -> None:
        board = _perfboard()
        layout = Layout()
        layout.wires.append(_wire(Point(0.5, 0.5), Point(1.5, 0.5)))
        nid = uuid.uuid4()
        sch = Schematic(nets={nid: Net(id=nid, name="N1", pins=set())})

        req = RouteRequest.from_session(
            board=board,
            layout=layout,
            schematic=sch,
            components={},
            instances={},
            routing_settings=RoutingSettings(),
        )

        # Live mutation invisible to the request.
        layout.wires.clear()
        sch.nets.clear()

        assert len(req.layout.wires) == 1
        assert nid in req.schematic.nets

    def test_request_is_frozen(self) -> None:
        req = RouteRequest.from_session(
            board=_perfboard(),
            layout=Layout(),
            schematic=Schematic(nets={}),
            components={},
            instances={},
            routing_settings=RoutingSettings(),
        )
        with pytest.raises(Exception):  # FrozenInstanceError
            req.side = "bottom"  # type: ignore[misc]

    def test_components_and_instances_are_proxies(self) -> None:
        req = RouteRequest.from_session(
            board=_perfboard(),
            layout=Layout(),
            schematic=Schematic(nets={}),
            components={},
            instances={},
            routing_settings=RoutingSettings(),
        )
        with pytest.raises(TypeError):
            req.components["x"] = None  # type: ignore[index,assignment]
        with pytest.raises(TypeError):
            req.instances[uuid.uuid4()] = None  # type: ignore[index,assignment]

    def test_net_ids_is_frozenset(self) -> None:
        req = RouteRequest.from_session(
            board=_perfboard(),
            layout=Layout(),
            schematic=Schematic(nets={}),
            components={},
            instances={},
            routing_settings=RoutingSettings(),
            net_ids={uuid.uuid4()},
        )
        assert isinstance(req.net_ids, frozenset)

    def test_net_ids_none_passes_through(self) -> None:
        req = RouteRequest.from_session(
            board=_perfboard(),
            layout=Layout(),
            schematic=Schematic(nets={}),
            components={},
            instances={},
            routing_settings=RoutingSettings(),
            net_ids=None,
        )
        assert req.net_ids is None
