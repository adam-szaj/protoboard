# SPDX-License-Identifier: MIT
"""Tests for ComponentGroup.lock_internal_wires (unified drag model Step 8).

Covers:
* default is False (back-compat),
* YAML round-trip (write + read),
* find_owned_wires is NOT affected (flag is drag-time, not ownership),
* DragDetector promotes locked-group internal-wire conflicts to HARD.
"""

from __future__ import annotations

import uuid

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.group import ComponentGroup
from protoboard.domain.layout.group_ownership import find_owned_wires
from protoboard.domain.layout.knot import JointKnot, PinKnot
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import KnotRef, Wire
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from tests.unit._autoroute_invariants import make_sil, sil_families


class TestDefault:
    def test_default_is_false(self) -> None:
        g = ComponentGroup(
            id=uuid.uuid4(),
            name="G",
            component_ids=frozenset(),
        )
        assert g.lock_internal_wires is False


class TestOwnershipUnaffected:
    def test_find_owned_wires_ignores_lock_flag(self) -> None:
        # Two PinKnots both in group → wire is owned regardless of
        # lock_internal_wires.  Flag only affects DragDetector behaviour.
        c1, c2 = uuid.uuid4(), uuid.uuid4()
        g_open = ComponentGroup(
            id=uuid.uuid4(),
            name="G",
            component_ids=frozenset({c1, c2}),
            lock_internal_wires=False,
        )
        g_locked = ComponentGroup(
            id=uuid.uuid4(),
            name="G",
            component_ids=frozenset({c1, c2}),
            lock_internal_wires=True,
        )
        ka = PinKnot(id=uuid.uuid4(), pin=PinRef(c1, 1))
        kb = PinKnot(id=uuid.uuid4(), pin=PinRef(c2, 1))
        w = Wire(
            id=uuid.uuid4(),
            path=[Point(0, 0), Point(1, 0)],
            a=KnotRef(id=ka.id),
            b=KnotRef(id=kb.id),
        )
        knots = {ka.id: ka, kb.id: kb}
        sch = Schematic(nets={})

        owned_open = find_owned_wires(
            [w],
            g_open,
            wire_net_map={},
            knots=knots,  # type: ignore[arg-type]
            group_comp_ids=frozenset({c1, c2}),
            schematic=sch,
        )
        owned_locked = find_owned_wires(
            [w],
            g_locked,
            wire_net_map={},
            knots=knots,  # type: ignore[arg-type]
            group_comp_ids=frozenset({c1, c2}),
            schematic=sch,
        )
        assert owned_open == owned_locked == {w.id}


class TestYamlRoundTrip:
    def test_lock_internal_wires_roundtrips(self) -> None:
        from protoboard.services.persistence.project_yaml import (
            _layout_to_yaml,
            _yaml,
        )

        layout = Layout()
        layout.ensure_root_group()
        gid = uuid.uuid4()
        layout.groups[gid] = ComponentGroup(
            id=gid,
            name="LockedG",
            component_ids=frozenset({uuid.uuid4()}),
            lock_internal_wires=True,
        )
        yaml_str = _layout_to_yaml(layout)
        assert "lock_internal_wires" in yaml_str
        # Round-trip parse.
        data = _yaml.load(yaml_str)
        groups_raw = data.get("groups") or []
        # Find the locked group entry by name.
        found = None
        for raw in groups_raw:
            if raw.get("name") == "LockedG":
                found = raw
                break
        assert found is not None
        assert found.get("lock_internal_wires") is True

    def test_unlocked_default_omits_field(self) -> None:
        # lock_internal_wires=False should not bloat the YAML.
        from protoboard.services.persistence.project_yaml import _layout_to_yaml

        layout = Layout()
        layout.ensure_root_group()
        gid = uuid.uuid4()
        layout.groups[gid] = ComponentGroup(
            id=gid,
            name="OpenG",
            component_ids=frozenset({uuid.uuid4()}),
        )
        yaml_str = _layout_to_yaml(layout)
        assert "lock_internal_wires" not in yaml_str


# ---------------------------------------------------------------------------
# DragDetector promotion to HARD
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


class TestDragDetectorLockPromotion:
    def test_locked_group_wire_promotes_pin_on_wire_to_hard(self) -> None:
        # Setup: G locked, has internal-net wire from c_g.pin1 to a
        # joint at (3.5, 0.5).  Foreign comp c_drag dragged so pin1
        # lands on (3.5, 0.5).  Detector should set severity=HARD via
        # hard_locked_wire_pads.
        from protoboard.controller.tools.drag_commit_decision import DragSeverity
        from protoboard.domain.board.pad_state import PadIndex
        from protoboard.services.drag_detector import (
            DragDetector,
            DragSnapshot,
        )
        from protoboard.services.pad_strategy import DRCEnforcingStrategy

        defn = make_sil("R", 2)
        families = sil_families(defn)
        inst_g = ComponentInstance.new(definition_ref="R", refdes="Rg", value="").bind(
            defn, families
        )
        inst_drag = ComponentInstance.new(
            definition_ref="R", refdes="Rd", value=""
        ).bind(defn, families)
        pl_g = ComponentPlacement(
            component_id=inst_g.id,
            position=Point(0.5, 0.5),
            rotation=0,
            side="top",
        )
        pl_drag = ComponentPlacement(
            component_id=inst_drag.id,
            position=Point(3.5, 0.5),
            rotation=0,
            side="top",
        )
        ka = PinKnot(id=uuid.uuid4(), pin=PinRef(inst_g.id, 1))
        joint = JointKnot(id=uuid.uuid4(), position=Point(3.5, 0.5))
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(3.5, 0.5)],
            a=KnotRef(id=ka.id),
            b=KnotRef(id=joint.id),
        )
        net = Net(id=uuid.uuid4(), name="N", pins={PinRef(inst_g.id, 1)})
        group_locked = ComponentGroup(
            id=uuid.uuid4(),
            name="G",
            component_ids=frozenset({inst_g.id}),
            lock_internal_wires=True,
        )

        layout = Layout()
        layout.ensure_root_group()
        layout.placements[inst_g.id] = pl_g
        layout.placements[inst_drag.id] = pl_drag
        layout.knots[ka.id] = ka
        layout.knots[joint.id] = joint
        layout.add_wire(wire)
        sch = Schematic(nets={net.id: net})
        pad_index = PadIndex(
            board=_perfboard(),
            layout=layout,
            schematic=sch,
            components={"R": defn},
            instances={inst_g.id: inst_g, inst_drag.id: inst_drag},
        )

        snap = DragSnapshot(
            pad_index=pad_index,
            placements=layout.placements,
            instances={inst_g.id: inst_g, inst_drag.id: inst_drag},
            components={"R": defn},
            schematic=sch,
            wires=list(layout.wires),
            drag_set_members=frozenset({inst_drag.id}),
            wire_net_map={wire.id: net.id},
            knots=dict(layout.knots),
            member_groups=[],
            stationary_locked_groups=[group_locked],
        )
        decision = DragDetector(DRCEnforcingStrategy()).classify(
            snap,
            {inst_drag.id},
        )
        assert decision.severity is DragSeverity.HARD
        assert len(decision.hard_locked_wire_pads) > 0
