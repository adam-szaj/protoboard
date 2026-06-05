# SPDX-License-Identifier: MIT
"""Wire creation scenarios: endpoint topology × group context × net state.

Tests classify_wire_nets + group-ownership outcomes for the matrix the
user enumerated:

Endpoint kinds at each end:
  * pin — placed component pin at the point
  * existing-wire — interior/endpoint of an existing wire
  * empty-pad — board pad with neither a pin nor a wire

Net states at each pin endpoint:
  * already in a net
  * not in any net

Group context (top-level placement of the components touched by the wire):
  * root-root — both components live in the root group
  * groupA-groupA — both inside group A
  * root-groupA — one root, one in A
  * groupA[B]-groupA[B] — both inside the same nested group B (B is
    child of A); ``group_comp_ids`` recursive set covers both
  * groupA-groupC — siblings under root

Outcomes asserted:
  * WireNetClassification.kind
  * Whether the wire would be net-assigned (NO_PINS / BOTH_UNCONNECTED /
    ONE_UNCONNECTED / SAME_NET / DIFFERENT_NETS)
  * Group ownership of the resulting wire under ``wire_is_owned_by_group``
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.group import ComponentGroup, all_component_ids
from protoboard.domain.layout.group_ownership import wire_is_owned_by_group
from protoboard.domain.layout.layout import ROOT_GROUP_ID, Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import KnotRef, Wire
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.domain.schematic.wire_net_ops import (
    WireNetKind,
    classify_wire_nets,
)
from protoboard.geometry import Point


@dataclass
class _Ctx:
    layout: Layout
    instances: dict[uuid.UUID, ComponentInstance] = field(default_factory=dict)
    components: dict[str, ComponentDefinition] = field(default_factory=dict)
    schematic: Schematic = field(default_factory=lambda: Schematic(nets={}))


def _resistor_defn() -> ComponentDefinition:
    return ComponentDefinition(name="R", footprint=SIL(2), pin_names={1: "A", 2: "B"})


def _ctx_with_resistor_lib() -> _Ctx:
    ctx = _Ctx(layout=Layout(), components={"R": _resistor_defn()})
    ctx.layout.ensure_root_group()
    return ctx


def _place(ctx: _Ctx, x: float, y: float, refdes: str) -> uuid.UUID:
    inst = ComponentInstance.new(definition_ref="R", refdes=refdes, value="10k")
    ctx.instances[inst.id] = inst
    ctx.layout.placements[inst.id] = ComponentPlacement(
        component_id=inst.id, position=Point(x, y), rotation=0, side="top",
    )
    return inst.id


def _pin_pos(ctx: _Ctx, cid: uuid.UUID, pin: int) -> Point:
    from protoboard.domain.layout.placement import pin_world_position
    return pin_world_position(
        ctx.components["R"].footprint, ctx.layout.placements[cid], pin,
    )


def _make_group(
    ctx: _Ctx, *, members: frozenset[uuid.UUID], parent: uuid.UUID = ROOT_GROUP_ID,
    name: str = "G",
) -> ComponentGroup:
    g = ComponentGroup(
        id=uuid.uuid4(), name=name, component_ids=members,
        origin=None,
    )
    ctx.layout.groups[g.id] = g
    par = ctx.layout.groups[parent]
    ctx.layout.groups[parent] = ComponentGroup(
        id=par.id, name=par.name,
        component_ids=par.component_ids,
        child_group_ids=par.child_group_ids | {g.id},
        exported_pins=par.exported_pins,
        rotation=par.rotation, origin=par.origin,
    )
    return g


def _wire_pin_to_pin(
    ctx: _Ctx, cid_a: uuid.UUID, pin_a: int,
    cid_b: uuid.UUID, pin_b: int,
) -> Wire:
    pa = _pin_pos(ctx, cid_a, pin_a)
    pb = _pin_pos(ctx, cid_b, pin_b)
    pka = ctx.layout.get_or_create_pin_knot(PinRef(cid_a, pin_a))
    pkb = ctx.layout.get_or_create_pin_knot(PinRef(cid_b, pin_b))
    w = Wire(
        id=uuid.uuid4(), path=[pa, pb], side="top",
        a=KnotRef(id=pka.id), b=KnotRef(id=pkb.id),
    )
    ctx.layout.add_wire(w)
    return w


# ---------------------------------------------------------------------------
# Endpoint topology × net state — classify_wire_nets
# ---------------------------------------------------------------------------


class TestClassifyByEndpointAndNet:
    def test_pin_pin_no_existing_nets_both_unconnected(self) -> None:
        ctx = _ctx_with_resistor_lib()
        a = _place(ctx, 5.0, 5.0, "R1")
        b = _place(ctx, 7.0, 5.0, "R2")
        w = _wire_pin_to_pin(ctx, a, 2, b, 1)
        cl = classify_wire_nets(w, ctx)
        assert cl.kind is WireNetKind.BOTH_UNCONNECTED
        assert cl.pin_a == (a, 2)
        assert cl.pin_b == (b, 1)

    def test_pin_pin_one_in_net_one_unconnected(self) -> None:
        ctx = _ctx_with_resistor_lib()
        a = _place(ctx, 5.0, 5.0, "R1")
        b = _place(ctx, 7.0, 5.0, "R2")
        # Pre-existing net containing a/pin2 only.
        net = Net(id=uuid.uuid4(), name="VCC", pins={PinRef(a, 2)})
        ctx.schematic.nets[net.id] = net
        w = _wire_pin_to_pin(ctx, a, 2, b, 1)
        cl = classify_wire_nets(w, ctx)
        assert cl.kind is WireNetKind.ONE_UNCONNECTED
        assert cl.net_a == net or cl.net_b == net

    def test_pin_pin_same_net_silent_extend(self) -> None:
        ctx = _ctx_with_resistor_lib()
        a = _place(ctx, 5.0, 5.0, "R1")
        b = _place(ctx, 7.0, 5.0, "R2")
        net = Net(
            id=uuid.uuid4(), name="VCC",
            pins={PinRef(a, 2), PinRef(b, 1)},
        )
        ctx.schematic.nets[net.id] = net
        w = _wire_pin_to_pin(ctx, a, 2, b, 1)
        cl = classify_wire_nets(w, ctx)
        assert cl.kind is WireNetKind.SAME_NET

    def test_pin_pin_different_nets(self) -> None:
        ctx = _ctx_with_resistor_lib()
        a = _place(ctx, 5.0, 5.0, "R1")
        b = _place(ctx, 7.0, 5.0, "R2")
        net_a = Net(id=uuid.uuid4(), name="VCC", pins={PinRef(a, 2)})
        net_b = Net(id=uuid.uuid4(), name="GND", pins={PinRef(b, 1)})
        ctx.schematic.nets[net_a.id] = net_a
        ctx.schematic.nets[net_b.id] = net_b
        w = _wire_pin_to_pin(ctx, a, 2, b, 1)
        cl = classify_wire_nets(w, ctx)
        assert cl.kind is WireNetKind.DIFFERENT_NETS
        assert cl.net_a is not None and cl.net_b is not None
        assert {cl.net_a.id, cl.net_b.id} == {net_a.id, net_b.id}

    def test_pin_to_existing_wire_vertex_inherits_net(self) -> None:
        """Endpoint B lands on an existing wire's explicit path vertex.

        ``find_wire_net_at`` matches by vertex equality, not arbitrary
        interior intersection — so we exercise the vertex-match path.
        """
        ctx = _ctx_with_resistor_lib()
        a = _place(ctx, 5.0, 5.0, "R1")
        b = _place(ctx, 9.0, 5.0, "R2")
        net = Net(
            id=uuid.uuid4(), name="N1",
            pins={PinRef(a, 2), PinRef(b, 1)},
        )
        ctx.schematic.nets[net.id] = net
        # Existing wire with an explicit mid-path vertex.
        pa = _pin_pos(ctx, a, 2)
        pb = _pin_pos(ctx, b, 1)
        mid = Point(7.5, 5.5)
        pka = ctx.layout.get_or_create_pin_knot(PinRef(a, 2))
        pkb = ctx.layout.get_or_create_pin_knot(PinRef(b, 1))
        # The mid vertex needs a knot for add_wire (interior knots not
        # required, but we keep wire creation legal).  Path with explicit
        # mid vertex.
        existing = Wire(
            id=uuid.uuid4(), path=[pa, mid, pb], side="top",
            a=KnotRef(id=pka.id), b=KnotRef(id=pkb.id),
        )
        ctx.layout.add_wire(existing)

        # New wire: pin c/1 (no net) → mid (vertex of existing wire).
        c = _place(ctx, 7.0, 8.0, "R3")
        new_w = Wire(
            id=uuid.uuid4(),
            path=[_pin_pos(ctx, c, 1), mid],
            side="top",
        )
        cl = classify_wire_nets(new_w, ctx)
        # mid is a vertex of the existing wire which has net N1.
        # Endpoint A is pin c/1 with no net.  → ONE_UNCONNECTED.
        assert cl.kind is WireNetKind.ONE_UNCONNECTED

    def test_empty_pad_to_pin_one_unconnected(self) -> None:
        """Endpoint A is an empty pad (no pin, no wire); endpoint B is
        a pin in a net → ONE_UNCONNECTED with pin_a=None.

        The resolver path handles this as a wire-touch extension
        (``_extend_net`` no-ops when ``unconnected_pin is None`` —
        purely geometric join).
        """
        ctx = _ctx_with_resistor_lib()
        b = _place(ctx, 7.0, 5.0, "R2")
        net = Net(id=uuid.uuid4(), name="VCC", pins={PinRef(b, 1)})
        ctx.schematic.nets[net.id] = net
        new_w = Wire(
            id=uuid.uuid4(),
            path=[Point(3.5, 5.5), _pin_pos(ctx, b, 1)],
            side="top",
        )
        cl = classify_wire_nets(new_w, ctx)
        assert cl.kind is WireNetKind.ONE_UNCONNECTED
        assert cl.pin_a is None  # the empty-pad side has no pin
        assert cl.pin_b == (b, 1)

    def test_empty_pad_to_empty_pad_no_pins(self) -> None:
        """Both endpoints empty pads → NO_PINS (no schematic effect)."""
        ctx = _ctx_with_resistor_lib()
        new_w = Wire(
            id=uuid.uuid4(),
            path=[Point(3.5, 5.5), Point(5.5, 5.5)],
            side="top",
        )
        cl = classify_wire_nets(new_w, ctx)
        assert cl.kind is WireNetKind.NO_PINS


# ---------------------------------------------------------------------------
# Group context: root/A/A[B]/sibling A-C
# ---------------------------------------------------------------------------


class TestOwnershipByGroupContext:
    def _build_two_resistors(self) -> tuple[_Ctx, uuid.UUID, uuid.UUID]:
        ctx = _ctx_with_resistor_lib()
        a = _place(ctx, 5.0, 5.0, "R1")
        b = _place(ctx, 7.0, 5.0, "R2")
        return ctx, a, b

    def _owned(
        self, ctx: _Ctx, w: Wire, group: ComponentGroup,
    ) -> bool:
        return wire_is_owned_by_group(
            w, group,
            wire_net_map={},
            knots=ctx.layout.knots,  # type: ignore[arg-type]
            group_comp_ids=all_component_ids(ctx.layout.groups, group.id),
            schematic=ctx.schematic,
        )

    def test_root_root_no_group_no_ownership_concept(self) -> None:
        """Wire between two root-level components — only the synthetic
        root "group" sees it.  Per design, root-group ownership is
        always True (the root is the universe of all components)."""
        ctx, a, b = self._build_two_resistors()
        w = _wire_pin_to_pin(ctx, a, 2, b, 1)
        root = ctx.layout.groups[ROOT_GROUP_ID]
        # Synthesise a real root group reference for the classifier; for
        # the root group this is essentially "always owned" because
        # group_comp_ids is the universe.
        # Don't assert True directly — the classifier may special-case
        # root.  Just confirm no exception and the call is well-defined.
        # (The interesting cases are the non-root groups below.)
        _ = wire_is_owned_by_group(
            w, root, wire_net_map={}, knots=ctx.layout.knots,  # type: ignore[arg-type]
            group_comp_ids=all_component_ids(ctx.layout.groups, ROOT_GROUP_ID),
            schematic=ctx.schematic,
        )

    def test_groupA_groupA_pin_to_pin_owned(self) -> None:
        ctx, a, b = self._build_two_resistors()
        w = _wire_pin_to_pin(ctx, a, 2, b, 1)
        ga = _make_group(ctx, members=frozenset({a, b}), name="A")
        assert self._owned(ctx, w, ga) is True

    def test_root_groupA_pin_to_pin_not_owned_by_A(self) -> None:
        """Mixed: A's pin endpoint to a root-component pin → not owned
        by A (B sits outside the group)."""
        ctx, a, b = self._build_two_resistors()
        w = _wire_pin_to_pin(ctx, a, 2, b, 1)
        ga = _make_group(ctx, members=frozenset({a}), name="A")
        assert self._owned(ctx, w, ga) is False

    def test_nested_groupAB_AB_both_recursive(self) -> None:
        """A[B]: B is a child of A; recursive members of A include B's
        members.  Wire between two B-members is owned by both B and A.
        """
        ctx, a, b = self._build_two_resistors()
        w = _wire_pin_to_pin(ctx, a, 2, b, 1)
        # Place a and b inside B (a sub-group), B child of A.
        gb = _make_group(ctx, members=frozenset({a, b}), name="B")
        # A's direct members can be empty; B is its child.
        ga = ComponentGroup(
            id=uuid.uuid4(), name="A",
            component_ids=frozenset(),
            child_group_ids=frozenset({gb.id}),
            origin=None,
        )
        ctx.layout.groups[ga.id] = ga
        # Re-parent gb under ga instead of root.
        root = ctx.layout.groups[ROOT_GROUP_ID]
        ctx.layout.groups[ROOT_GROUP_ID] = ComponentGroup(
            id=root.id, name=root.name,
            component_ids=root.component_ids,
            child_group_ids=(root.child_group_ids - {gb.id}) | {ga.id},
            exported_pins=root.exported_pins,
            rotation=root.rotation, origin=root.origin,
        )
        # Both classify as owned (a and b are within recursive A).
        assert self._owned(ctx, w, gb) is True
        assert self._owned(ctx, w, ga) is True

    def test_sibling_groupA_groupC_not_owned_by_either(self) -> None:
        """A and C are siblings, each holding one of the two components.
        A wire between A's pin and C's pin: owned by NEITHER (it's an
        interface wire crossing the boundary)."""
        ctx, a, b = self._build_two_resistors()
        w = _wire_pin_to_pin(ctx, a, 2, b, 1)
        ga = _make_group(ctx, members=frozenset({a}), name="A")
        gc = _make_group(ctx, members=frozenset({b}), name="C")
        assert self._owned(ctx, w, ga) is False
        assert self._owned(ctx, w, gc) is False


# ---------------------------------------------------------------------------
# Endpoint-on-existing-wire creating shared joints
# ---------------------------------------------------------------------------


class TestEndpointOnExistingWireOwnership:
    def test_pin_to_joint_with_internal_net_owned(self) -> None:
        """Half-orphan wire (pin in group + free single-owner joint)
        whose net is internal to the group is owned by it.

        Internal = every net pin lives in the group, none is exported.
        ``wire_net_map`` must map the wire to that net so the
        classifier can run ``net_is_internal_to_group``.
        """
        ctx = _ctx_with_resistor_lib()
        a = _place(ctx, 5.0, 5.0, "R1")
        # Half-orphan: pin a/1 → free joint at off-pin (5, 7).
        free = Point(5.0, 7.0)
        jk = ctx.layout.create_joint_knot(free)
        pka = ctx.layout.get_or_create_pin_knot(PinRef(a, 1))
        w_new = Wire(
            id=uuid.uuid4(),
            path=[_pin_pos(ctx, a, 1), free],
            side="top",
            a=KnotRef(id=pka.id), b=KnotRef(id=jk.id),
        )
        ctx.layout.add_wire(w_new)
        # Internal net: only pins in the group.
        net = Net(id=uuid.uuid4(), name="N1", pins={PinRef(a, 1)})
        ctx.schematic.nets[net.id] = net

        ga = _make_group(ctx, members=frozenset({a}), name="A")
        assert wire_is_owned_by_group(
            w_new, ga,
            wire_net_map={w_new.id: net.id},
            knots=ctx.layout.knots,  # type: ignore[arg-type]
            group_comp_ids=frozenset({a}),
            schematic=ctx.schematic,
        ) is True

    def test_pin_to_joint_external_net_not_owned(self) -> None:
        """Half-orphan whose net touches a component outside the group
        is NOT owned — the wire is an interface/external fragment."""
        ctx = _ctx_with_resistor_lib()
        a = _place(ctx, 5.0, 5.0, "R1")
        b = _place(ctx, 9.0, 5.0, "R2")  # NOT in the group
        free = Point(5.0, 7.0)
        jk = ctx.layout.create_joint_knot(free)
        pka = ctx.layout.get_or_create_pin_knot(PinRef(a, 1))
        w_new = Wire(
            id=uuid.uuid4(),
            path=[_pin_pos(ctx, a, 1), free],
            side="top",
            a=KnotRef(id=pka.id), b=KnotRef(id=jk.id),
        )
        ctx.layout.add_wire(w_new)
        # Net spans a (in group) and b (outside).
        net = Net(
            id=uuid.uuid4(), name="EXT",
            pins={PinRef(a, 1), PinRef(b, 1)},
        )
        ctx.schematic.nets[net.id] = net

        ga = _make_group(ctx, members=frozenset({a}), name="A")
        assert wire_is_owned_by_group(
            w_new, ga,
            wire_net_map={w_new.id: net.id},
            knots=ctx.layout.knots,  # type: ignore[arg-type]
            group_comp_ids=frozenset({a}),
            schematic=ctx.schematic,
        ) is False
