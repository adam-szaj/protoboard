# SPDX-License-Identifier: MIT
"""ReplaceWireFragmentsCommand must enrich insulated paths with pad-nodes.

Regression test for the bug where simplify-after-manual-edit converted an
off-pad diagonal into an on-pad ortho L-bend, *increasing* on-pad crossings
in violation of the simplify "M ≤ N" rule.  Root cause: the manual edit
path arrived from ``_commit_wire_edit`` with only user-clicked vertices,
so PadIndex never recorded ``InsulatedWireCrossing`` for the intermediate
pads the diagonal physically crosses.  Simplify then saw zero pad contacts
on the baseline and was free to add some.  Save+load fixed it because
``load_project`` runs the same enrichment.

ReplaceWireFragmentsCommand.apply now enriches insulated fragments through
``_enrich_insulated_fragments_with_pad_nodes`` so behaviour is consistent
regardless of how the path reached the layout.
"""

from __future__ import annotations

import uuid

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.commands.component_cmds import (
    ReplaceWireFragmentsCommand,
    _enrich_insulated_fragments_with_pad_nodes,
)
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.wire import Fragment, Wire
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point


def _perfboard(rows: int, cols: int) -> Board:
    return Board(root=BoardGrid(
        width_policy=SizePolicy.HUG, height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG, height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        ),
        rows=rows, cols=cols, row_gap=0, col_gap=0,
    ))


class _State:
    """Minimal ComponentState for command.apply()/revert()."""

    def __init__(self, board: Board, layout: Layout) -> None:
        self.board = board
        self.layout = layout
        self.schematic = Schematic(nets={})
        self.components: dict[str, ComponentDefinition] = {}
        self.instances: dict[uuid.UUID, ComponentInstance] = {}


class TestEnrichFragmentsHelper:
    def test_diagonal_path_gets_pad_intermediate(self) -> None:
        # 4x4 board; diagonal from (0.5,0.5) to (3.5,3.5) crosses
        # (1.5,1.5), (2.5,2.5) — two intermediate pads.
        board = _perfboard(4, 4)
        frag = Fragment(
            path=[Point(0.5, 0.5), Point(3.5, 3.5)],
            side="top",
        )
        enriched = _enrich_insulated_fragments_with_pad_nodes(
            (frag,), is_insulated=True, board=board,
        )
        assert len(enriched) == 1
        new_path = enriched[0].path
        # Original endpoints + 2 intermediate pads = 4 vertices.
        assert len(new_path) == 4
        assert new_path[0] == Point(0.5, 0.5)
        assert new_path[-1] == Point(3.5, 3.5)
        # Middle vertices land on pad centres.
        middles = {(p.x, p.y) for p in new_path[1:-1]}
        assert (1.5, 1.5) in middles
        assert (2.5, 2.5) in middles

    def test_bare_wire_unchanged(self) -> None:
        board = _perfboard(4, 4)
        frag = Fragment(
            path=[Point(0.5, 0.5), Point(3.5, 3.5)],
            side="top",
        )
        # Bare wires get pad-vertices from their callers, not here.
        out = _enrich_insulated_fragments_with_pad_nodes(
            (frag,), is_insulated=False, board=board,
        )
        assert out is (frag,) or out == (frag,)

    def test_already_enriched_returned_as_is(self) -> None:
        board = _perfboard(4, 4)
        frag = Fragment(
            path=[Point(0.5, 0.5), Point(1.5, 1.5),
                  Point(2.5, 2.5), Point(3.5, 3.5)],
            side="top",
        )
        out = _enrich_insulated_fragments_with_pad_nodes(
            (frag,), is_insulated=True, board=board,
        )
        # No new pads to insert → original tuple returned.
        assert out == (frag,)


class TestReplaceWireFragmentsApply:
    def test_apply_enriches_insulated_fragment_path(self) -> None:
        """After apply, the wire stored in layout has pad-intermediate vertices."""
        board = _perfboard(4, 4)
        # Seed a wire with a short straight path; apply a fragment swap
        # that introduces a diagonal needing enrichment.
        original_wire = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(1.5, 0.5)],
            insulated=True, side="top",
        )
        layout = Layout()
        layout.ensure_root_group()
        layout.wires.append(original_wire)
        layout.ensure_wire_knots(original_wire)
        state = _State(board, layout)

        new_fragment = Fragment(
            path=[Point(0.5, 0.5), Point(3.5, 3.5)],
            side="top",
        )
        cmd = ReplaceWireFragmentsCommand(
            wire_id=original_wire.id,
            new_fragments=(new_fragment,),
        )
        cmd.apply(state)  # type: ignore[arg-type]

        wire_after = layout.wires.get(original_wire.id)
        assert wire_after is not None
        # Path enriched: 4 vertices (endpoints + 2 intermediate pads).
        assert len(wire_after.path) == 4
        middles = {(p.x, p.y) for p in wire_after.path[1:-1]}
        assert (1.5, 1.5) in middles
        assert (2.5, 2.5) in middles

    def test_revert_restores_original_fragments(self) -> None:
        """Revert must restore the user's pre-apply fragments, not the
        enriched form — otherwise repeated apply/revert cycles would
        accumulate pad-nodes."""
        board = _perfboard(4, 4)
        original_fragment = Fragment(
            path=[Point(0.5, 0.5), Point(1.5, 0.5)],
            side="top",
        )
        wire = Wire(
            id=uuid.uuid4(),
            fragments=[original_fragment],
            insulated=True, side="top",
        )
        layout = Layout()
        layout.ensure_root_group()
        layout.wires.append(wire)
        layout.ensure_wire_knots(wire)
        state = _State(board, layout)

        new_fragment = Fragment(
            path=[Point(0.5, 0.5), Point(3.5, 3.5)],
            side="top",
        )
        cmd = ReplaceWireFragmentsCommand(
            wire_id=wire.id,
            new_fragments=(new_fragment,),
        )
        cmd.apply(state)  # type: ignore[arg-type]
        cmd.revert(state)  # type: ignore[arg-type]

        wire_after = layout.wires.get(wire.id)
        assert wire_after is not None
        # Back to the original 2-point path, not the enriched one.
        assert len(wire_after.path) == 2
        assert wire_after.path[0] == Point(0.5, 0.5)
        assert wire_after.path[1] == Point(1.5, 0.5)
