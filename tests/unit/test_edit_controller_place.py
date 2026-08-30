# SPDX-License-Identifier: MIT
"""EditController.resolve_placement / place_component — headless (no Qt).

The place verb is split: resolve_placement builds the instance + placement and
runs conflict resolution (returning a ResolvedPlacement or None); place_component
commits it. Both exercised directly on a :class:`HeadlessSession` with canned
placement prompts. The GUI floating / auto-accept UX stays in PlaceTool, whose
tests keep asserting the same behaviour through the tool.
"""

from __future__ import annotations

from pathlib import Path

from protoboard.controller.editing import HeadlessSession
from protoboard.controller.editing.placement_conflict import PlacementPrompts
from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, PadLayers, SizePolicy
from protoboard.geometry import Point
from protoboard.services.library.loader import load_library

_LIBRARY_DIR = Path(__file__).parent.parent / "fixtures" / "library"

# A prompt that never accepts a shared-pad conflict and cancels wire conflicts —
# enough for conflict-free placements and for asserting a block.
_DECLINE = PlacementPrompts(pad=lambda _req: False, wire=lambda _req: None)
# A prompt that accepts shared-pad conflicts.
_ACCEPT = PlacementPrompts(pad=lambda _req: True, wire=lambda _req: None)


def _perfboard(rows: int = 8, cols: int = 8) -> Board:
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


def _session() -> HeadlessSession:
    library = load_library([_LIBRARY_DIR])
    return HeadlessSession(board=_perfboard(), library=library)


def test_resolve_placement_mints_refdes_and_binds() -> None:
    session = _session()
    resolved = session.edit_controller.resolve_placement(
        "R",
        position=Point(2.0, 2.0),
        prompts=_DECLINE,
    )
    assert resolved is not None
    assert resolved.instance.refdes == "R1"
    assert resolved.instance.definition_ref == "R"
    # resolve_placement does not commit — nothing entered the layout yet.
    assert resolved.instance.id not in session.layout.placements


def test_place_component_commits_and_undoes() -> None:
    session = _session()
    resolved = session.edit_controller.resolve_placement(
        "R",
        position=Point(2.0, 2.0),
        prompts=_DECLINE,
    )
    assert resolved is not None
    cid = session.edit_controller.place_component(resolved)
    assert cid in session.layout.placements
    assert cid in session.instances

    session.stack.undo(session)  # type: ignore[arg-type]
    assert cid not in session.layout.placements


def test_refdes_increments_across_placements() -> None:
    session = _session()
    r1 = session.edit_controller.resolve_placement(
        "R",
        position=Point(1.0, 1.0),
        prompts=_DECLINE,
    )
    assert r1 is not None
    session.edit_controller.place_component(r1)
    r2 = session.edit_controller.resolve_placement(
        "R",
        position=Point(4.0, 1.0),
        prompts=_DECLINE,
    )
    assert r2 is not None
    assert (r1.instance.refdes, r2.instance.refdes) == ("R1", "R2")


def test_body_overlap_is_blocked() -> None:
    """A second component whose body overlaps the first (no coincident pin)
    is hard-blocked — pure body-on-body overlap, no prompt shown."""
    session = _session()
    r1 = session.edit_controller.resolve_placement(
        "R",
        position=Point(3.0, 3.0),
        prompts=_ACCEPT,
    )
    assert r1 is not None
    session.edit_controller.place_component(r1)
    # Offset by half a cell: bodies overlap but no pin coincides → block, even
    # with an accept-everything prompt.
    blocked = session.edit_controller.resolve_placement(
        "R",
        position=Point(3.0, 3.5),
        prompts=_ACCEPT,
    )
    assert blocked is None


def test_existing_instance_identity_is_reused() -> None:
    """A netlist-imported instance keeps its id/refdes/value."""
    from protoboard.domain.components.instance import ComponentInstance

    session = _session()
    imported = ComponentInstance.new(
        definition_ref="R",
        refdes="R42",
        value="10k",
    )
    resolved = session.edit_controller.resolve_placement(
        "R",
        position=Point(2.0, 2.0),
        existing_instance=imported,
        prompts=_DECLINE,
    )
    assert resolved is not None
    assert resolved.instance.id == imported.id
    assert resolved.instance.refdes == "R42"
    assert resolved.instance.value == "10k"
