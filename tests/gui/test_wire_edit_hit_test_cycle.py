# SPDX-License-Identifier: MIT
"""Hit-test cycle behaviour in wire-edit mode.

When the user clicks a knot where N wires of the same net meet at the same
world coordinate, the hit-test must:

1. Return the currently-selected wire on the first click (priority).
2. Rotate through the remaining sibling wires on subsequent clicks at the
   same cluster (round-robin).
3. Reset the cycle when the click moves to a different cluster.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from PySide6.QtCore import QPointF
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
from protoboard.gui.board_view import BoardView
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.select_tool import SelectTool
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
def three_wire_knot(qtbot: QtBot) -> tuple[EditorSession, SelectTool, list[Wire]]:
    """Three wires of one net meeting at the same shared pad (5.5, 5.5).

    Layout:

        wireA: (5.5,5.5) → (8.5,5.5)        — east leg
        wireB: (5.5,5.5) → (5.5,8.5)        — south leg
        wireC: (5.5,5.5) → (2.5,5.5)        — west leg

    All are bare, on the bottom side, joined on a single shared pad.  A
    real 1-pin component is placed at the shared pad and bound to a Net
    so `_build_wire_net_map` resolves all three wires to the same net.
    """
    library = load_library([_LIBRARY_DIR])
    sess = EditorSession(board=_perfboard(), library=library)

    # Place a 1-pin component at the shared pad so the wires resolve to a
    # real net via the connectivity graph.
    defn = ComponentDefinition(
        name="STUB1", footprint=SIL(1), pin_names={1: "P1"},
    )
    sess.components["STUB1"] = defn
    inst = ComponentInstance.new(
        definition_ref="STUB1", refdes="X1", value="",
    )
    sess.instances[inst.id] = inst
    sess.layout.placements[inst.id] = ComponentPlacement(
        component_id=inst.id,
        position=Point(5.5, 5.5),
        rotation=0,
        side="top",
    )
    net_id = uuid.uuid4()
    sess.schematic.nets[net_id] = Net(
        id=net_id,
        name="N1",
        pins={PinRef(component_id=inst.id, pin_number=1)},
    )

    wireA = Wire(
        id=uuid.uuid4(),
        path=[Point(5.5, 5.5), Point(8.5, 5.5)],
        insulated=False, side="bottom",
    )
    wireB = Wire(
        id=uuid.uuid4(),
        path=[Point(5.5, 5.5), Point(5.5, 8.5)],
        insulated=False, side="bottom",
    )
    wireC = Wire(
        id=uuid.uuid4(),
        path=[Point(5.5, 5.5), Point(2.5, 5.5)],
        insulated=False, side="bottom",
    )
    sess.layout.wires.extend([wireA, wireB, wireC])
    sess.invalidate_graph()

    view = BoardView(sess)
    tool = SelectTool()
    view.set_tool(tool)

    return sess, tool, [wireA, wireB, wireC]


class TestSharedKnotHitTest:
    def test_selected_wire_wins_first_click(self, three_wire_knot) -> None:
        sess, tool, (a, b, c) = three_wire_knot
        # Mimic having wireB selected and wire-edit armed (no start yet).
        tool._selected_wire_id = b.id
        tool._edit_start_wire_id = b.id
        tool._edit_start_idx = None

        hit_wire, hit_idx = tool._hit_test_wire_node_any(QPointF(5.5, 5.5))
        assert hit_wire is not None and hit_idx is not None
        assert hit_wire.id == b.id, (
            "selected wire must win when click lands on a shared knot"
        )

    def test_rotation_through_siblings_on_repeated_clicks(
        self, three_wire_knot
    ) -> None:
        sess, tool, (a, b, c) = three_wire_knot
        tool._selected_wire_id = b.id
        tool._edit_start_wire_id = b.id
        tool._edit_start_idx = None

        seen: list[uuid.UUID] = []
        for _ in range(5):
            w, _i = tool._hit_test_wire_node_any(QPointF(5.5, 5.5))
            assert w is not None
            seen.append(w.id)

        # First click: selected wire (B).
        assert seen[0] == b.id
        # All three siblings must appear before repetition.
        cycle_unique: list[uuid.UUID] = []
        for wid in seen:
            if wid not in cycle_unique:
                cycle_unique.append(wid)
        assert set(cycle_unique) == {a.id, b.id, c.id}, (
            f"all three siblings must appear in the rotation, got {cycle_unique}"
        )
        # The cycle must wrap: after three distinct wires, the 4th click
        # returns to the first.
        assert seen[3] == seen[0], "rotation must wrap after exhausting siblings"

    def test_cluster_change_resets_cycle(self, three_wire_knot) -> None:
        sess, tool, (a, b, c) = three_wire_knot
        tool._selected_wire_id = b.id
        tool._edit_start_wire_id = b.id
        tool._edit_start_idx = None

        # First two clicks at the shared knot advance the cycle.
        w1, _ = tool._hit_test_wire_node_any(QPointF(5.5, 5.5))
        w2, _ = tool._hit_test_wire_node_any(QPointF(5.5, 5.5))
        assert w1 is not None and w2 is not None
        assert w1.id != w2.id, "second click must rotate to a different wire"

        # Now click somewhere else (the outer end of wireA at 8.5, 5.5).
        w_other, _ = tool._hit_test_wire_node_any(QPointF(8.5, 5.5))
        assert w_other is not None and w_other.id == a.id

        # Returning to the shared knot starts the cycle again — selected
        # wire (B) wins, regardless of where we were mid-cycle before.
        w_back, _ = tool._hit_test_wire_node_any(QPointF(5.5, 5.5))
        assert w_back is not None and w_back.id == b.id

    def test_no_selected_wire_cycles_all_candidates(
        self, three_wire_knot
    ) -> None:
        sess, tool, (a, b, c) = three_wire_knot
        # No wire selected, no start armed.  The hit-test still considers
        # every wire on the resolved net (no restrict_to_start_wire).
        tool._selected_wire_id = None
        tool._edit_start_wire_id = None
        tool._edit_start_idx = None

        seen: set[uuid.UUID] = set()
        for _ in range(5):
            w, _ = tool._hit_test_wire_node_any(QPointF(5.5, 5.5))
            assert w is not None
            seen.add(w.id)
        assert seen == {a.id, b.id, c.id}, (
            "with no selection, all three must still cycle through"
        )

    def test_miss_clears_cycle_state(self, three_wire_knot) -> None:
        sess, tool, (a, b, c) = three_wire_knot
        tool._selected_wire_id = b.id
        tool._edit_start_wire_id = b.id

        _ = tool._hit_test_wire_node_any(QPointF(5.5, 5.5))
        assert tool._wire_edit_cycle_key is not None

        # Click somewhere far from any node — must reset cycle state.
        miss_wire, miss_idx = tool._hit_test_wire_node_any(QPointF(0.0, 0.0))
        assert miss_wire is None and miss_idx is None
        assert tool._wire_edit_cycle_key is None
        assert tool._wire_edit_cycle_last_wire_id is None

    def test_cancel_wire_edit_clears_cycle_state(
        self, three_wire_knot
    ) -> None:
        sess, tool, (a, b, c) = three_wire_knot
        tool._selected_wire_id = b.id
        tool._edit_start_wire_id = b.id
        _ = tool._hit_test_wire_node_any(QPointF(5.5, 5.5))
        assert tool._wire_edit_cycle_key is not None

        tool._cancel_wire_edit()
        assert tool._wire_edit_cycle_key is None
        assert tool._wire_edit_cycle_last_wire_id is None
