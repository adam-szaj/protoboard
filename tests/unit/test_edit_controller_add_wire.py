# SPDX-License-Identifier: MIT
"""EditController.add_wire — net-aware wire commit driven headless (no Qt).

Exercises the verb that absorbs RouteTool._commit: pad-node enrichment, the
AddWireWithNetsCommand, and the net resolution — including the injected
NetMergePrompt seam (qt-ports D4) for the two-distinct-nets merge. The route-tool
tests keep asserting the same behaviour through the tool, which now delegates
here.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, PadLayers, SizePolicy
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.geometry import Point
from protoboard.services.editing import HeadlessSession
from protoboard.services.library.loader import load_library

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
    """Place a 2-pin "R": pin 1 at (x, y), pin 2 at (x, y+1)."""
    defn = session.components["R"]
    inst = ComponentInstance.new(
        definition_ref="R", refdes=refdes, value="",
    ).bind(defn, session.package_families)
    session.instances[inst.id] = inst
    session.layout.placements[inst.id] = ComponentPlacement(
        component_id=inst.id, position=Point(x, y), rotation=0, side="top",
    )
    session.invalidate_graph()
    return inst.id


def test_add_wire_too_short_returns_none() -> None:
    session = _session()
    assert session.edit_controller.add_wire(
        [Point(1.0, 1.0)], insulated=True, side="top",
    ) is None
    assert len(session.layout.wires) == 0


def test_add_wire_creates_net_between_two_pins() -> None:
    session = _session()
    a = _place(session, 2.0, 2.0, "R1")
    b = _place(session, 4.0, 2.0, "R2")
    pa = session.get_pin_position(a, 1)
    pb = session.get_pin_position(b, 1)
    assert pa is not None and pb is not None

    wire_id = session.edit_controller.add_wire(
        [pa, pb], insulated=True, side="top",
    )
    assert wire_id is not None
    assert any(w.id == wire_id for w in session.layout.wires)
    # BOTH_UNCONNECTED → a fresh net now spans the two wired pins.
    all_pins = {
        (r.component_id, r.pin_number)
        for net in session.schematic.nets.values()
        for r in net.pins
    }
    assert (a, 1) in all_pins
    assert (b, 1) in all_pins


def test_add_wire_undo_removes_wire_and_net() -> None:
    session = _session()
    a = _place(session, 2.0, 2.0, "R1")
    b = _place(session, 4.0, 2.0, "R2")
    pa = session.get_pin_position(a, 1)
    pb = session.get_pin_position(b, 1)
    assert pa is not None and pb is not None
    nets_before = len(session.schematic.nets)

    wire_id = session.edit_controller.add_wire([pa, pb], insulated=True, side="top")
    assert wire_id is not None
    assert len(session.schematic.nets) == nets_before + 1

    session.stack.undo(session)  # type: ignore[arg-type]
    assert not any(w.id == wire_id for w in session.layout.wires)
    assert len(session.schematic.nets) == nets_before


def test_add_wire_merge_prompt_invoked_and_can_cancel() -> None:
    session = _session()
    a = _place(session, 2.0, 2.0, "R1")
    b = _place(session, 4.0, 2.0, "R2")
    pa = session.get_pin_position(a, 1)
    pb = session.get_pin_position(b, 1)
    assert pa is not None and pb is not None
    # Put each wired pin on its own distinct net → wiring them fuses two nets.
    net_a = Net(id=uuid.uuid4(), name="NA", pins={PinRef(component_id=a, pin_number=1)})
    net_b = Net(id=uuid.uuid4(), name="NB", pins={PinRef(component_id=b, pin_number=1)})
    session.schematic.nets = {net_a.id: net_a, net_b.id: net_b}
    session.invalidate_graph()

    asked: list[tuple[str, str]] = []

    def cancel_prompt(x: Net, y: Net) -> str | None:
        asked.append((x.name, y.name))
        return None  # cancel

    wire_id = session.edit_controller.add_wire(
        [pa, pb], insulated=True, side="top", merge_prompt=cancel_prompt,
    )
    assert wire_id is None                       # merge cancelled → no commit
    assert asked and set(asked[0]) == {"NA", "NB"}
    assert len(session.layout.wires) == 0        # wire not added
    assert net_a.id in session.schematic.nets    # nets untouched
    assert net_b.id in session.schematic.nets


def test_add_wire_merge_prompt_accept_merges() -> None:
    session = _session()
    a = _place(session, 2.0, 2.0, "R1")
    b = _place(session, 4.0, 2.0, "R2")
    pa = session.get_pin_position(a, 1)
    pb = session.get_pin_position(b, 1)
    assert pa is not None and pb is not None
    net_a = Net(id=uuid.uuid4(), name="NA", pins={PinRef(component_id=a, pin_number=1)})
    net_b = Net(id=uuid.uuid4(), name="NB", pins={PinRef(component_id=b, pin_number=1)})
    session.schematic.nets = {net_a.id: net_a, net_b.id: net_b}
    session.invalidate_graph()

    wire_id = session.edit_controller.add_wire(
        [pa, pb], insulated=True, side="top",
        merge_prompt=lambda x, y: "MERGED",
    )
    assert wire_id is not None
    assert session.schematic.nets[net_a.id].name == "MERGED"
    assert net_b.id not in session.schematic.nets  # merged away


def test_add_wire_raises_on_uninjected_merge() -> None:
    """The library default is loud: a two-net merge with no prompt raises."""
    import pytest

    session = _session()
    a = _place(session, 2.0, 2.0, "R1")
    b = _place(session, 4.0, 2.0, "R2")
    pa = session.get_pin_position(a, 1)
    pb = session.get_pin_position(b, 1)
    assert pa is not None and pb is not None
    net_a = Net(id=uuid.uuid4(), name="NA", pins={PinRef(component_id=a, pin_number=1)})
    net_b = Net(id=uuid.uuid4(), name="NB", pins={PinRef(component_id=b, pin_number=1)})
    session.schematic.nets = {net_a.id: net_a, net_b.id: net_b}
    session.invalidate_graph()

    with pytest.raises(ValueError, match="merge_prompt"):
        session.edit_controller.add_wire([pa, pb], insulated=True, side="top")
