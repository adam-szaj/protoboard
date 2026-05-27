# SPDX-License-Identifier: MIT
"""Shared invariants for autoroute correctness tests.

The single most important post-autoroute property is: no two distinct
schematic nets may end up in the same electric set.  That is a real
electrical short — invisible without DRC, ruinous on hardware.

`assert_no_net_fusion` is consumed by both the targeted scenarios in
test_autoroute_invariants.py and the hypothesis fuzz in
tests/property/test_autoroute_fuzz.py.  Keep them in sync.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace

from protoboard.domain.board.board import Board
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.connectivity.graph import ConnectivityGraph
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.wire import Wire
from protoboard.domain.schematic.schematic import Schematic


def assert_no_net_fusion(
    board: Board,
    layout: Layout,
    extra_wires: list[Wire],
    schematic: Schematic,
    components: dict[str, ComponentDefinition],
    instances: dict,  # dict[UUID, ComponentInstance]
) -> None:
    """Verify no electric set in *layout* + *extra_wires* holds pins of two nets.

    Raises AssertionError with a description of every fused cluster.  Pass
    `extra_wires=result.wires` to check the output of an autoroute() call.
    """
    final_layout = replace(layout, wires=list(layout.wires) + list(extra_wires))
    graph = ConnectivityGraph(
        board=board, layout=final_layout,
        components=components, instances=instances,
    )

    key_to_nets: dict[object, set[str]] = defaultdict(set)
    for net in schematic.nets.values():
        for ref in net.pins:
            ck = graph.node_for_pin(ref)
            if ck is not None:
                key_to_nets[ck].add(net.name)

    fused = {ck: names for ck, names in key_to_nets.items() if len(names) > 1}
    if not fused:
        return

    lines = ["Net fusion detected after autoroute:"]
    for ck, names in fused.items():
        lines.append(f"  canonical={ck} nets={sorted(names)}")
    # Append the wires for the convenience of test triage.
    lines.append("Wires under test:")
    for w in extra_wires:
        lines.append(
            f"  id={str(w.id)[:8]} ins={w.insulated} side={w.side} "
            f"path={[(p.x, p.y) for p in w.path]}"
        )
    raise AssertionError("\n".join(lines))


def assert_no_redundant_routes(
    board: Board,
    layout: Layout,
    extra_wires: list[Wire],
    components: dict[str, ComponentDefinition],
    instances: dict,  # dict[UUID, ComponentInstance]
) -> None:
    """Verify every wire in *extra_wires* is essential to its net's tree.

    A wire is redundant when removing it still leaves its endpoints in the
    same electric set — meaning some other path already connects them.
    For an autoroute result that should be a spanning tree per net, any
    such wire is a duplicate edge: two independent routes between the
    same pair of pads.

    This typically happens when a net has ≥3 pins.  The greedy MST routes
    the closest pair first; the second segment then routes a remaining pin
    to the connected electric set, and A* (which receives the *whole*
    electric set as target_pads) can end its path on a pad reached through
    a different corridor than the first wire — producing a parallel route.
    """
    all_wires = list(layout.wires) + list(extra_wires)
    redundant: list[Wire] = []
    for w in extra_wires:
        if len(w.path) < 2:
            continue
        others = [ww for ww in all_wires if ww.id != w.id]
        test_layout = replace(layout, wires=others)
        graph = ConnectivityGraph(
            board=board, layout=test_layout,
            components=components, instances=instances,
        )
        if graph.are_connected(w.path[0], w.side, w.path[-1], w.side):
            redundant.append(w)
    if not redundant:
        return

    lines = [f"Redundant routes detected ({len(redundant)} of "
             f"{len(extra_wires)} wires):"]
    for w in redundant:
        lines.append(
            f"  id={str(w.id)[:8]} side={w.side} ins={w.insulated} "
            f"({w.path[0].x},{w.path[0].y}) -> "
            f"({w.path[-1].x},{w.path[-1].y}) hops={len(w.path)-1}"
        )
    raise AssertionError("\n".join(lines))


# ---------------------------------------------------------------------------
# Layout factories shared by handcrafted + property tests
# ---------------------------------------------------------------------------

from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.components.footprints import SingleRow
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.geometry import Point


def make_perfboard(rows: int, cols: int) -> Board:
    """Plain plated-through perfboard, both layers."""
    return Board(
        root=BoardGrid(
            width_policy=SizePolicy.HUG, height_policy=SizePolicy.HUG,
            cell=PadAtom(
                width_policy=SizePolicy.HUG, height_policy=SizePolicy.HUG,
                layers=PadLayers(top=True, bottom=True, plated_through=True),
            ),
            rows=rows, cols=cols, row_gap=0.0, col_gap=0.0,
        )
    )


def make_sil(name: str, n_pins: int = 2) -> ComponentDefinition:
    return ComponentDefinition(
        name=name, footprint=SingleRow(n=n_pins),
        pin_names={i: str(i) for i in range(1, n_pins + 1)},
    )


def place(cid, x: float, y: float, rotation: int = 90) -> ComponentPlacement:
    """rotation=90 → horizontal pins (pin1=(x,y), pin2=(x+1,y))."""
    return ComponentPlacement(
        component_id=cid, position=Point(x, y),
        rotation=rotation, side="top",
    )


def bare(*pts: tuple[float, float], side: str = "top") -> Wire:
    import uuid as _u
    return Wire(
        id=_u.uuid4(),
        path=[Point(*p) for p in pts],
        insulated=False, side=side,  # type: ignore[arg-type]
    )


def insulated(*pts: tuple[float, float], side: str = "top") -> Wire:
    import uuid as _u
    return Wire(
        id=_u.uuid4(),
        path=[Point(*p) for p in pts],
        insulated=True, side=side,  # type: ignore[arg-type]
    )
