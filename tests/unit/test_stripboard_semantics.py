# SPDX-License-Identifier: MIT
"""Electrical and editing rules for copper built into a stripboard."""

import uuid
from dataclasses import dataclass, replace
from typing import cast

import pytest

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import HBox, SizePolicy, StripAtom, VBox
from protoboard.domain.board.pad_state import SHORT_CIRCUIT, PadIndex
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.commands.base import ComponentState
from protoboard.domain.commands.board_cmds import SplitStripCommand
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.connectivity.incremental import ConnectivityGraph
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import Wire
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from protoboard.headless_session import HeadlessSession
from protoboard.services.library.loader import ComponentLibrary
from tests.unit._autoroute_invariants import make_sil, sil_families


@dataclass
class _BoardState:
    board: Board


def _board(layers: PadLayers, *, vertical: bool = False) -> Board:
    strip = StripAtom(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        length=4,
        orientation="vertical" if vertical else "horizontal",
        layers=layers,
    )
    container = VBox if vertical else HBox
    return Board(
        root=container(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[strip],
            gap=0,
        )
    )


@pytest.mark.parametrize(
    "layers",
    [
        PadLayers(top=True),
        PadLayers(top=False, bottom=True),
        PadLayers(top=True, bottom=True),
        PadLayers(top=True, bottom=True, plated_through=True),
    ],
)
def test_graph_connects_each_copper_layer(layers: PadLayers) -> None:
    board = _board(layers)
    graph = ConnectivityGraph(board=board, layout=Layout(), components={})
    for side in layers.layer_ids():
        nodes = [graph.node_for_pad(pt, side) for pt in board.pad_positions()]
        assert nodes[0] is not None
        assert len(set(nodes)) == 1
    if layers.top and layers.bottom:
        same_node = graph.node_for_pad(Point(0.5, 0.5), "top") == graph.node_for_pad(
            Point(0.5, 0.5), "bottom"
        )
        assert same_node == layers.plated_through


def test_strip_propagates_nets_and_short_circuits_after_incremental_edits() -> None:
    board = _board(PadLayers(top=False, bottom=True))
    index = PadIndex(board, Layout(), Schematic(nets={}), {}, {})
    first, last = board.pad_positions()[0], board.pad_positions()[-1]
    net_a, net_b = uuid.uuid4(), uuid.uuid4()
    a = Wire(id=uuid.uuid4(), path=[first, Point(0.5, 1.5)], side="bottom")
    b = Wire(id=uuid.uuid4(), path=[last, Point(3.5, 1.5)], side="bottom")
    assert index.is_free(last, "bottom")
    index.add_wire(a, net=net_a)
    assert index.electrical_net_at(last, "bottom") == net_a
    assert not index.can_route_through(last, "bottom", net_b, "bare")
    assert index.electrical_net_at(last, "top") is None
    twin = index.clone()
    twin.remove_wire(a)
    assert twin.electrical_net_at(last, "bottom") is None
    assert index.electrical_net_at(last, "bottom") == net_a
    index.add_wire(b, net=net_b)
    assert index.electrical_net_at(first, "bottom") is SHORT_CIRCUIT
    assert index.electrical_net_at(last, "bottom") is SHORT_CIRCUIT
    index.remove_wire(a)
    assert index.electrical_net_at(first, "bottom") == net_b
    index.remove_wire(b)
    assert index.is_free(first, "bottom")


def test_pin_bonds_unplated_strip_layers_until_removed() -> None:
    board = _board(PadLayers(top=True, bottom=True))
    definition = make_sil("J", 2)
    instance = ComponentInstance.new(definition_ref="J", refdes="J1", value="").bind(
        definition, sil_families(definition)
    )
    index = PadIndex(board, Layout(), Schematic(nets={}), {}, {})
    wire = Wire(id=uuid.uuid4(), path=[Point(3.5, 0.5), Point(3.5, 1.5)], side="bottom")
    net = uuid.uuid4()
    index.add_wire(wire, net=net)
    assert index.electrical_net_at(Point(1.5, 0.5), "top") is None
    placement = ComponentPlacement(
        component_id=instance.id, position=Point(0.5, 0.5), rotation=0, side="top"
    )
    index.add_component(instance.id, placement, definition, instance=instance)
    assert index.electrical_net_at(Point(1.5, 0.5), "top") == net
    index.remove_component(instance.id)
    assert index.electrical_net_at(Point(1.5, 0.5), "top") is None
    assert index.electrical_net_at(Point(1.5, 0.5), "bottom") == net


@pytest.mark.parametrize("vertical", [False, True])
def test_cut_keeps_hole_positions_and_separates_nodes(vertical: bool) -> None:
    board = _board(PadLayers(top=False, bottom=True), vertical=vertical)
    positions = board.pad_positions()
    strip = next(atom for atom, _ in board.atoms() if isinstance(atom, StripAtom))
    state = cast(ComponentState, _BoardState(board))
    command = SplitStripCommand(target=strip, cut_index=2, cut_between=True)
    command.apply(state)
    assert state.board is not board
    assert state.board.pad_positions() == positions
    graph = ConnectivityGraph(board=state.board, layout=Layout(), components={})
    assert graph.node_for_pad(positions[0], "bottom") != graph.node_for_pad(
        positions[-1], "bottom"
    )
    command.revert(state)
    assert state.board is board


def test_cut_preserves_label() -> None:
    board = _board(PadLayers(top=False, bottom=True))
    strip = next(atom for atom, _ in board.atoms() if isinstance(atom, StripAtom))
    strip = replace(strip, label="GND")
    board = Board(root=replace(cast(HBox, board.root), children=[strip]))
    state = cast(ComponentState, _BoardState(board))
    SplitStripCommand(target=strip, cut_index=2, cut_between=True).apply(state)
    assert [atom.label for atom, _ in state.board.atoms()] == ["GND", "GND"]


def test_cut_vertical_strip_nested_in_horizontal_container_keeps_geometry() -> None:
    strip_board = _board(PadLayers(top=False, bottom=True), vertical=True)
    strip = next(atom for atom, _ in strip_board.atoms() if isinstance(atom, StripAtom))
    board = Board(
        root=HBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[strip],
            gap=1,
        )
    )
    positions = board.pad_positions()
    state = cast(ComponentState, _BoardState(board))
    SplitStripCommand(target=strip, cut_index=2, cut_between=True).apply(state)
    assert state.board.pad_positions() == positions
    assert state.board.bounds == board.bounds


def test_removing_redundant_wire_keeps_builtin_strip_connected() -> None:
    board = _board(PadLayers(top=False, bottom=True))
    graph = ConnectivityGraph(board=board, layout=Layout(), components={})
    wire = Wire(id=uuid.uuid4(), path=[Point(0.5, 0.5), Point(3.5, 0.5)], side="bottom")
    graph.add_wire(wire)
    graph.remove_wire(wire)
    nodes = [graph.node_for_pad(point, "bottom") for point in board.pad_positions()]
    assert nodes[0] is not None
    assert len(set(nodes)) == 1


def test_removing_parallel_bridges_preserves_each_strip() -> None:
    rows = [_board(PadLayers(top=False, bottom=True)).root for _ in range(2)]
    board = Board(
        root=VBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=rows,
            gap=0,
        )
    )
    graph = ConnectivityGraph(board=board, layout=Layout(), components={})
    bridges = [
        Wire(id=uuid.uuid4(), path=[Point(x, 0.5), Point(x, 1.5)], side="bottom")
        for x in (0.5, 3.5)
    ]
    for wire in bridges:
        graph.add_wire(wire)
    graph.remove_wire(bridges[0])
    assert graph.node_for_pad(Point(0.5, 0.5), "bottom") == graph.node_for_pad(
        Point(3.5, 1.5), "bottom"
    )
    graph.remove_wire(bridges[1])
    for y in (0.5, 1.5):
        nodes = [graph.node_for_pad(Point(x + 0.5, y), "bottom") for x in range(4)]
        assert nodes[0] is not None
        assert len(set(nodes)) == 1
    assert graph.component_count() == 2


def test_wire_inherits_net_from_remote_strip_pad_and_cut_stops_it() -> None:
    board = _board(PadLayers(top=False, bottom=True))
    definition = make_sil("J", 2)
    families = sil_families(definition)
    session = HeadlessSession(
        board, ComponentLibrary({"J": definition}, package_families=families)
    )
    instances = [
        ComponentInstance.new(definition_ref="J", refdes=refdes, value="").bind(
            definition, families
        )
        for refdes in ("J1", "J2")
    ]
    for instance, position in zip(
        instances, [Point(0.5, 0.5), Point(0.5, 2.5)], strict=True
    ):
        session.instances[instance.id] = instance
        session.layout.placements[instance.id] = ComponentPlacement(
            component_id=instance.id, position=position, rotation=0, side="top"
        )
    source, target = [PinRef(component_id=i.id, pin_number=1) for i in instances]
    net = Net(id=uuid.uuid4(), name="VCC", pins={source})
    session.schematic.nets[net.id] = net
    session.invalidate_graph()
    assert session.pad_index().electrical_net_at(Point(3.5, 0.5), "bottom") == net.id
    wire = Wire(
        id=uuid.uuid4(),
        path=[Point(3.5, 0.5), Point(0.5, 2.5)],
        side="bottom",
        insulated=True,
    )
    wire_id = session.edit_controller.add_wire(wire.path, insulated=True, side="bottom")
    assert wire_id is not None
    assert session.schematic.net_for_pin(target) is net
    assert session.cached_wire_net_map()[wire_id] == net.id
    session.stack.undo(cast(ComponentState, session))
    session.invalidate_graph()
    assert session.schematic.net_for_pin(target) is None
    strip = next(atom for atom, _ in board.atoms() if isinstance(atom, StripAtom))
    session.edit_controller.execute_command(
        SplitStripCommand(target=strip, cut_index=2, cut_between=True)
    )
    assert session.pad_index().electrical_net_at(Point(3.5, 0.5), "bottom") is None
    wire_id = session.edit_controller.add_wire(wire.path, insulated=True, side="bottom")
    assert wire_id is not None
    assert session.schematic.net_for_pin(target) is None
