# SPDX-License-Identifier: MIT
"""Tests for OwnershipResolver — the cohesive view over wire-ownership.

Guards two things:
1. Equivalence — resolver methods give the same answers as the free functions
   they delegate to (so the wrapper changed nothing behaviourally).
2. The value-add — one resolver instance reuses its ``_pin_world_cache`` across a
   batch, resolving each (component, pin) world position once (mirrors the
   test_perf_regressions idiom of counting pin_world_position calls).
"""

from __future__ import annotations

import uuid

from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.group import ComponentGroup
from protoboard.domain.layout.group_ownership import (
    OwnershipResolver,
    find_owned_wires,
    net_is_internal_to_group,
    wire_is_owned_by_group,
)
from protoboard.domain.layout.knot import JointKnot, Knot, PinKnot
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import KnotRef, Wire
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point


def _defn() -> ComponentDefinition:
    return ComponentDefinition.from_footprint(name="R", footprint=SIL(2), pin_names={1: "1", 2: "2"})


def _pin_knot(component_id: uuid.UUID, pin_number: int) -> PinKnot:
    return PinKnot(id=uuid.uuid4(), pin=PinRef(component_id=component_id, pin_number=pin_number))


def _wire(a: Knot, b: Knot, path: list[Point]) -> Wire:
    return Wire(id=uuid.uuid4(), path=path, a=KnotRef(id=a.id), b=KnotRef(id=b.id))


def _two_pin_group_scene() -> tuple[
    OwnershipResolver, ComponentGroup, frozenset[uuid.UUID], list[Wire], dict
]:
    """Group of two SIL(2) components, a wire between two of their pins (owned)."""
    defn = _defn()
    c1 = ComponentInstance.new(definition_ref="R", refdes="R1", value="").bind(defn, {})
    c2 = ComponentInstance.new(definition_ref="R", refdes="R2", value="").bind(defn, {})
    # SIL(2) rot=90 → pin1@(0.5,0.5), pin2@(1.5,0.5).
    pl1 = ComponentPlacement(component_id=c1.id, position=Point(0.5, 0.5), rotation=90, side="top")
    pl2 = ComponentPlacement(component_id=c2.id, position=Point(0.5, 2.5), rotation=90, side="top")
    placements = {c1.id: pl1, c2.id: pl2}
    instances = {c1.id: c1, c2.id: c2}
    definitions = {"R": defn}

    ka = _pin_knot(c1.id, 1)  # at (0.5, 0.5)
    kb = _pin_knot(c2.id, 1)  # at (0.5, 2.5)
    knots: dict[uuid.UUID, Knot] = {ka.id: ka, kb.id: kb}
    wire = _wire(ka, kb, [Point(0.5, 0.5), Point(0.5, 2.5)])

    group_comp_ids = frozenset({c1.id, c2.id})
    group = ComponentGroup(id=uuid.uuid4(), name="G", component_ids=group_comp_ids)

    net = Net(id=uuid.uuid4(), name="N", pins={PinRef(c1.id, 1), PinRef(c2.id, 1)})
    sch = Schematic(nets={net.id: net})
    wire_net_map = {wire.id: net.id}

    resolver = OwnershipResolver(
        knots=knots, wire_net_map=wire_net_map, schematic=sch,
        placements=placements, instances=instances, definitions=definitions,
    )
    ctx = dict(
        wire_net_map=wire_net_map, knots=knots, schematic=sch,
        placements=placements, instances=instances, definitions=definitions,
    )
    return resolver, group, group_comp_ids, [wire], ctx


class TestEquivalence:
    def test_is_owned_matches_free_function(self) -> None:
        resolver, group, gids, wires, ctx = _two_pin_group_scene()
        w = wires[0]
        assert resolver.is_owned(w, group, gids) is wire_is_owned_by_group(
            w, group, group_comp_ids=gids, **ctx
        )
        assert resolver.is_owned(w, group, gids) is True  # both endpoints group pins

    def test_owned_wire_ids_matches_find_owned_wires(self) -> None:
        resolver, group, gids, wires, ctx = _two_pin_group_scene()
        assert resolver.owned_wire_ids(wires, group, gids) == find_owned_wires(
            wires, group, group_comp_ids=gids, **ctx
        )

    def test_net_is_internal_matches_free_function(self) -> None:
        resolver, group, gids, wires, ctx = _two_pin_group_scene()
        net_id = next(iter(ctx["schematic"].nets))
        assert resolver.net_is_internal(net_id, group) == net_is_internal_to_group(
            net_id, group, ctx["schematic"]
        )

    def test_group_bbox_matches_free_function(self) -> None:
        resolver, group, gids, wires, ctx = _two_pin_group_scene()
        from protoboard.domain.layout.group_ownership import group_bbox_with_owned_wires
        assert resolver.group_bbox(group, gids, wires) == group_bbox_with_owned_wires(
            group, gids, ctx["placements"], ctx["instances"], ctx["definitions"], wires,
            wire_net_map=ctx["wire_net_map"], knots=ctx["knots"], schematic=ctx["schematic"],
        )


class TestCacheReuse:
    def test_pin_world_position_resolved_once_per_pin_across_batch(
        self, monkeypatch: object
    ) -> None:
        # Several wires fanning out from the SAME pin; one resolver should resolve
        # that pin's world position once, not once per wire.
        from protoboard.domain.layout import placement as plc_mod

        _resolver, group, gids, wires, ctx = _two_pin_group_scene()
        # Add 3 more wires sharing pin1 of the first component (same endpoint).
        ka = next(iter(ctx["knots"].values()))  # the (c1,1) pin knot
        kb_extra = []
        for _ in range(3):
            jk = JointKnot(id=uuid.uuid4(), position=Point(0.5, 5.5))
            ctx["knots"][jk.id] = jk
            w = _wire(ka, jk, [Point(0.5, 0.5), Point(0.5, 5.5)])
            wires.append(w)
            kb_extra.append(jk)

        original = plc_mod.pin_world_position
        calls: dict[tuple, int] = {}

        def counting(footprint, placement, pin_number):  # type: ignore[no-untyped-def]
            calls[(placement.component_id, pin_number)] = (
                calls.get((placement.component_id, pin_number), 0) + 1
            )
            return original(footprint, placement, pin_number)

        monkeypatch.setattr(plc_mod, "pin_world_position", counting)  # type: ignore[attr-defined]

        fresh = OwnershipResolver(
            knots=ctx["knots"], wire_net_map=ctx["wire_net_map"], schematic=ctx["schematic"],
            placements=ctx["placements"], instances=ctx["instances"], definitions=ctx["definitions"],
        )
        fresh.owned_wire_ids(wires, group, gids)

        # Pin (c1, 1) is the shared endpoint of 4 wires; with the batch cache it
        # is resolved at most a small constant number of times, NOT once per wire.
        shared_calls = max(
            (v for k, v in calls.items() if k[1] == 1), default=0
        )
        assert shared_calls <= 2, f"pin world pos resolved {shared_calls}× — cache not reused"
