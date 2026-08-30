# SPDX-License-Identifier: MIT
"""Tests for protoboard.domain.layout.group_ownership.

Covers the net-aware "wire ∈ group?" classification taxonomy:

* net_is_internal_to_group — net entirely inside group (no exported pins,
  not global).
* wire_is_owned_by_group — wire structurally belongs to group:
    1. both endpoints PinKnots of group pins (owned regardless of net)
    2. one PinKnot of group + one JointKnot tail; owned iff net internal
    3. T-junction tail of exported net — NOT owned
    4. orphan in bbox — NOT owned
    5. external pin endpoint — NOT owned (DRC short case, classification
       clean)
* group_bbox_with_owned_wires — bbox includes owned wires, excludes
  non-owned.
"""

from __future__ import annotations

import uuid

from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.group import ComponentGroup
from protoboard.domain.layout.group_ownership import (
    group_bbox_with_owned_wires,
    net_is_internal_to_group,
    wire_in_group_bbox,
    wire_is_owned_by_group,
)
from protoboard.domain.layout.knot import JointKnot, PinKnot
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import KnotRef, Wire
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from tests.unit._autoroute_invariants import make_sil, sil_families

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _defn(name: str = "R") -> ComponentDefinition:
    return make_sil(name, 2)


def _inst(defn: ComponentDefinition, refdes: str) -> ComponentInstance:
    return ComponentInstance.new(
        definition_ref=defn.name,
        refdes=refdes,
        value="",
    ).bind(defn, sil_families(defn))


def _pin_knot(component_id: uuid.UUID, pin_number: int) -> PinKnot:
    return PinKnot(
        id=uuid.uuid4(),
        pin=PinRef(component_id=component_id, pin_number=pin_number),
    )


def _joint_knot(position: Point) -> JointKnot:
    return JointKnot(id=uuid.uuid4(), position=position)


def _wire(
    a: PinKnot | JointKnot,
    b: PinKnot | JointKnot,
    path: list[Point],
) -> Wire:
    return Wire(
        id=uuid.uuid4(),
        path=path,
        a=KnotRef(id=a.id),
        b=KnotRef(id=b.id),
    )


def _make_group(
    comp_ids: frozenset[uuid.UUID],
    exported: frozenset[PinRef] = frozenset(),
) -> ComponentGroup:
    return ComponentGroup(
        id=uuid.uuid4(),
        name="G",
        component_ids=comp_ids,
        exported_pins=exported,
    )


def _net(
    name: str,
    pins: set[PinRef],
    global_net: bool = False,
) -> Net:
    return Net(id=uuid.uuid4(), name=name, pins=pins, global_net=global_net)


# ---------------------------------------------------------------------------
# net_is_internal_to_group
# ---------------------------------------------------------------------------


class TestNetIsInternalToGroup:
    def test_net_entirely_inside_group_no_exports_is_internal(self) -> None:
        c1, c2 = uuid.uuid4(), uuid.uuid4()
        group = _make_group(frozenset({c1, c2}))
        net = _net("N1", {PinRef(c1, 1), PinRef(c2, 1)})
        sch = Schematic(nets={net.id: net})
        assert net_is_internal_to_group(net.id, group, sch) is True

    def test_net_with_exported_pin_is_not_internal(self) -> None:
        c1, c2 = uuid.uuid4(), uuid.uuid4()
        group = _make_group(
            frozenset({c1, c2}),
            exported=frozenset({PinRef(c1, 1)}),
        )
        net = _net("N1", {PinRef(c1, 1), PinRef(c2, 1)})
        sch = Schematic(nets={net.id: net})
        assert net_is_internal_to_group(net.id, group, sch) is False

    def test_net_touching_external_component_is_not_internal(self) -> None:
        c1, c_ext = uuid.uuid4(), uuid.uuid4()
        group = _make_group(frozenset({c1}))
        net = _net("N1", {PinRef(c1, 1), PinRef(c_ext, 1)})
        sch = Schematic(nets={net.id: net})
        # External pin in net → either exported (interface) or DRC short.
        # Either way, the net is not "internal to the group".
        assert net_is_internal_to_group(net.id, group, sch) is False

    def test_global_net_is_never_internal(self) -> None:
        c1 = uuid.uuid4()
        group = _make_group(frozenset({c1}))
        net = _net("VCC", {PinRef(c1, 1)}, global_net=True)
        sch = Schematic(nets={net.id: net})
        assert net_is_internal_to_group(net.id, group, sch) is False

    def test_none_net_id_is_not_internal(self) -> None:
        c1 = uuid.uuid4()
        group = _make_group(frozenset({c1}))
        sch = Schematic(nets={})
        assert net_is_internal_to_group(None, group, sch) is False

    def test_net_without_any_group_pin_is_not_internal(self) -> None:
        c1, c_other = uuid.uuid4(), uuid.uuid4()
        group = _make_group(frozenset({c1}))
        net = _net("N1", {PinRef(c_other, 1)})
        sch = Schematic(nets={net.id: net})
        assert net_is_internal_to_group(net.id, group, sch) is False


# ---------------------------------------------------------------------------
# wire_is_owned_by_group
# ---------------------------------------------------------------------------


class TestWireOwnedBothPinKnots:
    def test_wire_with_two_group_pin_endpoints_is_owned(self) -> None:
        c1, c2 = uuid.uuid4(), uuid.uuid4()
        group = _make_group(frozenset({c1, c2}))
        sch = Schematic(nets={})

        ka = _pin_knot(c1, 1)
        kb = _pin_knot(c2, 1)
        w = _wire(ka, kb, [Point(0, 0), Point(1, 0)])
        knots = {ka.id: ka, kb.id: kb}

        assert (
            wire_is_owned_by_group(
                w,
                group,
                wire_net_map={},
                knots=knots,  # type: ignore[arg-type]
                group_comp_ids=frozenset({c1, c2}),
                schematic=sch,
            )
            is True
        )

    def test_wire_with_one_external_pin_is_not_owned(self) -> None:
        c_in, c_out = uuid.uuid4(), uuid.uuid4()
        group = _make_group(frozenset({c_in}))
        sch = Schematic(nets={})

        ka = _pin_knot(c_in, 1)
        kb = _pin_knot(c_out, 1)
        w = _wire(ka, kb, [Point(0, 0), Point(1, 0)])
        knots = {ka.id: ka, kb.id: kb}

        assert (
            wire_is_owned_by_group(
                w,
                group,
                wire_net_map={},
                knots=knots,  # type: ignore[arg-type]
                group_comp_ids=frozenset({c_in}),
                schematic=sch,
            )
            is False
        )

    def test_wire_with_two_pinknots_owned_even_if_net_exported(self) -> None:
        # Both endpoints on group pins → owned, even if the net has
        # exported pins reaching outside.  Net identity is irrelevant
        # for the "both anchored" case.
        c1, c2 = uuid.uuid4(), uuid.uuid4()
        group = _make_group(
            frozenset({c1, c2}),
            exported=frozenset({PinRef(c1, 1)}),
        )
        net = _net("INT", {PinRef(c1, 1), PinRef(c2, 1)})
        sch = Schematic(nets={net.id: net})

        ka = _pin_knot(c1, 1)
        kb = _pin_knot(c2, 1)
        w = _wire(ka, kb, [Point(0, 0), Point(1, 0)])
        knots = {ka.id: ka, kb.id: kb}

        assert (
            wire_is_owned_by_group(
                w,
                group,
                wire_net_map={w.id: net.id},
                knots=knots,  # type: ignore[arg-type]
                group_comp_ids=frozenset({c1, c2}),
                schematic=sch,
            )
            is True
        )


class TestWireOwnedHalfOrphan:
    def test_half_orphan_internal_net_is_owned(self) -> None:
        c1, c2 = uuid.uuid4(), uuid.uuid4()
        group = _make_group(frozenset({c1, c2}))
        net = _net("INT", {PinRef(c1, 1), PinRef(c2, 1)})
        sch = Schematic(nets={net.id: net})

        ka = _pin_knot(c1, 1)
        kb = _joint_knot(Point(2, 0))
        w = _wire(ka, kb, [Point(0, 0), Point(2, 0)])
        knots = {ka.id: ka, kb.id: kb}

        assert (
            wire_is_owned_by_group(
                w,
                group,
                wire_net_map={w.id: net.id},
                knots=knots,  # type: ignore[arg-type]
                group_comp_ids=frozenset({c1, c2}),
                schematic=sch,
            )
            is True
        )

    def test_half_orphan_exported_net_is_not_owned(self) -> None:
        c1, c2 = uuid.uuid4(), uuid.uuid4()
        group = _make_group(
            frozenset({c1, c2}),
            exported=frozenset({PinRef(c1, 1)}),
        )
        net = _net("EXP", {PinRef(c1, 1), PinRef(c2, 1)})
        sch = Schematic(nets={net.id: net})

        ka = _pin_knot(c1, 1)
        kb = _joint_knot(Point(2, 0))
        w = _wire(ka, kb, [Point(0, 0), Point(2, 0)])
        knots = {ka.id: ka, kb.id: kb}

        # The half-orphan tail hangs off an exported net — interface
        # wire, not internal.
        assert (
            wire_is_owned_by_group(
                w,
                group,
                wire_net_map={w.id: net.id},
                knots=knots,  # type: ignore[arg-type]
                group_comp_ids=frozenset({c1, c2}),
                schematic=sch,
            )
            is False
        )

    def test_half_orphan_global_net_is_not_owned(self) -> None:
        c1 = uuid.uuid4()
        group = _make_group(frozenset({c1}))
        net = _net("VCC", {PinRef(c1, 1)}, global_net=True)
        sch = Schematic(nets={net.id: net})

        ka = _pin_knot(c1, 1)
        kb = _joint_knot(Point(2, 0))
        w = _wire(ka, kb, [Point(0, 0), Point(2, 0)])
        knots = {ka.id: ka, kb.id: kb}

        assert (
            wire_is_owned_by_group(
                w,
                group,
                wire_net_map={w.id: net.id},
                knots=knots,  # type: ignore[arg-type]
                group_comp_ids=frozenset({c1}),
                schematic=sch,
            )
            is False
        )

    def test_half_orphan_no_resolvable_net_is_not_owned(self) -> None:
        # Wire's net cannot be resolved (wire_net_map miss).  Pure tail
        # without inheritance → not owned.
        c1 = uuid.uuid4()
        group = _make_group(frozenset({c1}))
        sch = Schematic(nets={})

        ka = _pin_knot(c1, 1)
        kb = _joint_knot(Point(2, 0))
        w = _wire(ka, kb, [Point(0, 0), Point(2, 0)])
        knots = {ka.id: ka, kb.id: kb}

        assert (
            wire_is_owned_by_group(
                w,
                group,
                wire_net_map={},
                knots=knots,  # type: ignore[arg-type]
                group_comp_ids=frozenset({c1}),
                schematic=sch,
            )
            is False
        )


class TestWireOwnedOrphan:
    def test_orphan_wire_no_net_is_not_owned(self) -> None:
        c1 = uuid.uuid4()
        group = _make_group(frozenset({c1}))
        sch = Schematic(nets={})

        ka = _joint_knot(Point(0, 0))
        kb = _joint_knot(Point(2, 0))
        w = _wire(ka, kb, [Point(0, 0), Point(2, 0)])
        knots = {ka.id: ka, kb.id: kb}

        assert (
            wire_is_owned_by_group(
                w,
                group,
                wire_net_map={},
                knots=knots,  # type: ignore[arg-type]
                group_comp_ids=frozenset({c1}),
                schematic=sch,
            )
            is False
        )

    def test_orphan_wire_on_internal_net_pads_is_still_not_owned(self) -> None:
        # Full-orphan: both endpoints are JointKnots, no PinKnot anchor.
        # Even if wire_net_map resolves the orphan to a net internal to
        # the group (because the orphan happens to sit on pads of that
        # net — e.g. a legacy fragment left after partial RemoveWire),
        # the wire is NOT owned by the group.  Per 2026-06-01 design
        # discussion: orphans have no structural affiliation, can be
        # deleted from outside or adopted into any net.
        c1, c2 = uuid.uuid4(), uuid.uuid4()
        group = _make_group(frozenset({c1, c2}))
        net = _net("INT", {PinRef(c1, 1), PinRef(c2, 1)})
        sch = Schematic(nets={net.id: net})

        ka = _joint_knot(Point(0, 0))
        kb = _joint_knot(Point(2, 0))
        w = _wire(ka, kb, [Point(0, 0), Point(2, 0)])
        knots = {ka.id: ka, kb.id: kb}

        assert (
            wire_is_owned_by_group(
                w,
                group,
                wire_net_map={w.id: net.id},
                knots=knots,  # type: ignore[arg-type]
                group_comp_ids=frozenset({c1, c2}),
                schematic=sch,
            )
            is False
        )


class TestWireOwnedShortCase:
    def test_external_pin_to_internal_net_is_not_owned(self) -> None:
        # DRC short case: external pin endpoint, but net (if inherited)
        # would be internal.  Classification stays clean: external pin
        # endpoint disqualifies the wire from ownership.  DRC reports
        # the short separately.
        c_in, c_out = uuid.uuid4(), uuid.uuid4()
        group = _make_group(frozenset({c_in}))
        net = _net("INT", {PinRef(c_in, 1)})
        sch = Schematic(nets={net.id: net})

        ka = _pin_knot(c_in, 1)
        kb = _pin_knot(c_out, 1)
        w = _wire(ka, kb, [Point(0, 0), Point(2, 0)])
        knots = {ka.id: ka, kb.id: kb}

        assert (
            wire_is_owned_by_group(
                w,
                group,
                wire_net_map={w.id: net.id},
                knots=knots,  # type: ignore[arg-type]
                group_comp_ids=frozenset({c_in}),
                schematic=sch,
            )
            is False
        )


# ---------------------------------------------------------------------------
# wire_in_group_bbox (diagnostic helper)
# ---------------------------------------------------------------------------


class TestWireInGroupBbox:
    def test_wire_entirely_inside_bbox(self) -> None:
        c1 = uuid.uuid4()
        ka = _pin_knot(c1, 1)
        kb = _pin_knot(c1, 2)
        w = _wire(ka, kb, [Point(2.0, 2.0), Point(3.0, 2.0)])
        assert wire_in_group_bbox(w, (0.0, 0.0, 5.0, 5.0)) is True

    def test_wire_extending_outside_bbox(self) -> None:
        c1 = uuid.uuid4()
        ka = _pin_knot(c1, 1)
        kb = _pin_knot(c1, 2)
        w = _wire(ka, kb, [Point(2.0, 2.0), Point(7.0, 2.0)])
        assert wire_in_group_bbox(w, (0.0, 0.0, 5.0, 5.0)) is False

    def test_tolerance_allows_edge_overlap(self) -> None:
        c1 = uuid.uuid4()
        ka = _pin_knot(c1, 1)
        kb = _pin_knot(c1, 2)
        w = _wire(ka, kb, [Point(0.0, 0.0), Point(5.3, 5.3)])
        # tol=0.5 allows 5.3 (5.0 + 0.3).
        assert wire_in_group_bbox(w, (0.0, 0.0, 5.0, 5.0), tol=0.5) is True
        # tol=0.1 doesn't.
        assert wire_in_group_bbox(w, (0.0, 0.0, 5.0, 5.0), tol=0.1) is False


# ---------------------------------------------------------------------------
# group_bbox_with_owned_wires
# ---------------------------------------------------------------------------


class TestGroupBboxWithOwnedWires:
    def test_bbox_covers_group_pins_only_when_no_wires(self) -> None:
        defn = _defn()
        i1 = _inst(defn, "R1")
        c1 = i1.id
        placements = {
            c1: ComponentPlacement(
                component_id=c1,
                position=Point(2.0, 3.0),
                rotation=0,
                side="top",
            )
        }
        instances = {c1: i1}
        components = {defn.name: defn}

        group = _make_group(frozenset({c1}))
        sch = Schematic(nets={})

        bbox = group_bbox_with_owned_wires(
            group,
            frozenset({c1}),
            placements,
            instances,
            components,
            [],
            wire_net_map={},
            knots={},
            schematic=sch,
            pad=0.5,
        )

        # SIL(2): pin 1 at offset (0, 0), pin 2 at offset (0, 1) before
        # rotation.  With placement at (2.0, 3.0): pins at (2.0, 3.0)
        # and (2.0, 4.0).  Bbox with pad=0.5: (1.5, 2.5, 2.5, 4.5).
        assert bbox is not None
        x0, y0, x1, y1 = bbox
        assert x0 == 1.5
        assert y0 == 2.5
        assert x1 == 2.5
        assert y1 == 4.5

    def test_bbox_includes_owned_wire_vertices(self) -> None:
        defn = _defn()
        i1 = _inst(defn, "R1")
        i2 = _inst(defn, "R2")
        c1, c2 = i1.id, i2.id
        placements = {
            c1: ComponentPlacement(
                component_id=c1,
                position=Point(0.0, 0.0),
                rotation=0,
                side="top",
            ),
            c2: ComponentPlacement(
                component_id=c2,
                position=Point(2.0, 0.0),
                rotation=0,
                side="top",
            ),
        }
        instances = {c1: i1, c2: i2}
        components = {defn.name: defn}

        group = _make_group(frozenset({c1, c2}))
        sch = Schematic(nets={})

        # Wire stretching far away — but owned (both PinKnots in group).
        ka = _pin_knot(c1, 1)
        kb = _pin_knot(c2, 1)
        w = _wire(ka, kb, [Point(0, 0), Point(10, 0), Point(2, 0)])
        knots = {ka.id: ka, kb.id: kb}

        bbox = group_bbox_with_owned_wires(
            group,
            frozenset({c1, c2}),
            placements,
            instances,
            components,
            [w],
            wire_net_map={},
            knots=knots,  # type: ignore[arg-type]
            schematic=sch,
            pad=0.0,
        )
        assert bbox is not None
        x0, _, x1, _ = bbox
        # Wire midpoint at x=10 should extend bbox.
        assert x1 >= 10.0
        assert x0 <= 0.0

    def test_bbox_excludes_non_owned_wire_vertices(self) -> None:
        defn = _defn()
        i1 = _inst(defn, "R1")
        i_ext = _inst(defn, "R2")
        c1, c_ext = i1.id, i_ext.id
        placements = {
            c1: ComponentPlacement(
                component_id=c1,
                position=Point(0.0, 0.0),
                rotation=0,
                side="top",
            ),
            c_ext: ComponentPlacement(
                component_id=c_ext,
                position=Point(5.0, 0.0),
                rotation=0,
                side="top",
            ),
        }
        instances = {c1: i1, c_ext: i_ext}
        components = {defn.name: defn}

        group = _make_group(frozenset({c1}))
        sch = Schematic(nets={})

        # Wire crossing group bbox but anchored to external component
        # — must NOT extend group bbox.
        ka = _pin_knot(c1, 1)
        kb = _pin_knot(c_ext, 1)
        w = _wire(ka, kb, [Point(0, 0), Point(-5, 0), Point(5, 0)])
        knots = {ka.id: ka, kb.id: kb}

        bbox = group_bbox_with_owned_wires(
            group,
            frozenset({c1}),
            placements,
            instances,
            components,
            [w],
            wire_net_map={},
            knots=knots,  # type: ignore[arg-type]
            schematic=sch,
            pad=0.0,
        )
        assert bbox is not None
        x0, _, x1, _ = bbox
        # Without owned wire contribution, bbox extends only over c1
        # pins (SIL(2) at origin) — x in [0, 0].
        assert x0 == 0.0
        assert x1 == 0.0

    def test_empty_group_returns_none(self) -> None:
        group = _make_group(frozenset())
        sch = Schematic(nets={})
        bbox = group_bbox_with_owned_wires(
            group,
            frozenset(),
            {},
            {},
            {},
            [],
            wire_net_map={},
            knots={},
            schematic=sch,
        )
        assert bbox is None
