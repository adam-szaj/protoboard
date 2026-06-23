# SPDX-License-Identifier: MIT
"""Tests for classify_drag_commit — the Qt-free drag-commit classifier."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement, pin_world_position
from protoboard.domain.layout.wire import Wire
from protoboard.domain.schematic.net import Net
from protoboard.domain.schematic.schematic import PinRef, Schematic
from protoboard.geometry import Point
from protoboard.gui.placement_conflict import classify_drag_commit
from protoboard.gui.tools.drag_commit_decision import DragSeverity


# ---------------------------------------------------------------------------
# Minimal EditorSession stand-in — covers the attributes classify_drag_commit
# uses (layout, instances, components, schematic, get_pin_position).
# ---------------------------------------------------------------------------

@dataclass
class _SessionStub:
    layout: Layout
    instances: dict[uuid.UUID, ComponentInstance]
    components: dict[str, ComponentDefinition]
    schematic: Schematic = field(default_factory=lambda: Schematic(nets={}))

    def get_pin_position(self, comp_id: uuid.UUID, pin_num: int) -> Point | None:
        pl = self.layout.placements.get(comp_id)
        inst = self.instances.get(comp_id)
        if pl is None or inst is None:
            return None
        defn = self.components.get(inst.definition_ref)
        if defn is None:
            return None
        return pin_world_position(defn.footprint, pl, pin_num)


def _defn(name: str, n: int = 2) -> ComponentDefinition:
    return ComponentDefinition(
        name=name, footprint=SIL(n), pin_names={i: str(i) for i in range(1, n + 1)}
    )


def _inst(defn: ComponentDefinition, refdes: str = "R1") -> ComponentInstance:
    return ComponentInstance.new(definition_ref=defn.name, refdes=refdes, value="")


def _pl(x: float, y: float) -> ComponentPlacement:
    return ComponentPlacement(
        component_id=uuid.uuid4(), position=Point(x, y), rotation=0, side="top"
    )


def _build(
    placements: list[tuple[ComponentInstance, ComponentDefinition, ComponentPlacement]],
    wires: list[Wire] | None = None,
    nets: list[Net] | None = None,
) -> _SessionStub:
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
    return _SessionStub(
        layout=layout, instances=instances, components=components, schematic=sch
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestClassifyDragCommit:
    def test_clear_when_nothing_conflicts(self) -> None:
        defn = _defn("R", 2)
        inst = _inst(defn, "R1")
        pl = ComponentPlacement(
            component_id=inst.id, position=Point(0.5, 0.5), rotation=0, side="top"
        )
        session = _build([(inst, defn, pl)])

        decision = classify_drag_commit(
            session=session,  # type: ignore[arg-type]
            moved_comp_ids={inst.id},
            dragged_wire_ids=frozenset(),
        )
        assert decision.severity == DragSeverity.CLEAR
        assert decision.auto_remove_wire_ids == ()
        assert decision.description == ""

    def test_hard_on_body_overlap(self) -> None:
        # Two 2-pin parts placed at the same position with no schematic — bodies
        # overlap, all pins overlap → "shared pad", which is BOTH_UNCONNECTED
        # (soft). To force pure body-overlap we offset so bboxes overlap but
        # pins don't coincide.
        defn_a = _defn("A", 2)
        defn_b = _defn("B", 2)
        inst_a = _inst(defn_a, "A1")
        inst_b = _inst(defn_b, "B1")
        # A spans (0,0)-(1,0); B spans (0.3,0)-(1.3,0). Bboxes overlap, pins
        # don't coincide (A pins at 0.5,0.5 / 1.5,0.5; B pins at 0.8,0.5 / 1.8,0.5).
        pl_a = ComponentPlacement(
            component_id=inst_a.id, position=Point(0.5, 0.5), rotation=0, side="top"
        )
        pl_b = ComponentPlacement(
            component_id=inst_b.id, position=Point(0.8, 0.5), rotation=0, side="top"
        )
        session = _build([(inst_a, defn_a, pl_a), (inst_b, defn_b, pl_b)])

        decision = classify_drag_commit(
            session=session,  # type: ignore[arg-type]
            moved_comp_ids={inst_a.id},
            dragged_wire_ids=frozenset(),
        )
        assert decision.severity == DragSeverity.HARD
        assert decision.blocking_pad_positions  # at least one marker
        assert "body overlap" in decision.description

    def test_hard_on_pad_pad_different_nets(self) -> None:
        net_a = uuid.uuid4()
        net_b = uuid.uuid4()
        defn = _defn("R", 2)
        inst_a = _inst(defn, "R1")
        inst_b = _inst(defn, "R2")
        # Same pin position for pin 1 of both.
        pl_a = ComponentPlacement(
            component_id=inst_a.id, position=Point(0.5, 0.5), rotation=0, side="top"
        )
        pl_b = ComponentPlacement(
            component_id=inst_b.id, position=Point(0.5, 0.5), rotation=0, side="top"
        )
        nets = [
            Net(id=net_a, name="VCC",
                pins={PinRef(component_id=inst_a.id, pin_number=1)}),
            Net(id=net_b, name="GND",
                pins={PinRef(component_id=inst_b.id, pin_number=1)}),
        ]
        session = _build(
            [(inst_a, defn, pl_a), (inst_b, defn, pl_b)],
            nets=nets,
        )

        decision = classify_drag_commit(
            session=session,  # type: ignore[arg-type]
            moved_comp_ids={inst_a.id},
            dragged_wire_ids=frozenset(),
        )
        assert decision.severity == DragSeverity.HARD
        assert decision.blocking_pad_positions
        assert "different-net" in decision.description

    def test_soft_on_pin_on_foreign_wire(self) -> None:
        # Wire belongs to net N1 (connects R2's pin 1). Moved R1 lands its pin 1
        # at (3.5, 0.5), which is in the middle of the wire path.
        defn = _defn("R", 2)
        inst_a = _inst(defn, "R1")
        inst_b = _inst(defn, "R2")
        pl_a = ComponentPlacement(
            component_id=inst_a.id, position=Point(3.5, 0.5), rotation=0, side="top"
        )  # pin1 at (3.5, 0.5), pin2 at (4.5, 0.5)
        pl_b = ComponentPlacement(
            component_id=inst_b.id, position=Point(0.5, 0.5), rotation=0, side="top"
        )  # pin1 at (0.5, 0.5)

        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(3.5, 0.5), Point(7.5, 0.5)],
            insulated=True,
            side="top",
        )
        net_n = uuid.uuid4()
        nets = [
            Net(id=net_n, name="N1",
                pins={PinRef(component_id=inst_b.id, pin_number=1)}),
        ]
        session = _build(
            [(inst_a, defn, pl_a), (inst_b, defn, pl_b)],
            wires=[wire],
            nets=nets,
        )

        decision = classify_drag_commit(
            session=session,  # type: ignore[arg-type]
            moved_comp_ids={inst_a.id},
            dragged_wire_ids=frozenset(),
        )
        assert decision.severity == DragSeverity.SOFT
        assert wire.id in decision.auto_remove_wire_ids
        assert "foreign wire" in decision.description

    def test_soft_on_both_unconnected_pad(self) -> None:
        # Two components share a pad, neither has a net → BOTH_UNCONNECTED → SOFT.
        defn = _defn("R", 2)
        inst_a = _inst(defn, "R1")
        inst_b = _inst(defn, "R2")
        pl_a = ComponentPlacement(
            component_id=inst_a.id, position=Point(0.5, 0.5), rotation=0, side="top"
        )
        pl_b = ComponentPlacement(
            component_id=inst_b.id, position=Point(0.5, 0.5), rotation=0, side="top"
        )
        session = _build([(inst_a, defn, pl_a), (inst_b, defn, pl_b)])

        decision = classify_drag_commit(
            session=session,  # type: ignore[arg-type]
            moved_comp_ids={inst_a.id},
            dragged_wire_ids=frozenset(),
        )
        assert decision.severity == DragSeverity.SOFT
        assert "shared pad" in decision.description

    def test_soft_on_skewed_dragged_wire(self) -> None:
        # Component R1 alone — no conflicts with others. But one wire that
        # moved with R1 ended up with a skewed segment (post-drag artifact).
        defn = _defn("R", 2)
        inst = _inst(defn, "R1")
        pl = ComponentPlacement(
            component_id=inst.id, position=Point(0.5, 0.5), rotation=0, side="top"
        )
        # Skewed segment: (0.5,0.5)→(2.5,3.5) — dx=2, dy=3 — neither ortho nor diag.
        skewed = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(2.5, 3.5)],
            insulated=True,
            side="top",
        )
        session = _build([(inst, defn, pl)], wires=[skewed])

        decision = classify_drag_commit(
            session=session,  # type: ignore[arg-type]
            moved_comp_ids={inst.id},
            dragged_wire_ids=frozenset({skewed.id}),
        )
        assert decision.severity == DragSeverity.SOFT
        assert skewed.id in decision.auto_remove_wire_ids
        assert "skewed" in decision.description

    def test_hard_wins_over_soft_when_both_present(self) -> None:
        # Same-position-different-nets HARD plus a skewed wire SOFT → HARD wins.
        net_a = uuid.uuid4()
        net_b = uuid.uuid4()
        defn = _defn("R", 2)
        inst_a = _inst(defn, "R1")
        inst_b = _inst(defn, "R2")
        pl_a = ComponentPlacement(
            component_id=inst_a.id, position=Point(0.5, 0.5), rotation=0, side="top"
        )
        pl_b = ComponentPlacement(
            component_id=inst_b.id, position=Point(0.5, 0.5), rotation=0, side="top"
        )
        nets = [
            Net(id=net_a, name="VCC",
                pins={PinRef(component_id=inst_a.id, pin_number=1)}),
            Net(id=net_b, name="GND",
                pins={PinRef(component_id=inst_b.id, pin_number=1)}),
        ]
        skewed = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(2.5, 3.5)],
            insulated=True,
            side="top",
        )
        session = _build(
            [(inst_a, defn, pl_a), (inst_b, defn, pl_b)],
            wires=[skewed],
            nets=nets,
        )

        decision = classify_drag_commit(
            session=session,  # type: ignore[arg-type]
            moved_comp_ids={inst_a.id},
            dragged_wire_ids=frozenset({skewed.id}),
        )
        assert decision.severity == DragSeverity.HARD


class TestClassifyDoesNotShowDialogs:
    """Belt-and-braces: classify_drag_commit must be Qt-dialog-free."""

    def test_no_qdialog_imported_on_codepath(self, monkeypatch) -> None:  # noqa: ANN001
        # If classify_drag_commit ever instantiated a Qt dialog, importing
        # the dialog module would call its module-level code. We assert that
        # the function works without QApplication present and without
        # touching dialog modules — proxy: run the CLEAR case from above.
        import sys
        # Remove dialog modules from sys.modules if present, then assert they
        # aren't re-imported by classify_drag_commit.
        for mod in (
            "protoboard.gui.dialogs.pad_conflict_dialog",
            "protoboard.gui.dialogs.wire_conflict_dialog",
        ):
            sys.modules.pop(mod, None)

        defn = _defn("R", 2)
        inst = _inst(defn, "R1")
        pl = ComponentPlacement(
            component_id=inst.id, position=Point(0.5, 0.5), rotation=0, side="top"
        )
        session = _build([(inst, defn, pl)])

        decision = classify_drag_commit(
            session=session,  # type: ignore[arg-type]
            moved_comp_ids={inst.id},
            dragged_wire_ids=frozenset(),
        )
        assert decision.severity == DragSeverity.CLEAR
        # Confirm dialog modules were NOT imported as a side effect.
        assert "protoboard.gui.dialogs.pad_conflict_dialog" not in sys.modules
        assert "protoboard.gui.dialogs.wire_conflict_dialog" not in sys.modules
