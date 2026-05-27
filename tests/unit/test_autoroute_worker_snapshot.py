# SPDX-License-Identifier: MIT
"""AutorouteWorker must snapshot its inputs at construction.

Regression for the data race where ``session.layout`` / ``session.schematic``
were aliased into the worker — if the user moved a component or renamed a
net while autoroute was in flight, the worker read torn state.
"""

from __future__ import annotations

import uuid

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.wire import Wire
from protoboard.domain.routing_settings import RoutingSettings
from protoboard.domain.schematic.net import Net
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from protoboard.gui.workers.autoroute_worker import AutorouteWorker
from protoboard.services.routing.snapshot import RouteRequest


def _perfboard() -> Board:
    grid = BoardGrid(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        ),
        rows=4,
        cols=4,
        row_gap=0.0,
        col_gap=0.0,
    )
    return Board(root=grid)


def _wire(p0: Point, p1: Point) -> Wire:
    return Wire(
        id=uuid.uuid4(),
        path=[p0, p1],
        insulated=True,
        side="top",
    )


class TestAutorouteWorkerSnapshot:
    def test_legacy_kwargs_snapshot_at_construction(self, qtbot) -> None:
        """Constructing the worker with legacy kwargs must take a snapshot.

        After construction, mutating the live ``Layout.wires`` must not be
        visible from the worker's internal request.
        """
        layout = Layout()
        layout.wires.append(_wire(Point(0.5, 0.5), Point(1.5, 0.5)))
        nid = uuid.uuid4()
        sch = Schematic(nets={nid: Net(id=nid, name="N1", pins=set())})

        worker = AutorouteWorker(
            board=_perfboard(),
            layout=layout,
            schematic=sch,
            components={},
            instances={},
            routing_settings=RoutingSettings(),
        )
        qtbot.addWidget  # ensure pytest-qt is active (no actual widget — worker is QThread)

        # Mutate live state after construction — worker must not see it.
        layout.wires.clear()
        sch.nets.clear()

        snap_layout = worker._request.layout
        snap_sch = worker._request.schematic
        assert len(snap_layout.wires) == 1
        assert nid in snap_sch.nets

    def test_explicit_request_pass_through(self, qtbot) -> None:
        layout = Layout()
        sch = Schematic(nets={})
        req = RouteRequest.from_session(
            board=_perfboard(),
            layout=layout,
            schematic=sch,
            components={},
            instances={},
            routing_settings=RoutingSettings(),
        )
        worker = AutorouteWorker(request=req)
        qtbot.addWidget
        assert worker._request is req

    def test_missing_inputs_raises(self, qtbot) -> None:
        qtbot.addWidget
        import pytest as _pt
        with _pt.raises(TypeError):
            AutorouteWorker(board=_perfboard())  # missing the rest

    def test_net_ids_independent_of_caller_set(self, qtbot) -> None:
        """Mutating the caller's net_ids set must not affect the worker."""
        layout = Layout()
        sch = Schematic(nets={})
        caller_set = {uuid.uuid4(), uuid.uuid4()}
        worker = AutorouteWorker(
            board=_perfboard(),
            layout=layout,
            schematic=sch,
            components={},
            instances={},
            routing_settings=RoutingSettings(),
            net_ids=caller_set,
        )
        qtbot.addWidget
        caller_set.clear()
        assert worker._request.net_ids is not None
        assert len(worker._request.net_ids) == 2
