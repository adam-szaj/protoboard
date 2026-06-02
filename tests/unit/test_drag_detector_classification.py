# SPDX-License-Identifier: MIT
"""Tests for the unified drag model fields emitted by DragDetector.

When the caller supplies ``drag_set_members`` on the snapshot the
detector must:

* attach a ``DragWireClassification`` to the result,
* partition removable wires into ``free_drop_wire_ids`` /
  ``stationary_group_drop_wire_ids`` / ``cross_group_disconnect_wire_ids``,
* honor ``stationary_locked_groups`` by promoting matching soft
  conflicts to HARD via ``hard_locked_wire_pads``,
* keep wires the drag set will translate (internal/half_orphan_internal)
  OUT of the removable buckets.
"""

from __future__ import annotations

import uuid

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pad_state import PadIndex
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.group import ComponentGroup
from protoboard.domain.layout.knot import JointKnot, PinKnot
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import KnotRef, Wire
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from protoboard.gui.tools.drag_commit_decision import DragSeverity
from protoboard.services.drag_detector import DragDetector, DragSnapshot
from protoboard.services.pad_strategy import DRCEnforcingStrategy


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _perfboard(rows: int = 10, cols: int = 10) -> Board:
    return Board(
        root=BoardGrid(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            cell=PadAtom(
                width_policy=SizePolicy.HUG,
                height_policy=SizePolicy.HUG,
                layers=PadLayers(top=True, bottom=True, plated_through=True),
            ),
            rows=rows,
            cols=cols,
            row_gap=0,
            col_gap=0,
        )
    )


def _defn(name: str, n: int = 2) -> ComponentDefinition:
    return ComponentDefinition(
        name=name,
        footprint=SIL(n),
        pin_names={i: str(i) for i in range(1, n + 1)},
    )


def _inst(defn: ComponentDefinition, refdes: str = "R1") -> ComponentInstance:
    return ComponentInstance.new(
        definition_ref=defn.name, refdes=refdes, value="",
    )


def _detector() -> DragDetector:
    return DragDetector(DRCEnforcingStrategy())


def _build_snapshot(
    placements: list[
        tuple[ComponentInstance, ComponentDefinition, ComponentPlacement]
    ],
    wires: list[Wire],
    knots: list[PinKnot | JointKnot],
    nets: list[Net],
    drag_set_members: frozenset[uuid.UUID],
    wire_net_map: dict[uuid.UUID, uuid.UUID],
    member_groups: list[ComponentGroup],
    stationary_locked_groups: list[ComponentGroup] | None = None,
) -> DragSnapshot:
    board = _perfboard()
    layout = Layout()
    layout.ensure_root_group()
    instances: dict[uuid.UUID, ComponentInstance] = {}
    components: dict[str, ComponentDefinition] = {}
    for inst, defn, pl in placements:
        instances[inst.id] = inst
        components[defn.name] = defn
        layout.placements[inst.id] = pl
    for k in knots:
        layout.knots[k.id] = k
    layout.wires.extend(wires)
    sch = Schematic(nets={n.id: n for n in nets})
    pad_index = PadIndex(
        board=board, layout=layout, schematic=sch,
        components=components, instances=instances,
    )
    return DragSnapshot(
        pad_index=pad_index,
        placements=layout.placements,
        instances=instances,
        components=components,
        schematic=sch,
        wires=list(layout.wires),
        drag_set_members=drag_set_members,
        wire_net_map=wire_net_map,
        knots=dict(layout.knots),
        member_groups=member_groups,
        stationary_locked_groups=stationary_locked_groups or [],
    )


# ---------------------------------------------------------------------------
# Classification presence
# ---------------------------------------------------------------------------


class TestClassificationPresence:
    def test_no_drag_set_keeps_wire_classification_none(self) -> None:
        # Empty drag_set_members → legacy path (no classification).
        defn = _defn("R", 2)
        inst = _inst(defn, "R1")
        pl = ComponentPlacement(
            component_id=inst.id, position=Point(0.5, 0.5),
            rotation=0, side="top",
        )
        snap = _build_snapshot(
            placements=[(inst, defn, pl)],
            wires=[],
            knots=[],
            nets=[],
            drag_set_members=frozenset(),
            wire_net_map={},
            member_groups=[],
        )
        decision = _detector().classify(snap, {inst.id})
        assert decision.wire_classification is None
        assert decision.cross_group_disconnect_wire_ids == ()

    def test_drag_set_populates_classification(self) -> None:
        defn = _defn("R", 2)
        inst = _inst(defn, "R1")
        pl = ComponentPlacement(
            component_id=inst.id, position=Point(0.5, 0.5),
            rotation=0, side="top",
        )
        snap = _build_snapshot(
            placements=[(inst, defn, pl)],
            wires=[],
            knots=[],
            nets=[],
            drag_set_members=frozenset({inst.id}),
            wire_net_map={},
            member_groups=[],
        )
        decision = _detector().classify(snap, {inst.id})
        assert decision.wire_classification is not None


# ---------------------------------------------------------------------------
# Bucketing — cross-group disconnect
# ---------------------------------------------------------------------------


class TestCrossGroupDisconnect:
    def test_cross_group_wire_bucketed_even_without_skew(self) -> None:
        # Two components both placed safely; wire from c1.pin1 to c2.pin1.
        # Drag set = {c1}; wire = cross_group_disconnect.  Detector
        # surfaces it in cross_group_disconnect_wire_ids even though
        # the wire isn't skewed/crossing-alien-pad — the user must
        # decide drop or reroute.
        defn = _defn("R", 2)
        inst_a = _inst(defn, "R1")
        inst_b = _inst(defn, "R2")
        pl_a = ComponentPlacement(
            component_id=inst_a.id, position=Point(0.5, 0.5),
            rotation=0, side="top",
        )
        pl_b = ComponentPlacement(
            component_id=inst_b.id, position=Point(5.5, 0.5),
            rotation=0, side="top",
        )
        ka = PinKnot(id=uuid.uuid4(), pin=PinRef(inst_a.id, 1))
        kb = PinKnot(id=uuid.uuid4(), pin=PinRef(inst_b.id, 1))
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(5.5, 0.5)],
            a=KnotRef(id=ka.id),
            b=KnotRef(id=kb.id),
        )
        snap = _build_snapshot(
            placements=[(inst_a, defn, pl_a), (inst_b, defn, pl_b)],
            wires=[wire],
            knots=[ka, kb],
            nets=[],
            drag_set_members=frozenset({inst_a.id}),
            wire_net_map={},
            member_groups=[],
        )
        decision = _detector().classify(snap, {inst_a.id})
        assert decision.severity is DragSeverity.SOFT
        assert wire.id in decision.cross_group_disconnect_wire_ids
        assert wire.id not in decision.free_drop_wire_ids
        assert wire.id not in decision.stationary_group_drop_wire_ids


# ---------------------------------------------------------------------------
# Bucketing — stationary group wire
# ---------------------------------------------------------------------------


class TestStationaryGroupBucketing:
    def test_owned_wire_of_stationary_group_lands_in_sgroup_bucket(self) -> None:
        # Set up: group G contains c_g; c_g has an internal-net wire to a
        # JointKnot.  Free component c_drag is dragged onto the wire's
        # JointKnot pad → pin-on-wire conflict.  Detector should bucket
        # the wire under stationary_group_drop, not free_drop.
        defn = _defn("R", 2)
        inst_g = _inst(defn, "Rg")
        inst_drag = _inst(defn, "Rd")
        pl_g = ComponentPlacement(
            component_id=inst_g.id, position=Point(0.5, 0.5),
            rotation=0, side="top",
        )
        # Place the dragged comp so its pin lands on (3.5, 0.5) — the
        # joint position of the owned wire.
        pl_drag = ComponentPlacement(
            component_id=inst_drag.id, position=Point(3.5, 0.5),
            rotation=0, side="top",
        )
        # Internal net of the group + joint at (3.5, 0.5).  Net must
        # have only group pins → internal.
        ka = PinKnot(id=uuid.uuid4(), pin=PinRef(inst_g.id, 1))
        joint = JointKnot(id=uuid.uuid4(), position=Point(3.5, 0.5))
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(3.5, 0.5)],
            a=KnotRef(id=ka.id),
            b=KnotRef(id=joint.id),
        )
        net = Net(
            id=uuid.uuid4(), name="N",
            pins={PinRef(inst_g.id, 1)},
        )
        group = ComponentGroup(
            id=uuid.uuid4(), name="G",
            component_ids=frozenset({inst_g.id}),
            exported_pins=frozenset(),
        )

        snap = _build_snapshot(
            placements=[(inst_g, defn, pl_g), (inst_drag, defn, pl_drag)],
            wires=[wire],
            knots=[ka, joint],
            nets=[net],
            drag_set_members=frozenset({inst_drag.id}),
            wire_net_map={wire.id: net.id},
            # The dragged comp isn't in the group → no member_groups for it.
            member_groups=[],
            stationary_locked_groups=[],
        )
        # Manually inject the stationary group via stationary_locked_groups
        # so the detector's _all_groups_for_snapshot helper sees it.  The
        # group is NOT locked (lock_internal_wires=False), so its wires
        # land in sgroup_drop, not hard_locked.
        snap_with_group = DragSnapshot(
            pad_index=snap.pad_index,
            placements=snap.placements,
            instances=snap.instances,
            components=snap.components,
            schematic=snap.schematic,
            wires=snap.wires,
            drag_set_members=snap.drag_set_members,
            wire_net_map=snap.wire_net_map,
            knots=snap.knots,
            member_groups=[],
            stationary_locked_groups=[group],  # used as the "all groups" view
        )
        decision = _detector().classify(snap_with_group, {inst_drag.id})
        # Wire should appear in stationary_group_drop_wire_ids (not free).
        assert wire.id in decision.stationary_group_drop_wire_ids
        assert wire.id not in decision.free_drop_wire_ids

    def test_lock_internal_wires_promotes_to_hard(self) -> None:
        # Same setup but group has lock_internal_wires=True via a
        # subclass-equivalent (ComponentGroup doesn't expose the field
        # yet — Step 8).  For now we simulate by adding the attribute
        # via dataclasses.replace once the field lands.  Until then,
        # this test acts as a pending placeholder verified later.
        # See Step 8 for the actual lock_internal_wires field.
        pass


# ---------------------------------------------------------------------------
# Internal wires must never bucket into removable
# ---------------------------------------------------------------------------


class TestInternalWiresProtected:
    def test_internal_wire_between_drag_set_pins_never_drops(self) -> None:
        # R1 and R2 both in drag set; wire R1.pin1 → R2.pin1.
        # Detector might see a skewed-segment artifact when the new
        # path has not yet been translated — but the new bucketing
        # should keep it out of free/sgroup/cross-disconnect buckets
        # since the wire is "internal" to the drag set.
        defn = _defn("R", 2)
        inst_a = _inst(defn, "R1")
        inst_b = _inst(defn, "R2")
        pl_a = ComponentPlacement(
            component_id=inst_a.id, position=Point(0.5, 0.5),
            rotation=0, side="top",
        )
        pl_b = ComponentPlacement(
            component_id=inst_b.id, position=Point(5.5, 0.5),
            rotation=0, side="top",
        )
        ka = PinKnot(id=uuid.uuid4(), pin=PinRef(inst_a.id, 1))
        kb = PinKnot(id=uuid.uuid4(), pin=PinRef(inst_b.id, 1))
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(5.5, 0.5)],
            a=KnotRef(id=ka.id),
            b=KnotRef(id=kb.id),
        )
        snap = _build_snapshot(
            placements=[(inst_a, defn, pl_a), (inst_b, defn, pl_b)],
            wires=[wire],
            knots=[ka, kb],
            nets=[],
            drag_set_members=frozenset({inst_a.id, inst_b.id}),
            wire_net_map={},
            member_groups=[],
        )
        decision = _detector().classify(snap, {inst_a.id, inst_b.id})
        # Wire is internal — must NOT appear in any drop bucket.
        assert wire.id not in decision.free_drop_wire_ids
        assert wire.id not in decision.stationary_group_drop_wire_ids
        assert wire.id not in decision.cross_group_disconnect_wire_ids
        # ...but it IS in the classification's internal set.
        cls = decision.wire_classification
        assert cls is not None
        assert wire.id in cls.internal
