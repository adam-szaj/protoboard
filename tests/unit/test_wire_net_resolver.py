# SPDX-License-Identifier: MIT
"""Tests for WireNetResolver — cohesive view over wire→net resolution.

The resolver only gathers the shared ``schematic / layout / instances /
components / board / pad_index`` bundle once and delegates to the free
``_wire_net_id``. These tests assert method-vs-function equivalence so the
wrapper can never silently diverge from the canonical implementation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import Wire
from protoboard.domain.layout.wire_utils import WireNetResolver, _wire_net_id
from protoboard.domain.schematic.net import Net
from protoboard.domain.schematic.schematic import PinRef, Schematic
from protoboard.geometry import Point


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _defn() -> ComponentDefinition:
    return ComponentDefinition(name="R", footprint=SIL(2), pin_names={1: "A", 2: "B"})


def _inst(refdes: str = "R1") -> ComponentInstance:
    return ComponentInstance.new(definition_ref="R", refdes=refdes, value="10k")


def _placement(comp_id: uuid.UUID, x: float, y: float) -> ComponentPlacement:
    # SIL(2) rot=90 → pin1 at (x, y), pin2 at (x+1, y).
    return ComponentPlacement(
        component_id=comp_id, position=Point(x, y), rotation=90, side="top",
        shared_pins=frozenset(),
    )


def _wire(*pts: tuple[float, float]) -> Wire:
    return Wire(
        id=uuid.uuid4(),
        path=[Point(x, y) for x, y in pts],
        insulated=False,
        side="top",
    )


@dataclass
class _FakeState:
    """Minimal ComponentState-like duck for WireNetResolver.from_state."""
    layout: Layout
    instances: dict[uuid.UUID, ComponentInstance]
    components: dict[str, ComponentDefinition]
    board: object | None = None


# ---------------------------------------------------------------------------
# Equivalence
# ---------------------------------------------------------------------------

class TestWireNetResolverEquivalence:
    def test_netted_pin_matches_free_function(self) -> None:
        net_id = uuid.uuid4()
        defn = _defn()
        r1 = _inst()
        pl = _placement(r1.id, 2.0, 2.0)  # pin1 at (2,2)
        layout = Layout(placements={r1.id: pl})
        instances = {r1.id: r1}
        components = {"R": defn}
        sch = Schematic(nets={
            net_id: Net(
                id=net_id, name="N1",
                pins=frozenset({PinRef(component_id=r1.id, pin_number=1)}),
            )
        })
        # Wire terminating on pin1's pad.
        wire = _wire((2.0, 2.0), (2.0, 5.0))

        expected = _wire_net_id(wire, sch, layout, instances, components)
        resolver = WireNetResolver(
            schematic=sch, layout=layout,
            instances=instances, components=components,
        )
        got = resolver.net_id(wire)

        assert got == expected == net_id

    def test_no_pin_touch_matches_free_function(self) -> None:
        defn = _defn()
        r1 = _inst()
        pl = _placement(r1.id, 2.0, 2.0)
        layout = Layout(placements={r1.id: pl})
        instances = {r1.id: r1}
        components = {"R": defn}
        sch = Schematic(nets={})
        # Wire nowhere near any pin → no net.
        wire = _wire((10.0, 10.0), (10.0, 12.0))

        expected = _wire_net_id(wire, sch, layout, instances, components)
        resolver = WireNetResolver(
            schematic=sch, layout=layout,
            instances=instances, components=components,
        )
        assert resolver.net_id(wire) == expected is None

    def test_pin_present_but_unnetted_matches_free_function(self) -> None:
        defn = _defn()
        r1 = _inst()
        pl = _placement(r1.id, 2.0, 2.0)
        layout = Layout(placements={r1.id: pl})
        instances = {r1.id: r1}
        components = {"R": defn}
        sch = Schematic(nets={})  # pin exists but is on no net
        wire = _wire((2.0, 2.0), (2.0, 5.0))

        expected = _wire_net_id(wire, sch, layout, instances, components)
        resolver = WireNetResolver(
            schematic=sch, layout=layout,
            instances=instances, components=components,
        )
        assert resolver.net_id(wire) == expected is None


# ---------------------------------------------------------------------------
# from_state
# ---------------------------------------------------------------------------

class TestFromState:
    def test_from_state_reads_context(self) -> None:
        defn = _defn()
        r1 = _inst()
        pl = _placement(r1.id, 2.0, 2.0)
        layout = Layout(placements={r1.id: pl})
        instances = {r1.id: r1}
        components = {"R": defn}
        sch = Schematic(nets={})
        board_sentinel = object()
        state = _FakeState(
            layout=layout, instances=instances,
            components=components, board=board_sentinel,
        )

        resolver = WireNetResolver.from_state(state, sch)

        assert resolver.schematic is sch
        assert resolver.layout is layout
        assert resolver.instances is instances
        assert resolver.components is components
        assert resolver.board is board_sentinel
        assert resolver.pad_index is None

    def test_from_state_forwards_pad_index(self) -> None:
        defn = _defn()
        r1 = _inst()
        layout = Layout(placements={r1.id: _placement(r1.id, 2.0, 2.0)})
        state = _FakeState(
            layout=layout, instances={r1.id: r1}, components={"R": defn},
        )
        sentinel = object()
        resolver = WireNetResolver.from_state(
            state, Schematic(nets={}), pad_index=sentinel,  # type: ignore[arg-type]
        )
        assert resolver.pad_index is sentinel

    def test_from_state_missing_board_defaults_none(self) -> None:
        defn = _defn()
        r1 = _inst()
        layout = Layout(placements={r1.id: _placement(r1.id, 2.0, 2.0)})

        @dataclass
        class _NoBoard:
            layout: Layout
            instances: dict[uuid.UUID, ComponentInstance]
            components: dict[str, ComponentDefinition]

        state = _NoBoard(
            layout=layout, instances={r1.id: r1}, components={"R": defn},
        )
        resolver = WireNetResolver.from_state(state, Schematic(nets={}))
        assert resolver.board is None
