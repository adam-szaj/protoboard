# SPDX-License-Identifier: MIT
"""EditController.apply_autoroute / run_drc / autoroute — headless (no Qt).

apply_autoroute absorbs the duplicate-filter + macro half of
MainWindow._on_autoroute_done; run_drc / autoroute are the synchronous
counterparts to the GUI's background workers, for the CLI. All exercised
directly on a :class:`HeadlessSession`.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, PadLayers, SizePolicy
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import Wire
from protoboard.domain.routing_settings import RoutingSettings
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.geometry import Point
from protoboard.headless_session import HeadlessSession
from protoboard.services.library.loader import load_library
from protoboard.services.routing.autorouter import AutorouteResult

_LIBRARY_DIR = Path(__file__).parent.parent / "fixtures" / "library"


def _perfboard(rows: int = 8, cols: int = 8) -> Board:
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


def _session() -> HeadlessSession:
    library = load_library([_LIBRARY_DIR])
    return HeadlessSession(board=_perfboard(), library=library)


def _place(session: HeadlessSession, x: float, y: float, refdes: str) -> uuid.UUID:
    defn = session.components["R"]
    inst = ComponentInstance.new(
        definition_ref="R",
        refdes=refdes,
        value="",
    ).bind(defn, session.package_families)
    session.instances[inst.id] = inst
    session.layout.placements[inst.id] = ComponentPlacement(
        component_id=inst.id,
        position=Point(x, y),
        rotation=0,
        side="top",
    )
    session.invalidate_graph()
    return inst.id


def _wire(a: Point, b: Point) -> Wire:
    return Wire(id=uuid.uuid4(), path=[a, b], insulated=True, side="top")


# -- apply_autoroute --------------------------------------------------------


def test_apply_autoroute_commits_wires() -> None:
    session = _session()
    result = AutorouteResult(
        wires=[_wire(Point(1, 1), Point(3, 1))],
        unrouted_nets=[],
    )
    applied = session.edit_controller.apply_autoroute(result)
    assert applied == 1
    assert len(session.layout.wires) == 1


def test_apply_autoroute_empty_result_returns_zero() -> None:
    session = _session()
    result = AutorouteResult(wires=[], unrouted_nets=["NetA"])
    assert session.edit_controller.apply_autoroute(result) == 0
    assert len(session.layout.wires) == 0


def test_apply_autoroute_filters_intra_result_duplicates() -> None:
    """Two result wires with the same connection collapse to one commit."""
    session = _session()
    result = AutorouteResult(
        wires=[
            _wire(Point(1, 1), Point(3, 1)),
            _wire(Point(1, 1), Point(3, 1)),  # same path/side/insulated → dup
        ],
        unrouted_nets=[],
    )
    applied = session.edit_controller.apply_autoroute(result)
    assert applied == 1
    assert len(session.layout.wires) == 1


def test_apply_autoroute_undo_removes_the_macro() -> None:
    session = _session()
    result = AutorouteResult(
        wires=[_wire(Point(1, 1), Point(3, 1)), _wire(Point(1, 3), Point(3, 3))],
        unrouted_nets=[],
    )
    applied = session.edit_controller.apply_autoroute(result)
    assert applied == 2 and len(session.layout.wires) == 2
    # One macro on the stack → one undo clears both wires.
    session.stack.undo(session)  # type: ignore[arg-type]
    assert len(session.layout.wires) == 0


# -- run_drc ----------------------------------------------------------------


def test_run_drc_returns_issue_list() -> None:
    session = _session()
    _place(session, 1.0, 1.0, "R1")
    issues = session.edit_controller.run_drc()
    assert isinstance(issues, list)


# -- autoroute (synchronous) ------------------------------------------------


def test_autoroute_runs_synchronously_and_returns_result() -> None:
    """The synchronous verb produces an AutorouteResult without touching the
    layout (an empty schematic has nothing to route → a clean empty result)."""
    session = _session()
    result = session.edit_controller.autoroute(None, settings=RoutingSettings())
    assert isinstance(result, AutorouteResult)
    # The verb never mutates the layout; committing is apply_autoroute's job.
    assert len(session.layout.wires) == 0


def test_autoroute_propagates_router_failure_without_edit(
    monkeypatch,
) -> None:
    session = _session()

    class FailingRouter:
        def route(self, *_args, **_kwargs):
            raise RuntimeError("router failed")

    from protoboard.services.routing import protocol

    monkeypatch.setattr(protocol.registry, "default", lambda: FailingRouter())

    with pytest.raises(RuntimeError, match="router failed"):
        session.edit_controller.autoroute(None, settings=RoutingSettings())
    assert session.layout.wires == []
    assert session.stack.can_undo() is False


def test_autoroute_propagates_drc_precondition_failure() -> None:
    """Off-board pins block autoroute — the precondition error propagates."""
    from protoboard.services.routing.autorouter import AutoroutePreconditionError

    session = _session()
    # A 2-pin R at the very edge pushes pin 2 off the board.
    a = _place(session, 7.0, 7.0, "R1")
    net = Net(
        id=uuid.uuid4(),
        name="N1",
        pins={PinRef(component_id=a, pin_number=1)},
    )
    session.schematic.nets = {net.id: net}
    session.invalidate_graph()
    before_wires = list(session.layout.wires)
    before_nets = set(session.schematic.nets)
    with pytest.raises(AutoroutePreconditionError):
        session.edit_controller.autoroute({net.id}, settings=RoutingSettings())
    assert list(session.layout.wires) == before_wires
    assert set(session.schematic.nets) == before_nets
    assert session.stack.can_undo() is False
