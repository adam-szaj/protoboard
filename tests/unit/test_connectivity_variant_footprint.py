# SPDX-License-Identifier: MIT
"""Connectivity must register a placed component's pin-pads at the *chosen
variant's* footprint positions, not the definition default.

Bug (IRF9540_P, rotated 90°): ``ConnectivityGraph`` built its pin→pad map from
``defn.footprint`` instead of the instance's resolved variant footprint.  For a
part whose default footprint orients pins along one axis but whose chosen variant
orients them along the other, pins 2/3 were registered at the *wrong* world
positions.  A bare wire physically touching the real pad then found no pin node
there, so it (and the pin) got no net — the path-dependent "MISSING_NET".
"""

from __future__ import annotations

import uuid

import pytest

from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import (
    ComponentPlacement,
    pin_world_position,
)
from protoboard.domain.layout.wire import Wire, WireCollection
from protoboard.domain.connectivity.graph import ConnectivityGraph
from protoboard.domain.schematic.net import PinRef
from protoboard.geometry import Point


def _catalogue():  # type: ignore[no-untyped-def]
    from protoboard.paths import BUILTIN_LIBRARY_DIR
    from protoboard.services.library.loader import load_library
    from protoboard.services.library.package_family_loader import (
        load_full_package_catalogue,
    )

    families = load_full_package_catalogue()
    lib = load_library([BUILTIN_LIBRARY_DIR], families)
    return lib, families


def _perfboard(rows: int, cols: int):  # type: ignore[no-untyped-def]
    from protoboard.domain.board.board import Board
    from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
    from protoboard.domain.board.pads import PadLayers

    grid = BoardGrid(
        width_policy=SizePolicy.HUG, height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG, height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        ),
        rows=rows, cols=cols, row_gap=0.0, col_gap=0.0,
    )
    return Board(root=grid)


_OPEN_BUG = pytest.mark.xfail(
    strict=True,
    reason="defn.footprint used in ConnectivityGraph._build_component_pin_map / "
    "_build_same_name_pin_merges instead of the instance's resolved variant "
    "footprint — see docs/todo.md 'Eliminate defn.footprint for placed "
    "components'. Remove this marker when incremental.py is fixed.",
)


class TestVariantPinConnectivity:
    """A bare wire on the real pin-2 pad of a rotated TO-220 part connects it."""

    @_OPEN_BUG
    def test_bare_wire_on_rotated_pin2_shares_node(self) -> None:
        lib, families = _catalogue()
        defn = lib.get("IRF9540_P")
        # The bug repro: the instance chose the SIL-3 variant (pins along the
        # local Y), whose footprint differs from the TO-220 default (pins along
        # local X).  This is exactly the ActiveLoad2 project's saved state.
        inst = ComponentInstance.new(
            definition_ref="IRF9540_P", refdes="Q1", value="",
            package_variant="sil-3/std",
        ).bind(defn, families)

        board = _perfboard(8, 8)
        # rotation 90: the variant orients pins along the placement's local Y,
        # which rotates to +x in world — pin2 lands one hole right of pin1.
        pl = ComponentPlacement(
            component_id=inst.id, position=Point(2.5, 2.5), rotation=90, side="top",
        )
        fp = inst.resolved_footprint()
        pin2_world = pin_world_position(fp, pl, 2)

        # A bare wire that runs THROUGH pin2's real pad to a neighbour pad.
        neighbour = Point(pin2_world.x, pin2_world.y + 1.0)
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(pin2_world.x, pin2_world.y), neighbour],
            insulated=False, side="top",
        )
        lay = Layout(placements={inst.id: pl}, wires=WireCollection([wire]))
        graph = ConnectivityGraph(
            board=board, layout=lay,
            components={defn.name: defn}, instances={inst.id: inst},
        )

        node_pin2 = graph.node_for_pin(PinRef(component_id=inst.id, pin_number=2))
        node_pad = graph.node_for_pad(neighbour, "top")
        assert node_pin2 is not None
        # The wire ran through pin2's pad, so pin2 and the neighbour pad it
        # reached must be the same electrical node.
        assert node_pin2 == node_pad

    @_OPEN_BUG
    def test_pin3_registered_at_variant_position(self) -> None:
        """node_for_pad at the variant's pin-3 world position is the pin-3 node."""
        lib, families = _catalogue()
        defn = lib.get("IRF9540_P")
        # The bug repro: the instance chose the SIL-3 variant (pins along the
        # local Y), whose footprint differs from the TO-220 default (pins along
        # local X).  This is exactly the ActiveLoad2 project's saved state.
        inst = ComponentInstance.new(
            definition_ref="IRF9540_P", refdes="Q1", value="",
            package_variant="sil-3/std",
        ).bind(defn, families)

        board = _perfboard(8, 8)
        pl = ComponentPlacement(
            component_id=inst.id, position=Point(2.5, 2.5), rotation=90, side="top",
        )
        fp = inst.resolved_footprint()
        pin3_world = pin_world_position(fp, pl, 3)

        lay = Layout(placements={inst.id: pl}, wires=WireCollection([]))
        graph = ConnectivityGraph(
            board=board, layout=lay,
            components={defn.name: defn}, instances={inst.id: inst},
        )

        node_pin3 = graph.node_for_pin(PinRef(component_id=inst.id, pin_number=3))
        node_at_world = graph.node_for_pad(pin3_world, "top")
        assert node_pin3 is not None
        assert node_pin3 == node_at_world
