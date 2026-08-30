# SPDX-License-Identifier: MIT
"""Group drag / rotate × wire-ownership behavior tests.

For each topology of where a wire's two endpoints sit relative to a
group's component set, this file asserts:

  * Drag (MoveComponent on every member) — does the wire move with the
    group, stay put, or remain stretched / dropped?
  * Rotate (RotateGroupCommand) — does ``RotateGroupCommand`` mutate
    the wire's path (internal) or leave it alone (external)?

Topologies covered:

  * **internal pin-pin**: both PinKnot endpoints on group components →
    owned, moves with drag, rotates.
  * **external interface**: one PinKnot in group, other PinKnot in a
    component outside the group → not owned, the group-side endpoint
    moves with the component while the outside endpoint stays
    (rendered as a stretched wire; the actual drag pipeline can then
    auto-remove it, but the domain commands themselves don't).
  * **half-orphan with shared free joint**: free knot has multiple
    owners → not owned, shared joint must not move when the
    group-anchored pin moves.
  * **full-orphan**: both free joints → not owned, not moved by
    RotateGroupCommand (whose internal-set is empty for it).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.commands.component_cmds import MoveComponentCommand
from protoboard.domain.commands.group_cmds import RotateGroupCommand
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.group import ComponentGroup
from protoboard.domain.layout.group_ownership import wire_is_owned_by_group
from protoboard.domain.layout.knot import JointKnot, PinKnot
from protoboard.domain.layout.layout import ROOT_GROUP_ID, Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import KnotRef, Wire
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from tests.unit._autoroute_invariants import make_sil, sil_families


def _board() -> Board:
    return Board(
        root=BoardGrid(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            cell=PadAtom(
                width_policy=SizePolicy.HUG,
                height_policy=SizePolicy.HUG,
                layers=PadLayers(top=True, bottom=True, plated_through=True),
            ),
            rows=40,
            cols=40,
            row_gap=0.0,
            col_gap=0.0,
        )
    )


@dataclass
class _Ctx:
    layout: Layout
    instances: dict[uuid.UUID, ComponentInstance] = field(default_factory=dict)
    components: dict[str, ComponentDefinition] = field(default_factory=dict)
    schematic: Schematic = field(default_factory=lambda: Schematic(nets={}))
    board: Board = field(default_factory=_board)
    package_families: dict = field(default_factory=dict)


def _resistor_defn() -> ComponentDefinition:
    return make_sil("R", 2)


def _ctx() -> _Ctx:
    defn = _resistor_defn()
    c = _Ctx(
        layout=Layout(),
        components={"R": defn},
        package_families=sil_families(defn),
    )
    c.layout.ensure_root_group()
    return c


def _place(ctx: _Ctx, x: float, y: float, refdes: str) -> uuid.UUID:
    inst = ComponentInstance.new(
        definition_ref="R",
        refdes=refdes,
        value="10k",
    ).bind(ctx.components["R"], ctx.package_families)
    ctx.instances[inst.id] = inst
    ctx.layout.placements[inst.id] = ComponentPlacement(
        component_id=inst.id,
        position=Point(x, y),
        rotation=0,
        side="top",
    )
    return inst.id


def _pin_pos(ctx: _Ctx, cid: uuid.UUID, pin: int) -> Point:
    from protoboard.domain.layout.placement import pin_world_position

    return pin_world_position(
        ctx.components["R"].default_footprint(ctx.package_families),
        ctx.layout.placements[cid],
        pin,
    )


def _attach_to_root(ctx: _Ctx, group: ComponentGroup) -> None:
    ctx.layout.groups[group.id] = group
    root = ctx.layout.groups[ROOT_GROUP_ID]
    ctx.layout.groups[ROOT_GROUP_ID] = ComponentGroup(
        id=root.id,
        name=root.name,
        component_ids=root.component_ids,
        child_group_ids=root.child_group_ids | {group.id},
        exported_pins=root.exported_pins,
    )


def _drag_components(
    ctx: _Ctx,
    cids: list[uuid.UUID],
    dx: float,
    dy: float,
    *,
    drag_wire_paths: bool = False,
) -> None:
    """Apply MoveComponentCommand to each cid in turn — simulates a
    group drag without going through the full SelectTool pipeline.

    When ``drag_wire_paths=True``, also translate wires whose every
    PinKnot endpoint references a dragged component (mirrors what the
    real ``TranslateAnchoredWires`` pass would do).
    """
    for cid in cids:
        old_pl = ctx.layout.placements[cid]
        new_pl = ComponentPlacement(
            component_id=cid,
            position=Point(old_pl.position.x + dx, old_pl.position.y + dy),
            rotation=old_pl.rotation,
            side=old_pl.side,
        )
        MoveComponentCommand(
            component_id=cid,
            new_placement=new_pl,
            old_placement=old_pl,
            skip_conflict_check=True,
            placement_only=True,
        ).apply(ctx)
    if drag_wire_paths:
        drag_set = set(cids)
        for w in ctx.layout.wires:
            if w.a is None or w.b is None:
                continue
            ka = ctx.layout.knots.get(w.a.id)
            kb = ctx.layout.knots.get(w.b.id)
            if not isinstance(ka, PinKnot) or not isinstance(kb, PinKnot):
                continue
            if ka.pin.component_id in drag_set and kb.pin.component_id in drag_set:
                w.path = [Point(p.x + dx, p.y + dy) for p in w.path]
                w.fragments = [
                    type(f)(
                        path=[Point(p.x + dx, p.y + dy) for p in f.path],
                        side=f.side,
                    )
                    for f in w.fragments
                ]


# ---------------------------------------------------------------------------
# Rotate × ownership
# ---------------------------------------------------------------------------


class TestRotateMovesOnlyInternalWires:
    def test_internal_pin_pin_rotates(self) -> None:
        ctx = _ctx()
        a = _place(ctx, 5.0, 5.0, "R1")
        b = _place(ctx, 5.0, 9.0, "R2")
        pka = ctx.layout.get_or_create_pin_knot(PinRef(a, 2))
        pkb = ctx.layout.get_or_create_pin_knot(PinRef(b, 1))
        w = Wire(
            id=uuid.uuid4(),
            path=[_pin_pos(ctx, a, 2), _pin_pos(ctx, b, 1)],
            side="top",
            a=KnotRef(id=pka.id),
            b=KnotRef(id=pkb.id),
        )
        ctx.layout.add_wire(w)
        g = ComponentGroup(
            id=uuid.uuid4(),
            name="G",
            component_ids=frozenset({a, b}),
        )
        _attach_to_root(ctx, g)

        pre = list(w.path)
        RotateGroupCommand(group_id=g.id, delta=90).apply(ctx)
        assert w.path != pre

    def test_external_interface_wire_not_rotated(self) -> None:
        """One PinKnot inside group, other outside → not owned, not
        rotated by RotateGroupCommand even though pin endpoint A moves
        with placement A's rotation.
        """
        ctx = _ctx()
        a = _place(ctx, 5.0, 5.0, "R1")
        b = _place(ctx, 9.0, 5.0, "R2")  # outside group
        pka = ctx.layout.get_or_create_pin_knot(PinRef(a, 2))
        pkb = ctx.layout.get_or_create_pin_knot(PinRef(b, 1))
        w = Wire(
            id=uuid.uuid4(),
            path=[_pin_pos(ctx, a, 2), _pin_pos(ctx, b, 1)],
            side="top",
            a=KnotRef(id=pka.id),
            b=KnotRef(id=pkb.id),
        )
        ctx.layout.add_wire(w)
        g = ComponentGroup(
            id=uuid.uuid4(),
            name="G",
            component_ids=frozenset({a}),  # only a is in
        )
        _attach_to_root(ctx, g)

        pre = list(w.path)
        RotateGroupCommand(group_id=g.id, delta=90).apply(ctx)
        # Path NOT rotated — wire is external, classifier leaves it alone.
        # (Placement A *did* rotate, but the wire's stored path stays at
        # pre-rotation values until a separate translate_anchored_wires
        # pass fixes the now-stretched endpoint.  Here we just confirm
        # ``RotateGroupCommand`` itself didn't mutate the path.)
        assert w.path == pre

    def test_full_orphan_wire_not_rotated(self) -> None:
        ctx = _ctx()
        a = _place(ctx, 5.0, 5.0, "R1")
        ja = ctx.layout.create_joint_knot(Point(5.5, 5.5))
        jb = ctx.layout.create_joint_knot(Point(7.5, 5.5))
        w = Wire(
            id=uuid.uuid4(),
            path=[Point(5.5, 5.5), Point(7.5, 5.5)],
            side="top",
            a=KnotRef(id=ja.id),
            b=KnotRef(id=jb.id),
        )
        ctx.layout.add_wire(w)
        g = ComponentGroup(
            id=uuid.uuid4(),
            name="G",
            component_ids=frozenset({a}),
        )
        _attach_to_root(ctx, g)
        pre = list(w.path)
        RotateGroupCommand(group_id=g.id, delta=90).apply(ctx)
        assert w.path == pre


# ---------------------------------------------------------------------------
# Drag: every wire endpoint that is a PinKnot of a moved component
#       physically tracks the placement (PinKnot.position is resolved
#       lazily from placement).  Wires not owned must not have their
#       *stored* path mutated by the move commands.
# ---------------------------------------------------------------------------


class TestDragInternalAndExternalWires:
    def test_internal_wire_pinknot_positions_track_placement(self) -> None:
        """PinKnot positions are derived from the live placement —
        confirm that after dragging both members, both pin endpoints'
        resolved positions equal the new placement.
        """
        from protoboard.domain.layout.placement import pin_world_position

        ctx = _ctx()
        a = _place(ctx, 5.0, 5.0, "R1")
        b = _place(ctx, 5.0, 9.0, "R2")
        pka = ctx.layout.get_or_create_pin_knot(PinRef(a, 2))
        pkb = ctx.layout.get_or_create_pin_knot(PinRef(b, 1))
        w = Wire(
            id=uuid.uuid4(),
            path=[_pin_pos(ctx, a, 2), _pin_pos(ctx, b, 1)],
            side="top",
            a=KnotRef(id=pka.id),
            b=KnotRef(id=pkb.id),
        )
        ctx.layout.add_wire(w)
        g = ComponentGroup(
            id=uuid.uuid4(),
            name="G",
            component_ids=frozenset({a, b}),
        )
        _attach_to_root(ctx, g)

        _drag_components(ctx, [a, b], 10.0, 7.0)
        defn = ctx.components["R"]
        # Resolved pin positions after drag.
        new_a = pin_world_position(
            defn.default_footprint(ctx.package_families), ctx.layout.placements[a], 2
        )
        new_b = pin_world_position(
            defn.default_footprint(ctx.package_families), ctx.layout.placements[b], 1
        )
        # Pin a/2 = placement + (0, 1) → (5, 6) → after drag → (15, 13).
        # Pin b/1 = placement + (0, 0) → (5, 9) → after drag → (15, 16).
        assert (new_a.x, new_a.y) == (15.0, 13.0)
        assert (new_b.x, new_b.y) == (15.0, 16.0)

    def test_external_wire_pinknot_only_moved_endpoint_tracks(self) -> None:
        """Pin A in group, pin B outside.  After dragging only a (group
        member), pin A's resolved world pos moves; pin B's stays.
        Wire's stored path is unchanged by MoveComponent (TranslateAnchored
        is a separate pipeline pass not exercised here)."""
        from protoboard.domain.layout.placement import pin_world_position

        ctx = _ctx()
        a = _place(ctx, 5.0, 5.0, "R1")
        b = _place(ctx, 9.0, 5.0, "R2")  # outside group
        pka = ctx.layout.get_or_create_pin_knot(PinRef(a, 2))
        pkb = ctx.layout.get_or_create_pin_knot(PinRef(b, 1))
        w = Wire(
            id=uuid.uuid4(),
            path=[_pin_pos(ctx, a, 2), _pin_pos(ctx, b, 1)],
            side="top",
            a=KnotRef(id=pka.id),
            b=KnotRef(id=pkb.id),
        )
        ctx.layout.add_wire(w)
        g = ComponentGroup(
            id=uuid.uuid4(),
            name="G",
            component_ids=frozenset({a}),
        )
        _attach_to_root(ctx, g)
        old_b_pos = _pin_pos(ctx, b, 1)
        _drag_components(ctx, [a], 10.0, 0.0)

        defn = ctx.components["R"]
        new_a = pin_world_position(
            defn.default_footprint(ctx.package_families), ctx.layout.placements[a], 2
        )
        new_b = pin_world_position(
            defn.default_footprint(ctx.package_families), ctx.layout.placements[b], 1
        )
        assert new_a.x == 15.0
        # B did not move.
        assert (new_b.x, new_b.y) == (old_b_pos.x, old_b_pos.y)


# ---------------------------------------------------------------------------
# Half-orphan with shared joint — drag must not mutate the shared joint
# ---------------------------------------------------------------------------


class TestSharedJointSurvivesDrag:
    def test_shared_joint_position_unchanged_by_member_drag(self) -> None:
        """A JointKnot shared by two wires is not moved by
        MoveComponentCommand on a group member — the joint sits at a
        fixed off-component world point.  This documents the *domain*
        contract; the higher-level drag pipeline relies on it to keep
        cross-group wires consistent.
        """
        ctx = _ctx()
        a = _place(ctx, 5.0, 5.0, "R1")
        b = _place(ctx, 9.0, 5.0, "R2")
        pka = ctx.layout.get_or_create_pin_knot(PinRef(a, 2))
        pkb = ctx.layout.get_or_create_pin_knot(PinRef(b, 1))
        # Shared joint at off-pin point.
        shared = Point(6.5, 7.5)
        jk = ctx.layout.create_joint_knot(shared)
        w1 = Wire(
            id=uuid.uuid4(),
            path=[_pin_pos(ctx, a, 2), shared],
            side="top",
            a=KnotRef(id=pka.id),
            b=KnotRef(id=jk.id),
        )
        ctx.layout.add_wire(w1)
        w2 = Wire(
            id=uuid.uuid4(),
            path=[shared, _pin_pos(ctx, b, 1)],
            side="top",
            a=KnotRef(id=jk.id),
            b=KnotRef(id=pkb.id),
        )
        ctx.layout.add_wire(w2)
        # Live joint instance carries both wire ids.
        live = ctx.layout.knots[jk.id]
        assert isinstance(live, JointKnot)
        assert len(live.wire_ids) == 2

        g = ComponentGroup(
            id=uuid.uuid4(),
            name="G",
            component_ids=frozenset({a}),
        )
        _attach_to_root(ctx, g)
        # Drag a only — joint must stay where it is.
        _drag_components(ctx, [a], 10.0, 0.0)
        after = ctx.layout.knots[jk.id]
        assert isinstance(after, JointKnot)
        assert (after.position.x, after.position.y) == (6.5, 7.5)


# ---------------------------------------------------------------------------
# Net assignment of created wires (sanity — pin endpoints retain their
# net binding through drag/rotate; net membership doesn't depend on
# geometry).
# ---------------------------------------------------------------------------


class TestNetAssignmentSurvivesGeometry:
    def test_drag_preserves_net_membership(self) -> None:
        ctx = _ctx()
        a = _place(ctx, 5.0, 5.0, "R1")
        b = _place(ctx, 5.0, 9.0, "R2")
        pka = ctx.layout.get_or_create_pin_knot(PinRef(a, 2))
        pkb = ctx.layout.get_or_create_pin_knot(PinRef(b, 1))
        w = Wire(
            id=uuid.uuid4(),
            path=[_pin_pos(ctx, a, 2), _pin_pos(ctx, b, 1)],
            side="top",
            a=KnotRef(id=pka.id),
            b=KnotRef(id=pkb.id),
        )
        ctx.layout.add_wire(w)
        net = Net(
            id=uuid.uuid4(),
            name="N1",
            pins={PinRef(a, 2), PinRef(b, 1)},
        )
        ctx.schematic.nets[net.id] = net
        g = ComponentGroup(
            id=uuid.uuid4(),
            name="G",
            component_ids=frozenset({a, b}),
        )
        _attach_to_root(ctx, g)
        _drag_components(ctx, [a, b], 5.0, 5.0, drag_wire_paths=True)
        # Net membership unchanged.
        assert ctx.schematic.nets[net.id].pins == {
            PinRef(a, 2),
            PinRef(b, 1),
        }
        # Wire still owned after drag (PinKnot endpoints intact).
        assert (
            wire_is_owned_by_group(
                w,
                g,
                wire_net_map={w.id: net.id},
                knots=ctx.layout.knots,  # type: ignore[arg-type]
                group_comp_ids=frozenset({a, b}),
                schematic=ctx.schematic,
                placements=ctx.layout.placements,
                instances=ctx.instances,
                definitions=ctx.components,
            )
            is True
        )

    def test_rotate_preserves_net_membership(self) -> None:
        ctx = _ctx()
        a = _place(ctx, 5.0, 5.0, "R1")
        b = _place(ctx, 5.0, 9.0, "R2")
        pka = ctx.layout.get_or_create_pin_knot(PinRef(a, 2))
        pkb = ctx.layout.get_or_create_pin_knot(PinRef(b, 1))
        w = Wire(
            id=uuid.uuid4(),
            path=[_pin_pos(ctx, a, 2), _pin_pos(ctx, b, 1)],
            side="top",
            a=KnotRef(id=pka.id),
            b=KnotRef(id=pkb.id),
        )
        ctx.layout.add_wire(w)
        net = Net(
            id=uuid.uuid4(),
            name="N1",
            pins={PinRef(a, 2), PinRef(b, 1)},
        )
        ctx.schematic.nets[net.id] = net
        g = ComponentGroup(
            id=uuid.uuid4(),
            name="G",
            component_ids=frozenset({a, b}),
        )
        _attach_to_root(ctx, g)
        RotateGroupCommand(group_id=g.id, delta=90).apply(ctx)
        assert ctx.schematic.nets[net.id].pins == {
            PinRef(a, 2),
            PinRef(b, 1),
        }
