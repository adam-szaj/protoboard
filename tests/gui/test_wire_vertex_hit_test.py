# SPDX-License-Identifier: MIT
"""Cluster-cycle behaviour of ``HitTester.hit_wire_vertex`` (v2 port).

Ports ``test_wire_edit_hit_test_cycle.py`` (which drives the legacy
``SelectTool._hit_test_wire_node_any``) onto the new stateless
``HitTester.hit_wire_vertex``.  The legacy method threaded cycle state on
``self``; the v2 method takes a :class:`WireEditCycle` in and returns the next
one, so the test threads the cycle through the return value.

When the user clicks a knot where N wires of the same net meet at one world
coordinate, the hit-test must: (1) return the currently-selected wire first,
(2) rotate through the siblings on repeated clicks at the same cluster,
(3) reset the cycle on a cluster change or a miss.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import Wire
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.geometry import Point
from protoboard.controller.selection.hit_test import HitTester, WireEditCycle
from protoboard.gui.session import EditorSession
from protoboard.services.library.loader import load_library

_LIBRARY_DIR = Path(__file__).parent.parent / "fixtures" / "library"


def _perfboard(rows: int = 16, cols: int = 16) -> Board:
    grid = BoardGrid(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        ),
        rows=rows,
        cols=cols,
        row_gap=0.0,
        col_gap=0.0,
    )
    return Board(root=grid)


@pytest.fixture()
def three_wire_knot(
    qtbot: QtBot,
) -> tuple[EditorSession, list[Wire], dict[uuid.UUID, uuid.UUID]]:
    """Three bare wires of one net meeting at the shared pad (5.5, 5.5).

    wireA east, wireB south, wireC west.  A 1-pin component bound to a Net at
    the shared pad makes ``build_wire_net_map`` resolve all three to one net.
    """
    library = load_library([_LIBRARY_DIR])
    sess = EditorSession(board=_perfboard(), library=library)

    defn = ComponentDefinition.from_footprint(name="STUB1", footprint=SIL(1), pin_names={1: "P1"})
    sess.components["STUB1"] = defn
    inst = ComponentInstance.new(definition_ref="STUB1", refdes="X1", value="")
    sess.instances[inst.id] = inst
    sess.layout.placements[inst.id] = ComponentPlacement(
        component_id=inst.id, position=Point(5.5, 5.5), rotation=0, side="top",
    )
    net_id = uuid.uuid4()
    sess.schematic.nets[net_id] = Net(
        id=net_id, name="N1",
        pins={PinRef(component_id=inst.id, pin_number=1)},
    )

    wire_a = Wire(
        id=uuid.uuid4(), path=[Point(5.5, 5.5), Point(8.5, 5.5)],
        insulated=False, side="bottom",
    )
    wire_b = Wire(
        id=uuid.uuid4(), path=[Point(5.5, 5.5), Point(5.5, 8.5)],
        insulated=False, side="bottom",
    )
    wire_c = Wire(
        id=uuid.uuid4(), path=[Point(5.5, 5.5), Point(2.5, 5.5)],
        insulated=False, side="bottom",
    )
    sess.layout.wires.extend([wire_a, wire_b, wire_c])
    sess.invalidate_graph()

    return sess, [wire_a, wire_b, wire_c], sess.cached_wire_net_map()


def _tester(sess: EditorSession) -> HitTester:
    return HitTester(sess, camera_side="bottom")


class TestSharedKnotHitTest:
    def test_selected_wire_wins_first_click(self, three_wire_knot) -> None:
        sess, (a, b, c), nm = three_wire_knot
        sel, _cycle = _tester(sess).hit_wire_vertex(
            5.5, 5.5, start_wire_id=b.id, wire_net_map=nm, cycle=WireEditCycle(),
        )
        assert sel is not None
        assert sel.wire_id == b.id, (
            "selected wire must win when click lands on a shared knot"
        )

    def test_rotation_through_siblings_on_repeated_clicks(
        self, three_wire_knot
    ) -> None:
        sess, (a, b, c), nm = three_wire_knot
        tester = _tester(sess)
        seen: list[uuid.UUID] = []
        cycle = WireEditCycle()
        for _ in range(5):
            sel, cycle = tester.hit_wire_vertex(
                5.5, 5.5, start_wire_id=b.id, wire_net_map=nm, cycle=cycle,
            )
            assert sel is not None
            seen.append(sel.wire_id)

        assert seen[0] == b.id
        cycle_unique: list[uuid.UUID] = []
        for wid in seen:
            if wid not in cycle_unique:
                cycle_unique.append(wid)
        assert set(cycle_unique) == {a.id, b.id, c.id}
        assert seen[3] == seen[0], "rotation must wrap after exhausting siblings"

    def test_cluster_change_resets_cycle(self, three_wire_knot) -> None:
        sess, (a, b, c), nm = three_wire_knot
        tester = _tester(sess)
        cycle = WireEditCycle()

        sel1, cycle = tester.hit_wire_vertex(
            5.5, 5.5, start_wire_id=b.id, wire_net_map=nm, cycle=cycle,
        )
        sel2, cycle = tester.hit_wire_vertex(
            5.5, 5.5, start_wire_id=b.id, wire_net_map=nm, cycle=cycle,
        )
        assert sel1 is not None and sel2 is not None
        assert sel1.wire_id != sel2.wire_id

        # Click the outer end of wireA — a different cluster.
        sel_other, cycle = tester.hit_wire_vertex(
            8.5, 5.5, start_wire_id=b.id, wire_net_map=nm, cycle=cycle,
        )
        assert sel_other is not None and sel_other.wire_id == a.id

        # Back at the knot: selected wire (B) wins again.
        sel_back, cycle = tester.hit_wire_vertex(
            5.5, 5.5, start_wire_id=b.id, wire_net_map=nm, cycle=cycle,
        )
        assert sel_back is not None and sel_back.wire_id == b.id

    def test_no_selected_wire_cycles_all_candidates(
        self, three_wire_knot
    ) -> None:
        sess, (a, b, c), nm = three_wire_knot
        tester = _tester(sess)
        seen: set[uuid.UUID] = set()
        cycle = WireEditCycle()
        for _ in range(5):
            sel, cycle = tester.hit_wire_vertex(
                5.5, 5.5, start_wire_id=None, wire_net_map=nm, cycle=cycle,
            )
            assert sel is not None
            seen.add(sel.wire_id)
        assert seen == {a.id, b.id, c.id}

    def test_miss_clears_cycle_state(self, three_wire_knot) -> None:
        sess, (a, b, c), nm = three_wire_knot
        tester = _tester(sess)
        _sel, cycle = tester.hit_wire_vertex(
            5.5, 5.5, start_wire_id=b.id, wire_net_map=nm, cycle=WireEditCycle(),
        )
        assert cycle.key is not None

        sel_miss, cycle = tester.hit_wire_vertex(
            0.0, 0.0, start_wire_id=b.id, wire_net_map=nm, cycle=cycle,
        )
        assert sel_miss is None
        assert cycle.key is None
        assert cycle.last_wire_id is None

    def test_index_is_the_shared_vertex(self, three_wire_knot) -> None:
        sess, (a, b, c), nm = three_wire_knot
        sel, _cycle = _tester(sess).hit_wire_vertex(
            5.5, 5.5, start_wire_id=b.id, wire_net_map=nm, cycle=WireEditCycle(),
        )
        assert sel is not None
        # All three wires start at the shared knot → vertex index 0.
        assert sel.index == 0


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
