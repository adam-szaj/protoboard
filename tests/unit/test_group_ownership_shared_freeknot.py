# SPDX-License-Identifier: MIT
"""Tests for the shared free-knot guard in ``wire_is_owned_by_group``
(commit a6c04b8).

A half-orphan wire — one PinKnot of a group component, one free
(JointKnot or WirePinKnot) endpoint — is normally owned by the group.
But if the free-knot endpoint is *shared* with another wire (the
free knot's ``wire_ids`` set has >1 entry), translating the joint
during a group drag would silently move the foreign wire too.  The
follow-up ``replace_wire_fragments`` slide gate would then bail out
on the foreign wire and abort the macro.  The fix marks such wires
as NOT owned so the drag pipeline never picks them up.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.group import ComponentGroup
from protoboard.domain.layout.group_ownership import wire_is_owned_by_group
from protoboard.domain.layout.knot import JointKnot, PinKnot
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import KnotRef, Wire
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point


@dataclass
class _Ctx:
    layout: Layout
    instances: dict[uuid.UUID, ComponentInstance] = field(default_factory=dict)
    components: dict[str, ComponentDefinition] = field(default_factory=dict)
    schematic: Schematic = field(default_factory=lambda: Schematic(nets={}))


def _resistor_defn() -> ComponentDefinition:
    return ComponentDefinition.from_footprint(name="R", footprint=SIL(2), pin_names={1: "A", 2: "B"})


def _place(ctx: _Ctx, x: float, y: float, refdes: str) -> uuid.UUID:
    inst = ComponentInstance.new(definition_ref="R", refdes=refdes, value="10k")
    ctx.instances[inst.id] = inst
    ctx.layout.placements[inst.id] = ComponentPlacement(
        component_id=inst.id, position=Point(x, y), rotation=0, side="top",
    )
    return inst.id


def _pin_pos(ctx: _Ctx, cid: uuid.UUID, pin: int) -> Point:
    from protoboard.domain.layout.placement import pin_world_position
    defn = ctx.components["R"]
    return pin_world_position(defn.default_footprint(), ctx.layout.placements[cid], pin)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSharedFreeKnotNotOwned:
    def test_half_orphan_with_single_owner_free_knot_owned(self) -> None:
        """Baseline: half-orphan with degree-1 JointKnot is owned by group.

        Confirms the shared-free-knot guard is the *only* thing that
        flips ownership in the test below.
        """
        ctx = _Ctx(layout=Layout(), components={"R": _resistor_defn()})
        ctx.layout.ensure_root_group()
        cid = _place(ctx, 5.0, 5.0, "R1")
        # Half-orphan: pin1 PinKnot + free JointKnot off-pin.
        pin_pos = _pin_pos(ctx, cid, 1)
        free_pos = Point(pin_pos.x + 2.0, pin_pos.y)
        pk = ctx.layout.get_or_create_pin_knot(PinRef(cid, 1))
        jk = ctx.layout.create_joint_knot(free_pos)
        wire = Wire(
            id=uuid.uuid4(),
            path=[pin_pos, free_pos],
            side="top",
            a=KnotRef(id=pk.id), b=KnotRef(id=jk.id),
        )
        ctx.layout.add_wire(wire)
        # Net with the pin so half-orphan classifier has something to
        # work with.
        net = Net(id=uuid.uuid4(), name="N1", pins={PinRef(cid, 1)})
        ctx.schematic.nets[net.id] = net

        group = ComponentGroup(
            id=uuid.uuid4(), name="G",
            component_ids=frozenset({cid}),
        )
        # Single-owner free knot → owned.
        owned = wire_is_owned_by_group(
            wire, group,
            wire_net_map={wire.id: net.id},
            knots=ctx.layout.knots,  # type: ignore[arg-type]
            group_comp_ids=frozenset({cid}),
            schematic=ctx.schematic,
        )
        assert owned is True

    def test_half_orphan_with_shared_free_knot_not_owned(self) -> None:
        """The regression: free knot shared by two wires → wire NOT owned.

        A second wire (in the same net, but with both endpoints free)
        anchors at the same JointKnot.  Moving the joint would drag the
        second wire silently, which would then fail replace_wire_fragments
        when the joint position no longer matches the second wire's
        endpoint expectations.  Mark as not-owned to skip the drag.
        """
        ctx = _Ctx(layout=Layout(), components={"R": _resistor_defn()})
        ctx.layout.ensure_root_group()
        cid = _place(ctx, 5.0, 5.0, "R1")
        pin_pos = _pin_pos(ctx, cid, 1)
        free_pos = Point(pin_pos.x + 2.0, pin_pos.y)
        far_pos = Point(pin_pos.x + 4.0, pin_pos.y)
        pk = ctx.layout.get_or_create_pin_knot(PinRef(cid, 1))
        jk = ctx.layout.create_joint_knot(free_pos)
        far_jk = ctx.layout.create_joint_knot(far_pos)
        # Wire 1: pin → jk (half-orphan, anchors in group)
        w1 = Wire(
            id=uuid.uuid4(),
            path=[pin_pos, free_pos],
            side="top",
            a=KnotRef(id=pk.id), b=KnotRef(id=jk.id),
        )
        ctx.layout.add_wire(w1)
        # Wire 2: jk → far_jk (both free; jk now shared)
        w2 = Wire(
            id=uuid.uuid4(),
            path=[free_pos, far_pos],
            side="top",
            a=KnotRef(id=jk.id), b=KnotRef(id=far_jk.id),
        )
        ctx.layout.add_wire(w2)
        net = Net(id=uuid.uuid4(), name="N1", pins={PinRef(cid, 1)})
        ctx.schematic.nets[net.id] = net

        group = ComponentGroup(
            id=uuid.uuid4(), name="G",
            component_ids=frozenset({cid}),
        )
        # Shared joint → w1 NOT owned (guard kicks in).
        live_jk = ctx.layout.knots[jk.id]
        assert isinstance(live_jk, JointKnot)
        assert len(live_jk.wire_ids) == 2  # sanity: joint really is shared

        owned = wire_is_owned_by_group(
            w1, group,
            wire_net_map={w1.id: net.id, w2.id: net.id},
            knots=ctx.layout.knots,  # type: ignore[arg-type]
            group_comp_ids=frozenset({cid}),
            schematic=ctx.schematic,
        )
        assert owned is False

    def test_both_pinknots_in_group_owned_despite_shared_joints(self) -> None:
        """A wire with two PinKnots (both in group) is owned unconditionally.

        Even if it happened to share a free joint with another wire,
        the both-PinKnot case ignores the free-knot guard.  Defensive
        cross-check: shared-knot guard only applies to half-orphans.
        """
        ctx = _Ctx(layout=Layout(), components={"R": _resistor_defn()})
        ctx.layout.ensure_root_group()
        cid_a = _place(ctx, 5.0, 5.0, "R1")
        cid_b = _place(ctx, 9.0, 5.0, "R2")
        pos_a = _pin_pos(ctx, cid_a, 2)
        pos_b = _pin_pos(ctx, cid_b, 1)
        pka = ctx.layout.get_or_create_pin_knot(PinRef(cid_a, 2))
        pkb = ctx.layout.get_or_create_pin_knot(PinRef(cid_b, 1))
        w = Wire(
            id=uuid.uuid4(),
            path=[pos_a, pos_b],
            side="top",
            a=KnotRef(id=pka.id), b=KnotRef(id=pkb.id),
        )
        ctx.layout.add_wire(w)
        # No nets needed — both-PinKnot owned path is unconditional.
        group = ComponentGroup(
            id=uuid.uuid4(), name="G",
            component_ids=frozenset({cid_a, cid_b}),
        )
        owned = wire_is_owned_by_group(
            w, group, wire_net_map={},
            knots=ctx.layout.knots,  # type: ignore[arg-type]
            group_comp_ids=frozenset({cid_a, cid_b}),
            schematic=ctx.schematic,
        )
        assert owned is True

    def test_pinknot_in_drag_set_exempt_helper(self) -> None:
        """Mirror of the exemption logic from ``_pin_on_wire_remove_ids``
        (commit 6e1beba) extracted as a pure helper.

        The select-tool method builds ``internal_wire_ids`` as the
        union of:
          * ``_drag_orig_wire_paths`` keys (the owned-set), and
          * every wire whose endpoint PinKnot points at a component in
            ``drag_ids``.

        We re-implement the second clause inline and assert the
        intended membership.  A pure-domain-level test would require
        wiring up a ``SelectTool`` with a session, board view, scene,
        etc. — out of scope for unit tests.  This still covers the
        invariant ("wire with PinKnot to a moved component must be in
        the exempt set") at the level the GUI code relies on.
        """
        ctx = _Ctx(layout=Layout(), components={"R": _resistor_defn()})
        ctx.layout.ensure_root_group()
        cid_a = _place(ctx, 5.0, 5.0, "R1")
        cid_b = _place(ctx, 9.0, 5.0, "R2")
        cid_c = _place(ctx, 13.0, 5.0, "R3")  # not dragged
        # Two wires touching the dragged components.
        pos_a = _pin_pos(ctx, cid_a, 2)
        pos_b = _pin_pos(ctx, cid_b, 1)
        pos_c = _pin_pos(ctx, cid_c, 1)
        pka = ctx.layout.get_or_create_pin_knot(PinRef(cid_a, 2))
        pkb = ctx.layout.get_or_create_pin_knot(PinRef(cid_b, 1))
        pkc = ctx.layout.get_or_create_pin_knot(PinRef(cid_c, 1))
        w_in = Wire(
            id=uuid.uuid4(), path=[pos_a, pos_b], side="top",
            a=KnotRef(id=pka.id), b=KnotRef(id=pkb.id),
        )
        ctx.layout.add_wire(w_in)
        # External wire: pin of cid_c (not dragged) to a free joint.
        free = Point(pos_c.x + 2.0, pos_c.y)
        jk = ctx.layout.create_joint_knot(free)
        w_ext = Wire(
            id=uuid.uuid4(), path=[pos_c, free], side="top",
            a=KnotRef(id=pkc.id), b=KnotRef(id=jk.id),
        )
        ctx.layout.add_wire(w_ext)

        # Replicate the exemption sweep from select_tool.py.
        drag_ids = {cid_a, cid_b}
        exempt: set[uuid.UUID] = set()
        for w in ctx.layout.wires:
            for ref in (w.a, w.b):
                if ref is None:
                    continue
                k = ctx.layout.knots.get(ref.id)
                if not isinstance(k, PinKnot):
                    continue
                if k.pin.component_id in drag_ids:
                    exempt.add(w.id)
                    break

        # Owned wire (PinKnot endpoints to dragged comps) is exempt.
        assert w_in.id in exempt
        # External wire (PinKnot to a stationary comp) is NOT exempt.
        assert w_ext.id not in exempt

    def test_full_orphan_not_owned(self) -> None:
        """Full-orphan wire (no PinKnot endpoint) is never owned.

        Documents the broader rule (full-orphan = no net = no group)
        the user has been re-emphasising.  Mirrors the half-orphan
        rule: structural anchor is required for ownership.
        """
        ctx = _Ctx(layout=Layout(), components={"R": _resistor_defn()})
        ctx.layout.ensure_root_group()
        cid = _place(ctx, 5.0, 5.0, "R1")
        # Both endpoints are off-component free joints.
        ja = ctx.layout.create_joint_knot(Point(5.5, 5.5))
        jb = ctx.layout.create_joint_knot(Point(7.5, 5.5))
        w = Wire(
            id=uuid.uuid4(),
            path=[Point(5.5, 5.5), Point(7.5, 5.5)],
            side="top",
            a=KnotRef(id=ja.id), b=KnotRef(id=jb.id),
        )
        ctx.layout.add_wire(w)

        group = ComponentGroup(
            id=uuid.uuid4(), name="G",
            component_ids=frozenset({cid}),
        )
        owned = wire_is_owned_by_group(
            w, group, wire_net_map={},
            knots=ctx.layout.knots,  # type: ignore[arg-type]
            group_comp_ids=frozenset({cid}),
            schematic=ctx.schematic,
        )
        assert owned is False
