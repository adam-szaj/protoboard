# SPDX-License-Identifier: MIT
"""Tests for DragDetector — strategy-driven drag-conflict classifier.

Mirrors test_classify_drag_commit scenarios at the DragDetector level so
the new strategy path is exercised directly (without going through the
placement_conflict.py wrapper).
"""

from __future__ import annotations

import uuid

from protoboard.controller.tools.drag_commit_decision import DragSeverity
from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pad_state import PadIndex
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.components.definition import (
    ComponentDefinition,
    MetaPin,
    PackageOption,
)
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.package import PackageFamily, PackageVariant
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.pad_strategy import DRCEnforcingStrategy
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import Wire
from protoboard.domain.schematic.net import Net
from protoboard.domain.schematic.schematic import PinRef, Schematic
from protoboard.geometry import Point
from protoboard.services.drag_detector import DragDetector, DragSnapshot
from tests.unit._autoroute_invariants import make_sil, sil_families

_TEST_FAMILIES: dict[str, PackageFamily] = {}


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
    defn = make_sil(name, n)
    _TEST_FAMILIES.update(sil_families(defn))
    return defn


def _rigid_defn(name: str, n: int = 2) -> ComponentDefinition:
    """A definition with a RIGID body (fixed pads, body blocks) over SIL(n).

    The generic synth package is non-rigid by design (bodies float, only pads
    matter for the bbox) — body-overlap severity needs a rigid body to fire.
    """
    import dataclasses

    from protoboard.domain.components.package.kind import (
        synth_generic_package,
    )
    from protoboard.domain.components.package.shapes import Prism

    pkg = synth_generic_package(SIL(n))
    assert isinstance(pkg, Prism)
    pkg = dataclasses.replace(pkg, bend_holes=0.0, kind=None)
    family_id = f"{name}-package"
    _TEST_FAMILIES[family_id] = PackageFamily(
        family_id=family_id,
        variants=(PackageVariant("std", "std", pkg),),
    )
    return ComponentDefinition(
        name=name,
        meta_pins=tuple(MetaPin((str(pin),)) for pin in range(1, n + 1)),
        package_options=(
            PackageOption(
                name=name,
                ref=f"{family_id}/std",
                pin_map={pin: (pin,) for pin in range(1, n + 1)},
            ),
        ),
    )


def _inst(defn: ComponentDefinition, refdes: str = "R1") -> ComponentInstance:
    return ComponentInstance.new(
        definition_ref=defn.name,
        refdes=refdes,
        value="",
    ).bind(defn, _TEST_FAMILIES)


def _snapshot(
    placements: list[tuple[ComponentInstance, ComponentDefinition, ComponentPlacement]],
    wires: list[Wire] | None = None,
    nets: list[Net] | None = None,
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
    if wires:
        layout.wires.extend(wires)
    sch = Schematic(nets={n.id: n for n in (nets or [])})
    pad_index = PadIndex(
        board=board,
        layout=layout,
        schematic=sch,
        components=components,
        instances=instances,
    )
    return DragSnapshot(
        pad_index=pad_index,
        placements=layout.placements,
        instances=instances,
        components=components,
        schematic=sch,
        wires=list(layout.wires),
    )


def _detector() -> DragDetector:
    return DragDetector(DRCEnforcingStrategy())


# ---------------------------------------------------------------------------
# Severity contract — mirrors classify_drag_commit's existing tests
# ---------------------------------------------------------------------------


class TestSeverity:
    def test_clear_when_nothing_conflicts(self) -> None:
        defn = _defn("R", 2)
        inst = _inst(defn, "R1")
        pl = ComponentPlacement(
            component_id=inst.id,
            position=Point(0.5, 0.5),
            rotation=0,
            side="top",
        )
        snap = _snapshot([(inst, defn, pl)])
        decision = _detector().classify(snap, {inst.id})
        assert decision.severity is DragSeverity.CLEAR
        assert decision.auto_remove_wire_ids == ()
        assert decision.description == ""

    def test_hard_on_body_overlap(self) -> None:
        # Rigid bodies: the generic synth body floats (bendable), so only a
        # rigid package's body bbox hard-blocks.
        defn_a = _rigid_defn("A", 2)
        defn_b = _rigid_defn("B", 2)
        inst_a = _inst(defn_a, "A1")
        inst_b = _inst(defn_b, "B1")
        # Bboxes overlap; no pin coincidence.
        pl_a = ComponentPlacement(
            component_id=inst_a.id,
            position=Point(0.5, 0.5),
            rotation=0,
            side="top",
        )
        pl_b = ComponentPlacement(
            component_id=inst_b.id,
            position=Point(0.8, 0.5),
            rotation=0,
            side="top",
        )
        snap = _snapshot([(inst_a, defn_a, pl_a), (inst_b, defn_b, pl_b)])
        decision = _detector().classify(snap, {inst_a.id})
        assert decision.severity is DragSeverity.HARD
        assert "body overlap" in decision.description

    def test_hard_on_pad_pad_different_nets(self) -> None:
        defn = _defn("R", 2)
        inst_a = _inst(defn, "R1")
        inst_b = _inst(defn, "R2")
        pl_a = ComponentPlacement(
            component_id=inst_a.id,
            position=Point(0.5, 0.5),
            rotation=0,
            side="top",
        )
        pl_b = ComponentPlacement(
            component_id=inst_b.id,
            position=Point(0.5, 0.5),
            rotation=0,
            side="top",
        )
        net_a = uuid.uuid4()
        net_b = uuid.uuid4()
        nets = [
            Net(
                id=net_a,
                name="VCC",
                pins=set({PinRef(component_id=inst_a.id, pin_number=1)}),
            ),
            Net(
                id=net_b,
                name="GND",
                pins=set({PinRef(component_id=inst_b.id, pin_number=1)}),
            ),
        ]
        snap = _snapshot(
            [(inst_a, defn, pl_a), (inst_b, defn, pl_b)],
            nets=nets,
        )
        decision = _detector().classify(snap, {inst_a.id})
        assert decision.severity is DragSeverity.HARD
        assert "different-net" in decision.description

    def test_soft_on_pin_on_foreign_wire(self) -> None:
        defn = _defn("R", 2)
        inst_a = _inst(defn, "R1")
        inst_b = _inst(defn, "R2")
        pl_a = ComponentPlacement(
            component_id=inst_a.id,
            position=Point(3.5, 0.5),
            rotation=0,
            side="top",
        )
        pl_b = ComponentPlacement(
            component_id=inst_b.id,
            position=Point(0.5, 0.5),
            rotation=0,
            side="top",
        )
        # Insulated so the wire passes through middle without bare-cross
        # rejection on construction.
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(3.5, 0.5), Point(7.5, 0.5)],
            insulated=True,
            side="top",
        )
        net_n = uuid.uuid4()
        nets = [
            Net(
                id=net_n,
                name="N1",
                pins=set({PinRef(component_id=inst_b.id, pin_number=1)}),
            ),
        ]
        snap = _snapshot(
            [(inst_a, defn, pl_a), (inst_b, defn, pl_b)],
            wires=[wire],
            nets=nets,
        )
        decision = _detector().classify(snap, {inst_a.id})
        assert decision.severity is DragSeverity.SOFT
        assert wire.id in decision.auto_remove_wire_ids

    def test_soft_on_pin_on_same_net_wire_rule6_strict(self) -> None:
        """Rule 6 strict: even same-net wire on pin pad must be removed."""
        defn = _defn("R", 2)
        inst_a = _inst(defn, "R1")
        inst_b = _inst(defn, "R2")
        pl_a = ComponentPlacement(
            component_id=inst_a.id,
            position=Point(3.5, 0.5),
            rotation=0,
            side="top",
        )
        pl_b = ComponentPlacement(
            component_id=inst_b.id,
            position=Point(0.5, 0.5),
            rotation=0,
            side="top",
        )
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(3.5, 0.5), Point(7.5, 0.5)],
            insulated=True,
            side="top",
        )
        net_n = uuid.uuid4()
        nets = [
            Net(
                id=net_n,
                name="N1",
                pins=set(
                    {
                        PinRef(component_id=inst_a.id, pin_number=1),
                        PinRef(component_id=inst_b.id, pin_number=1),
                    }
                ),
            ),
        ]
        snap = _snapshot(
            [(inst_a, defn, pl_a), (inst_b, defn, pl_b)],
            wires=[wire],
            nets=nets,
        )
        decision = _detector().classify(snap, {inst_a.id})
        # Same-net is still SOFT (not CLEAR) — wire gets auto-removed.
        assert decision.severity is DragSeverity.SOFT
        assert wire.id in decision.auto_remove_wire_ids

    def test_soft_on_both_unconnected_pad(self) -> None:
        defn = _defn("R", 2)
        inst_a = _inst(defn, "R1")
        inst_b = _inst(defn, "R2")
        pl_a = ComponentPlacement(
            component_id=inst_a.id,
            position=Point(0.5, 0.5),
            rotation=0,
            side="top",
        )
        pl_b = ComponentPlacement(
            component_id=inst_b.id,
            position=Point(0.5, 0.5),
            rotation=0,
            side="top",
        )
        snap = _snapshot([(inst_a, defn, pl_a), (inst_b, defn, pl_b)])
        decision = _detector().classify(snap, {inst_a.id})
        assert decision.severity is DragSeverity.SOFT
        assert "shared pad" in decision.description

    def test_soft_on_skewed_dragged_wire(self) -> None:
        defn = _defn("R", 2)
        inst = _inst(defn, "R1")
        pl = ComponentPlacement(
            component_id=inst.id,
            position=Point(0.5, 0.5),
            rotation=0,
            side="top",
        )
        skewed = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(2.5, 3.5)],
            insulated=True,
            side="top",
        )
        snap = _snapshot([(inst, defn, pl)], wires=[skewed])
        decision = _detector().classify(
            snap,
            {inst.id},
            dragged_wire_ids=frozenset({skewed.id}),
        )
        assert decision.severity is DragSeverity.SOFT
        assert skewed.id in decision.auto_remove_wire_ids
        assert "skewed" in decision.description

    def test_hard_wins_over_soft(self) -> None:
        defn = _defn("R", 2)
        inst_a = _inst(defn, "R1")
        inst_b = _inst(defn, "R2")
        pl_a = ComponentPlacement(
            component_id=inst_a.id,
            position=Point(0.5, 0.5),
            rotation=0,
            side="top",
        )
        pl_b = ComponentPlacement(
            component_id=inst_b.id,
            position=Point(0.5, 0.5),
            rotation=0,
            side="top",
        )
        net_a = uuid.uuid4()
        net_b = uuid.uuid4()
        nets = [
            Net(
                id=net_a,
                name="VCC",
                pins=set({PinRef(component_id=inst_a.id, pin_number=1)}),
            ),
            Net(
                id=net_b,
                name="GND",
                pins=set({PinRef(component_id=inst_b.id, pin_number=1)}),
            ),
        ]
        skewed = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(2.5, 3.5)],
            insulated=True,
            side="top",
        )
        snap = _snapshot(
            [(inst_a, defn, pl_a), (inst_b, defn, pl_b)],
            wires=[skewed],
            nets=nets,
        )
        decision = _detector().classify(
            snap,
            {inst_a.id},
            dragged_wire_ids=frozenset({skewed.id}),
        )
        assert decision.severity is DragSeverity.HARD


# ---------------------------------------------------------------------------
# Strategy hook — verify the gate runs (rule 6 strict via DRCEnforcing)
# ---------------------------------------------------------------------------


class TestStrategyHook:
    def test_wire_on_bottom_side_detected_from_top_drag(self) -> None:
        """Pin on top, wire on bottom — through-hole pin penetrates board so
        even bottom wires conflict (rule 5 → rule 6 strict)."""
        defn = _defn("R", 2)
        inst_a = _inst(defn, "R1")
        inst_b = _inst(defn, "R2")
        pl_a = ComponentPlacement(
            component_id=inst_a.id,
            position=Point(3.5, 0.5),
            rotation=0,
            side="top",
        )
        pl_b = ComponentPlacement(
            component_id=inst_b.id,
            position=Point(0.5, 0.5),
            rotation=0,
            side="top",
        )
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(3.5, 0.5), Point(7.5, 0.5)],
            insulated=False,
            side="bottom",
        )
        net_n = uuid.uuid4()
        nets = [
            Net(
                id=net_n,
                name="N1",
                pins=set({PinRef(component_id=inst_b.id, pin_number=1)}),
            ),
        ]
        snap = _snapshot(
            [(inst_a, defn, pl_a), (inst_b, defn, pl_b)],
            wires=[wire],
            nets=nets,
        )
        decision = _detector().classify(snap, {inst_a.id})
        assert decision.severity is DragSeverity.SOFT
        assert wire.id in decision.auto_remove_wire_ids
