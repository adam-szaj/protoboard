# SPDX-License-Identifier: MIT
"""Tests for find_wires_crossing_pads — segment-level wire/pad conflict detection."""

from __future__ import annotations

import uuid

from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.pad_conflict import find_wires_crossing_pads
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import Wire
from protoboard.domain.schematic.net import Net
from protoboard.domain.schematic.schematic import PinRef, Schematic
from protoboard.geometry import Point

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _defn(name: str, n_pins: int = 2) -> ComponentDefinition:
    return ComponentDefinition(
        name=name,
        footprint=SIL(n_pins),
        pin_names={i: str(i) for i in range(1, n_pins + 1)},
    )


def _inst(defn: ComponentDefinition) -> ComponentInstance:
    return ComponentInstance.new(definition_ref=defn.name, refdes="X1", value="")


def _pl(x: float, y: float, rotation: int = 0) -> ComponentPlacement:
    return ComponentPlacement(
        component_id=uuid.uuid4(),
        position=Point(x, y),
        rotation=rotation,
        side="top",
    )


def _wire(*points: tuple[float, float]) -> Wire:
    return Wire(
        id=uuid.uuid4(),
        path=[Point(x, y) for x, y in points],
        insulated=True,
        side="top",
    )


def _schematic(*nets: tuple[uuid.UUID, list[tuple[uuid.UUID, int]]]) -> Schematic:
    """Build a schematic with given nets: (net_id, [(comp_id, pin_num), ...])"""
    net_objs: dict[uuid.UUID, Net] = {}
    for net_id, pins in nets:
        pin_refs = {PinRef(component_id=c, pin_number=p) for c, p in pins}
        net_objs[net_id] = Net(id=net_id, name=f"N{net_id.int}", pins=pin_refs)
    return Schematic(nets=net_objs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFindWiresCrossingPads:

    def test_no_wires_returns_empty(self) -> None:
        defn = _defn("R", 2)
        inst = _inst(defn)
        pl = _pl(0.0, 0.0)
        pl.component_id = inst.id  # type: ignore[attr-defined]
        placement = ComponentPlacement(
            component_id=inst.id, position=Point(0.0, 0.0), rotation=0, side="top"
        )
        result = find_wires_crossing_pads(
            wires=[],
            moved_comp_ids={inst.id},
            layout_placements={inst.id: placement},
            instances={inst.id: inst},
            components={defn.name: defn},
            schematic=_schematic(),
        )
        assert result == []

    def test_wire_not_near_any_pad_ok(self) -> None:
        """Wire far from all pads → no conflict."""
        defn = _defn("R", 2)
        inst = _inst(defn)
        # SIL(2): pin1 at (0,0), pin2 at (1,0)
        placement = ComponentPlacement(
            component_id=inst.id, position=Point(0.0, 0.0), rotation=0, side="top"
        )
        wire = _wire((5.0, 5.0), (10.0, 5.0))
        result = find_wires_crossing_pads(
            wires=[wire],
            moved_comp_ids={inst.id},
            layout_placements={inst.id: placement},
            instances={inst.id: inst},
            components={defn.name: defn},
            schematic=_schematic(),
        )
        assert result == []

    def test_wire_vertex_on_alien_pad(self) -> None:
        """Wire vertex lands exactly on a pad with a different net → conflict."""
        net_a = uuid.uuid4()
        net_b = uuid.uuid4()

        defn_a = _defn("A", 1)
        inst_a = ComponentInstance.new(definition_ref="A", refdes="A1", value="")
        pl_a = ComponentPlacement(
            component_id=inst_a.id, position=Point(0.0, 0.0), rotation=0, side="top"
        )

        defn_b = _defn("B", 1)
        inst_b = ComponentInstance.new(definition_ref="B", refdes="B1", value="")
        pl_b = ComponentPlacement(
            component_id=inst_b.id, position=Point(3.0, 0.0), rotation=0, side="top"
        )

        # Wire from A's pad to some other point, but vertex lands on B's pad
        wire = _wire((0.0, 0.0), (3.0, 0.0))

        sch = _schematic(
            (net_a, [(inst_a.id, 1)]),
            (net_b, [(inst_b.id, 1)]),
        )

        result = find_wires_crossing_pads(
            wires=[wire],
            moved_comp_ids={inst_a.id},
            layout_placements={inst_a.id: pl_a, inst_b.id: pl_b},
            instances={inst_a.id: inst_a, inst_b.id: inst_b},
            components={"A": defn_a, "B": defn_b},
            schematic=sch,
        )
        assert wire.id in result

    def test_wire_vertex_same_net_ok(self) -> None:
        """Wire endpoint on pad of the SAME net → no conflict (intentional)."""
        net = uuid.uuid4()
        defn_a = _defn("A", 1)
        inst_a = ComponentInstance.new(definition_ref="A", refdes="A1", value="")
        pl_a = ComponentPlacement(
            component_id=inst_a.id, position=Point(0.0, 0.0), rotation=0, side="top"
        )
        defn_b = _defn("B", 1)
        inst_b = ComponentInstance.new(definition_ref="B", refdes="B1", value="")
        pl_b = ComponentPlacement(
            component_id=inst_b.id, position=Point(3.0, 0.0), rotation=0, side="top"
        )
        # Wire connecting A and B — both in same net
        wire = _wire((0.0, 0.0), (3.0, 0.0))
        sch = _schematic(
            (net, [(inst_a.id, 1), (inst_b.id, 1)]),
        )
        result = find_wires_crossing_pads(
            wires=[wire],
            moved_comp_ids={inst_a.id},
            layout_placements={inst_a.id: pl_a, inst_b.id: pl_b},
            instances={inst_a.id: inst_a, inst_b.id: inst_b},
            components={"A": defn_a, "B": defn_b},
            schematic=sch,
        )
        assert result == []

    def test_wire_mid_segment_crosses_alien_pad(self) -> None:
        """Pad sits in the middle of a wire segment but at no vertex → still detected."""
        net_a = uuid.uuid4()
        net_b = uuid.uuid4()

        defn_a = _defn("A", 1)
        inst_a = ComponentInstance.new(definition_ref="A", refdes="A1", value="")
        pl_a = ComponentPlacement(
            component_id=inst_a.id, position=Point(0.0, 0.0), rotation=0, side="top"
        )

        defn_b = _defn("B", 1)
        inst_b = ComponentInstance.new(definition_ref="B", refdes="B1", value="")
        # B's pin sits at (2, 0) — midpoint of wire segment (0,0)→(4,0)
        pl_b = ComponentPlacement(
            component_id=inst_b.id, position=Point(2.0, 0.0), rotation=0, side="top"
        )

        # Wire goes from (0,0) to (4,0) — passes through B's pad at (2,0)
        wire = _wire((0.0, 0.0), (4.0, 0.0))

        sch = _schematic(
            (net_a, [(inst_a.id, 1)]),
            (net_b, [(inst_b.id, 1)]),
        )

        result = find_wires_crossing_pads(
            wires=[wire],
            moved_comp_ids={inst_a.id},
            layout_placements={inst_a.id: pl_a, inst_b.id: pl_b},
            instances={inst_a.id: inst_a, inst_b.id: inst_b},
            components={"A": defn_a, "B": defn_b},
            schematic=sch,
        )
        assert wire.id in result

    def test_wire_diagonal_near_alien_pad(self) -> None:
        """Diagonal wire, alien pad near the segment but not on a vertex → detected."""
        net_a = uuid.uuid4()
        net_b = uuid.uuid4()

        defn_a = _defn("A", 1)
        inst_a = ComponentInstance.new(definition_ref="A", refdes="A1", value="")
        pl_a = ComponentPlacement(
            component_id=inst_a.id, position=Point(0.0, 0.0), rotation=0, side="top"
        )

        defn_b = _defn("B", 1)
        inst_b = ComponentInstance.new(definition_ref="B", refdes="B1", value="")
        # B's pin at (2, 2) — wire goes diagonally from (0,0) to (4,4),
        # passing exactly through (2,2), which is within _CROSS_EPS=0.5
        pl_b = ComponentPlacement(
            component_id=inst_b.id, position=Point(2.0, 2.0), rotation=0, side="top"
        )

        wire = _wire((0.0, 0.0), (4.0, 4.0))

        sch = _schematic(
            (net_a, [(inst_a.id, 1)]),
            (net_b, [(inst_b.id, 1)]),
        )

        result = find_wires_crossing_pads(
            wires=[wire],
            moved_comp_ids={inst_a.id},
            layout_placements={inst_a.id: pl_a, inst_b.id: pl_b},
            instances={inst_a.id: inst_a, inst_b.id: inst_b},
            components={"A": defn_a, "B": defn_b},
            schematic=sch,
        )
        assert wire.id in result

    def test_wire_too_far_from_diagonal_pad(self) -> None:
        """Pad further than _CROSS_EPS from diagonal segment → no conflict."""
        net_a = uuid.uuid4()
        net_b = uuid.uuid4()

        defn_a = _defn("A", 1)
        inst_a = ComponentInstance.new(definition_ref="A", refdes="A1", value="")
        pl_a = ComponentPlacement(
            component_id=inst_a.id, position=Point(0.0, 0.0), rotation=0, side="top"
        )

        defn_b = _defn("B", 1)
        inst_b = ComponentInstance.new(definition_ref="B", refdes="B1", value="")
        # Wire from (0,0) to (4,0) — horizontal; B's pad at (2, 1.0)
        # Distance from (2, 1.0) to the segment is 1.0 > 0.5 → no conflict
        pl_b = ComponentPlacement(
            component_id=inst_b.id, position=Point(2.0, 1.0), rotation=0, side="top"
        )

        wire = _wire((0.0, 0.0), (4.0, 0.0))

        sch = _schematic(
            (net_a, [(inst_a.id, 1)]),
            (net_b, [(inst_b.id, 1)]),
        )

        result = find_wires_crossing_pads(
            wires=[wire],
            moved_comp_ids={inst_a.id},
            layout_placements={inst_a.id: pl_a, inst_b.id: pl_b},
            instances={inst_a.id: inst_a, inst_b.id: inst_b},
            components={"A": defn_a, "B": defn_b},
            schematic=sch,
        )
        assert result == []

    def test_both_unconnected_no_conflict(self) -> None:
        """Wire and pad both without a net → no conflict (free elements)."""
        defn_a = _defn("A", 1)
        inst_a = ComponentInstance.new(definition_ref="A", refdes="A1", value="")
        pl_a = ComponentPlacement(
            component_id=inst_a.id, position=Point(2.0, 0.0), rotation=0, side="top"
        )
        # Wire passes through A's pad, but neither has a net
        wire = _wire((0.0, 0.0), (4.0, 0.0))
        sch = _schematic()  # empty — no nets

        result = find_wires_crossing_pads(
            wires=[wire],
            moved_comp_ids={inst_a.id},
            layout_placements={inst_a.id: pl_a},
            instances={inst_a.id: inst_a},
            components={"A": defn_a},
            schematic=sch,
        )
        assert result == []

    def test_wire_has_net_pad_unconnected_is_conflict(self) -> None:
        """Wire has a net, pad is unconnected → conflict (could create unintended join)."""
        net_a = uuid.uuid4()

        defn_a = _defn("A", 1)
        inst_a = ComponentInstance.new(definition_ref="A", refdes="A1", value="")
        pl_a = ComponentPlacement(
            component_id=inst_a.id, position=Point(0.0, 0.0), rotation=0, side="top"
        )

        defn_b = _defn("B", 1)
        inst_b = ComponentInstance.new(definition_ref="B", refdes="B1", value="")
        pl_b = ComponentPlacement(
            component_id=inst_b.id, position=Point(2.0, 0.0), rotation=0, side="top"
        )

        # Wire goes through B's pad; wire carries net_a; B is unconnected
        wire = _wire((0.0, 0.0), (4.0, 0.0))
        sch = _schematic(
            (net_a, [(inst_a.id, 1)]),
        )

        result = find_wires_crossing_pads(
            wires=[wire],
            moved_comp_ids={inst_a.id},
            layout_placements={inst_a.id: pl_a, inst_b.id: pl_b},
            instances={inst_a.id: inst_a, inst_b.id: inst_b},
            components={"A": defn_a, "B": defn_b},
            schematic=sch,
        )
        assert wire.id in result
