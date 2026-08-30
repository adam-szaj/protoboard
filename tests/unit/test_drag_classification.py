# SPDX-License-Identifier: MIT
"""Tests for protoboard.domain.layout.drag_classification.

Covers the 5-way disjoint partition of wires under a hypothetical drag:

* ``internal`` — both PinKnots in drag set
* ``cross_group_disconnect`` — one PinKnot in drag set + other foreign PinKnot
* ``half_orphan_internal`` — one PinKnot in drag set + JointKnot tail, net internal
* ``half_orphan_external`` — one PinKnot in drag set + JointKnot tail, net not internal
* ``external`` — neither endpoint anchored to drag set
"""

from __future__ import annotations

import uuid

from protoboard.domain.layout.drag_classification import (
    DragWireClassification,
    classify_wires_for_drag,
)
from protoboard.domain.layout.group import ComponentGroup
from protoboard.domain.layout.knot import JointKnot, PinKnot
from protoboard.domain.layout.wire import KnotRef, Wire
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point

# ---------------------------------------------------------------------------
# Builders (mirrors test_group_ownership.py for consistency)
# ---------------------------------------------------------------------------


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


def _group(
    comp_ids: frozenset[uuid.UUID],
    exported: frozenset[PinRef] = frozenset(),
) -> ComponentGroup:
    return ComponentGroup(
        id=uuid.uuid4(),
        name="G",
        component_ids=comp_ids,
        exported_pins=exported,
    )


def _net(name: str, pins: set[PinRef], global_net: bool = False) -> Net:
    return Net(id=uuid.uuid4(), name=name, pins=pins, global_net=global_net)


# ---------------------------------------------------------------------------
# internal bucket
# ---------------------------------------------------------------------------


class TestInternal:
    def test_both_pinknots_in_drag_set(self) -> None:
        c1, c2 = uuid.uuid4(), uuid.uuid4()
        ka = _pin_knot(c1, 1)
        kb = _pin_knot(c2, 1)
        w = _wire(ka, kb, [Point(0, 0), Point(1, 0)])
        knots = {ka.id: ka, kb.id: kb}

        cls = classify_wires_for_drag(
            wires=[w],
            drag_set_members=frozenset({c1, c2}),
            knots=knots,  # type: ignore[arg-type]
            wire_net_map={},
            schematic=Schematic(nets={}),
            member_groups=[],
        )
        assert cls.internal == frozenset({w.id})
        assert cls.cross_group_disconnect == frozenset()
        assert cls.half_orphan_internal == frozenset()
        assert cls.half_orphan_external == frozenset()
        assert cls.external == frozenset()

    def test_single_component_loopback_wire_is_internal(self) -> None:
        # R1.pin1 ↔ R1.pin2 — degenerate drag set of one component.
        c1 = uuid.uuid4()
        ka = _pin_knot(c1, 1)
        kb = _pin_knot(c1, 2)
        w = _wire(ka, kb, [Point(0, 0), Point(1, 0)])
        knots = {ka.id: ka, kb.id: kb}

        cls = classify_wires_for_drag(
            wires=[w],
            drag_set_members=frozenset({c1}),
            knots=knots,  # type: ignore[arg-type]
            wire_net_map={},
            schematic=Schematic(nets={}),
            member_groups=[],
        )
        assert cls.internal == frozenset({w.id})

    def test_multi_selection_drag_set_with_wire_between_members(self) -> None:
        c1, c2 = uuid.uuid4(), uuid.uuid4()
        ka = _pin_knot(c1, 1)
        kb = _pin_knot(c2, 2)
        w = _wire(ka, kb, [Point(0, 0), Point(2, 2)])
        knots = {ka.id: ka, kb.id: kb}

        cls = classify_wires_for_drag(
            wires=[w],
            drag_set_members=frozenset({c1, c2}),
            knots=knots,  # type: ignore[arg-type]
            wire_net_map={},
            schematic=Schematic(nets={}),
            # No member_groups for ad-hoc multi-selection — degenerate.
            member_groups=[],
        )
        assert cls.internal == frozenset({w.id})


# ---------------------------------------------------------------------------
# cross_group_disconnect bucket
# ---------------------------------------------------------------------------


class TestCrossGroupDisconnect:
    def test_one_pin_in_drag_one_pin_outside(self) -> None:
        c_in, c_out = uuid.uuid4(), uuid.uuid4()
        ka = _pin_knot(c_in, 1)
        kb = _pin_knot(c_out, 1)
        w = _wire(ka, kb, [Point(0, 0), Point(5, 0)])
        knots = {ka.id: ka, kb.id: kb}

        cls = classify_wires_for_drag(
            wires=[w],
            drag_set_members=frozenset({c_in}),
            knots=knots,  # type: ignore[arg-type]
            wire_net_map={},
            schematic=Schematic(nets={}),
            member_groups=[],
        )
        assert cls.cross_group_disconnect == frozenset({w.id})
        assert cls.internal == frozenset()
        assert cls.external == frozenset()

    def test_disconnect_classification_independent_of_net(self) -> None:
        # Cross-group disconnect should fire even if the net happens to
        # be "internal" — both PinKnot endpoints decide ownership.
        c_in, c_out = uuid.uuid4(), uuid.uuid4()
        group = _group(frozenset({c_in}))
        net = _net("N", {PinRef(c_in, 1), PinRef(c_out, 1)})

        ka = _pin_knot(c_in, 1)
        kb = _pin_knot(c_out, 1)
        w = _wire(ka, kb, [Point(0, 0), Point(5, 0)])
        knots = {ka.id: ka, kb.id: kb}

        cls = classify_wires_for_drag(
            wires=[w],
            drag_set_members=frozenset({c_in}),
            knots=knots,  # type: ignore[arg-type]
            wire_net_map={w.id: net.id},
            schematic=Schematic(nets={net.id: net}),
            member_groups=[group],
        )
        assert cls.cross_group_disconnect == frozenset({w.id})


# ---------------------------------------------------------------------------
# half_orphan buckets
# ---------------------------------------------------------------------------


class TestHalfOrphan:
    def test_half_orphan_internal_net_travels_with_drag(self) -> None:
        # Group with one component c1, internal net (no exports, not global).
        # Wire from c1.pin1 (PinKnot) to a JointKnot — net internal → half_orphan_internal.  # noqa: E501
        c1, c2 = uuid.uuid4(), uuid.uuid4()
        group = _group(frozenset({c1, c2}))
        net = _net("INTERNAL", {PinRef(c1, 1), PinRef(c2, 1)})

        ka = _pin_knot(c1, 1)
        jb = _joint_knot(Point(2, 2))
        w = _wire(ka, jb, [Point(0, 0), Point(2, 2)])
        knots = {ka.id: ka, jb.id: jb}

        cls = classify_wires_for_drag(
            wires=[w],
            drag_set_members=frozenset({c1, c2}),
            knots=knots,  # type: ignore[arg-type]
            wire_net_map={w.id: net.id},
            schematic=Schematic(nets={net.id: net}),
            member_groups=[group],
        )
        assert cls.half_orphan_internal == frozenset({w.id})
        assert cls.travels_with_drag == frozenset({w.id})

    def test_half_orphan_exported_net_is_external_drop(self) -> None:
        # Group exports c1.pin1 — net is interface, NOT internal.
        # Half-orphan tail of this net is external → dropped on drag.
        c1, c2 = uuid.uuid4(), uuid.uuid4()
        group = _group(
            frozenset({c1, c2}),
            exported=frozenset({PinRef(c1, 1)}),
        )
        net = _net("EXPORTED", {PinRef(c1, 1), PinRef(c2, 1)})

        ka = _pin_knot(c1, 1)
        jb = _joint_knot(Point(2, 2))
        w = _wire(ka, jb, [Point(0, 0), Point(2, 2)])
        knots = {ka.id: ka, jb.id: jb}

        cls = classify_wires_for_drag(
            wires=[w],
            drag_set_members=frozenset({c1, c2}),
            knots=knots,  # type: ignore[arg-type]
            wire_net_map={w.id: net.id},
            schematic=Schematic(nets={net.id: net}),
            member_groups=[group],
        )
        assert cls.half_orphan_external == frozenset({w.id})
        assert cls.half_orphan_internal == frozenset()

    def test_half_orphan_global_net_is_external_drop(self) -> None:
        c1 = uuid.uuid4()
        group = _group(frozenset({c1}))
        net = _net("VCC", {PinRef(c1, 1)}, global_net=True)

        ka = _pin_knot(c1, 1)
        jb = _joint_knot(Point(2, 2))
        w = _wire(ka, jb, [Point(0, 0), Point(2, 2)])
        knots = {ka.id: ka, jb.id: jb}

        cls = classify_wires_for_drag(
            wires=[w],
            drag_set_members=frozenset({c1}),
            knots=knots,  # type: ignore[arg-type]
            wire_net_map={w.id: net.id},
            schematic=Schematic(nets={net.id: net}),
            member_groups=[group],
        )
        assert cls.half_orphan_external == frozenset({w.id})

    def test_half_orphan_unresolvable_net_is_external_drop(self) -> None:
        # wire_net_map missing entry — half-orphan falls to external.
        c1 = uuid.uuid4()
        ka = _pin_knot(c1, 1)
        jb = _joint_knot(Point(1, 1))
        w = _wire(ka, jb, [Point(0, 0), Point(1, 1)])
        knots = {ka.id: ka, jb.id: jb}

        cls = classify_wires_for_drag(
            wires=[w],
            drag_set_members=frozenset({c1}),
            knots=knots,  # type: ignore[arg-type]
            wire_net_map={},
            schematic=Schematic(nets={}),
            member_groups=[],
        )
        assert cls.half_orphan_external == frozenset({w.id})

    def test_half_orphan_without_member_groups_is_external(self) -> None:
        # Multi-selection of free components — no member_groups means
        # nothing can be "internal".  Half-orphan tail → external drop.
        c1 = uuid.uuid4()
        ka = _pin_knot(c1, 1)
        jb = _joint_knot(Point(1, 1))
        w = _wire(ka, jb, [Point(0, 0), Point(1, 1)])
        knots = {ka.id: ka, jb.id: jb}
        net = _net("N", {PinRef(c1, 1)})

        cls = classify_wires_for_drag(
            wires=[w],
            drag_set_members=frozenset({c1}),
            knots=knots,  # type: ignore[arg-type]
            wire_net_map={w.id: net.id},
            schematic=Schematic(nets={net.id: net}),
            member_groups=[],
        )
        assert cls.half_orphan_external == frozenset({w.id})


# ---------------------------------------------------------------------------
# external bucket
# ---------------------------------------------------------------------------


class TestExternal:
    def test_wire_with_no_drag_set_endpoint_is_external(self) -> None:
        c1, c2 = uuid.uuid4(), uuid.uuid4()
        c_drag = uuid.uuid4()
        ka = _pin_knot(c1, 1)
        kb = _pin_knot(c2, 1)
        w = _wire(ka, kb, [Point(0, 0), Point(1, 0)])
        knots = {ka.id: ka, kb.id: kb}

        cls = classify_wires_for_drag(
            wires=[w],
            drag_set_members=frozenset({c_drag}),
            knots=knots,  # type: ignore[arg-type]
            wire_net_map={},
            schematic=Schematic(nets={}),
            member_groups=[],
        )
        assert cls.external == frozenset({w.id})

    def test_wire_with_both_joint_endpoints_is_external(self) -> None:
        # Full orphan (no PinKnot anchor) — never travels with any drag.
        c1 = uuid.uuid4()
        ja = _joint_knot(Point(0, 0))
        jb = _joint_knot(Point(1, 1))
        w = _wire(ja, jb, [Point(0, 0), Point(1, 1)])
        knots = {ja.id: ja, jb.id: jb}

        cls = classify_wires_for_drag(
            wires=[w],
            drag_set_members=frozenset({c1}),
            knots=knots,  # type: ignore[arg-type]
            wire_net_map={},
            schematic=Schematic(nets={}),
            member_groups=[],
        )
        assert cls.external == frozenset({w.id})

    def test_wire_with_missing_knot_refs_is_external(self) -> None:
        # Defensive: wire whose KnotRef points at a missing knot.
        c1 = uuid.uuid4()
        ka = _pin_knot(c1, 1)
        kb = _pin_knot(c1, 2)
        w = _wire(ka, kb, [Point(0, 0), Point(1, 0)])
        # Intentionally drop kb from knots dict.
        knots = {ka.id: ka}

        cls = classify_wires_for_drag(
            wires=[w],
            drag_set_members=frozenset({c1}),
            knots=knots,  # type: ignore[arg-type]
            wire_net_map={},
            schematic=Schematic(nets={}),
            member_groups=[],
        )
        assert cls.external == frozenset({w.id})


# ---------------------------------------------------------------------------
# Aggregate — multiple wires of different kinds in one classify call
# ---------------------------------------------------------------------------


class TestAggregate:
    def test_disjoint_partition_covers_every_wire_once(self) -> None:
        c_in_a, c_in_b = uuid.uuid4(), uuid.uuid4()
        c_out = uuid.uuid4()
        c_unrelated_a, c_unrelated_b = uuid.uuid4(), uuid.uuid4()
        group = _group(frozenset({c_in_a, c_in_b}))
        # internal net (no exports, both pins inside)
        net_int = _net("INT", {PinRef(c_in_a, 2), PinRef(c_in_b, 2)})

        # internal (both group pins)
        ka1 = _pin_knot(c_in_a, 1)
        ka2 = _pin_knot(c_in_b, 1)
        w_internal = _wire(ka1, ka2, [Point(0, 0), Point(1, 0)])

        # cross_group_disconnect (one group pin + one external pin)
        kc1 = _pin_knot(c_in_a, 2)
        kc2 = _pin_knot(c_out, 1)
        w_disconnect = _wire(kc1, kc2, [Point(0, 1), Point(3, 1)])

        # half_orphan_internal (group pin + joint, internal net).  Use
        # pin 2 on c_in_b — distinct from any PinKnot above.
        kh_pin = _pin_knot(c_in_b, 2)
        kh_joint = _joint_knot(Point(2, 3))
        w_half_int = _wire(kh_pin, kh_joint, [Point(2, 2), Point(2, 3)])

        # half_orphan_external (group pin + joint, no schematic net).
        kx_pin = _pin_knot(c_in_a, 5)
        kx_joint = _joint_knot(Point(7, 8))
        w_half_ext = _wire(kx_pin, kx_joint, [Point(6, 7), Point(7, 8)])

        # external (neither endpoint in drag set)
        ke1 = _pin_knot(c_unrelated_a, 1)
        ke2 = _pin_knot(c_unrelated_b, 1)
        w_external = _wire(ke1, ke2, [Point(9, 9), Point(10, 10)])

        knots = {
            ka1.id: ka1,
            ka2.id: ka2,
            kc1.id: kc1,
            kc2.id: kc2,
            kh_pin.id: kh_pin,
            kh_joint.id: kh_joint,
            kx_pin.id: kx_pin,
            kx_joint.id: kx_joint,
            ke1.id: ke1,
            ke2.id: ke2,
        }

        cls = classify_wires_for_drag(
            wires=[
                w_internal,
                w_disconnect,
                w_half_int,
                w_half_ext,
                w_external,
            ],
            drag_set_members=frozenset({c_in_a, c_in_b}),
            knots=knots,  # type: ignore[arg-type]
            wire_net_map={w_half_int.id: net_int.id},
            schematic=Schematic(nets={net_int.id: net_int}),
            member_groups=[group],
        )
        assert cls.internal == frozenset({w_internal.id})
        assert cls.cross_group_disconnect == frozenset({w_disconnect.id})
        assert cls.half_orphan_internal == frozenset({w_half_int.id})
        assert cls.half_orphan_external == frozenset({w_half_ext.id})
        assert cls.external == frozenset({w_external.id})

        # Sanity: each wire appears in exactly one bucket.
        all_buckets = (
            cls.internal
            | cls.cross_group_disconnect
            | cls.half_orphan_internal
            | cls.half_orphan_external
            | cls.external
        )
        assert all_buckets == frozenset(
            {
                w.id
                for w in [
                    w_internal,
                    w_disconnect,
                    w_half_int,
                    w_half_ext,
                    w_external,
                ]
            }
        )

    def test_travels_with_drag_combines_internal_and_half_orphan_internal(
        self,
    ) -> None:
        cls = DragWireClassification(
            internal=frozenset({uuid.UUID(int=1)}),
            cross_group_disconnect=frozenset({uuid.UUID(int=2)}),
            half_orphan_internal=frozenset({uuid.UUID(int=3)}),
            half_orphan_external=frozenset({uuid.UUID(int=4)}),
            external=frozenset({uuid.UUID(int=5)}),
        )
        assert cls.travels_with_drag == frozenset(
            {uuid.UUID(int=1), uuid.UUID(int=3)},
        )
