# SPDX-License-Identifier: MIT
"""EditController armed-transform API + snap — headless (no Qt).

Exercises the stateful transform sub-tool the controller now owns
(``begin_transform`` … ``rotate_armed`` / ``move_armed`` / ``flip_armed`` /
``set_transform_pivot`` / ``accept_transform`` / ``cancel_transform``) and the
Qt-free ``snap`` method extracted from the view. The GUI drives the SAME verbs
through a thin adapter, so this is the parity guard for that seam.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, PadLayers, SizePolicy
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import ROOT_GROUP_ID
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.geometry import Point
from protoboard.services.editing import HeadlessSession
from protoboard.services.editing.armed_transform import ArmedTransform
from protoboard.services.library.loader import load_library

_LIBRARY_DIR = Path(__file__).parent.parent / "fixtures" / "library"


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


def _place(session: HeadlessSession, x: float, y: float, refdes: str) -> uuid.UUID:
    defn = session.components["R"]
    inst = ComponentInstance.new(
        definition_ref="R", refdes=refdes, value="",
    ).bind(defn, session.package_families)
    session.instances[inst.id] = inst
    session.layout.placements[inst.id] = ComponentPlacement(
        component_id=inst.id, position=Point(x, y), rotation=0, side="top",
    )
    session.invalidate_graph()
    return inst.id


def _arm(
    session: HeadlessSession, cid: uuid.UUID, *, mode: str = "rotate"
) -> ArmedTransform:
    return session.edit_controller.begin_transform(
        frozenset({cid}), None,
        active_group_id=ROOT_GROUP_ID, mode=mode,
    )


# -- arm / rotate / accept ---------------------------------------------------


def test_begin_transform_arms_and_exposes() -> None:
    session = _session()
    cid = _place(session, 2.5, 2.5, "R1")
    armed = _arm(session, cid)
    assert session.edit_controller.armed_transform is armed
    assert armed.mode == "rotate"


def test_rotate_armed_then_accept_commits_once() -> None:
    session = _session()
    cid = _place(session, 2.5, 2.5, "R1")
    ctrl = session.edit_controller
    base = session.layout.placements[cid].rotation
    _arm(session, cid)
    ctrl.rotate_armed(90)
    assert session.layout.placements[cid].rotation == (base + 90) % 360
    ctrl.accept_transform()
    # Committed and disarmed.
    assert ctrl.armed_transform is None
    assert session.layout.placements[cid].rotation == (base + 90) % 360
    # One undo entry restores the original pose.
    session.stack.undo(session)
    assert session.layout.placements[cid].rotation == base


def test_cancel_restores_and_disarms() -> None:
    session = _session()
    cid = _place(session, 2.5, 2.5, "R1")
    ctrl = session.edit_controller
    base = session.layout.placements[cid].rotation
    _arm(session, cid)
    ctrl.rotate_armed(90)
    ctrl.cancel_transform()
    assert ctrl.armed_transform is None
    assert session.layout.placements[cid].rotation == base
    # Cancel does NOT push an undo entry.
    assert not session.stack.can_undo()


def test_move_armed_shifts_position() -> None:
    session = _session()
    cid = _place(session, 2.5, 2.5, "R1")
    ctrl = session.edit_controller
    before = session.layout.placements[cid].position
    _arm(session, cid, mode="move")
    ctrl.move_armed(1.0, 0.0)
    after = session.layout.placements[cid].position
    assert (after.x, after.y) == (before.x + 1.0, before.y)


def test_flip_armed_is_callable_while_armed() -> None:
    # NOTE: armed [F] flip is a pin-swap gated on a 2-pin footprint via
    # ``_pin_count`` (defn.footprint), which resolves to 0 for library defns
    # since defn.footprint was removed — so it is a no-op here. This asserts
    # only that the verb is wired and safe while armed (parity: same behaviour
    # as the moved FloatTransform); the dead gate is a pre-existing bug tracked
    # separately, not part of E3.
    session = _session()
    cid = _place(session, 2.5, 2.5, "R1")
    ctrl = session.edit_controller
    _arm(session, cid)
    ctrl.flip_armed()  # must not raise; leaves the armed transform live
    assert ctrl.armed_transform is not None


def test_switch_mode_preserves_arm() -> None:
    session = _session()
    cid = _place(session, 2.5, 2.5, "R1")
    ctrl = session.edit_controller
    armed = _arm(session, cid)
    ctrl.rotate_armed(90)
    ctrl.switch_transform_mode("move")
    assert ctrl.armed_transform is armed
    assert armed.mode == "move"


def test_set_transform_pivot_snaps_to_pad() -> None:
    session = _session()
    cid = _place(session, 2.5, 2.5, "R1")
    ctrl = session.edit_controller
    armed = _arm(session, cid)
    # Click near pad centre (3.5, 2.5) → shared pivot lands on the pad grid.
    ctrl.set_transform_pivot(Point(3.6, 2.4))
    assert armed.pivot is not None
    assert (armed.pivot.x, armed.pivot.y) == (3.5, 2.5)


# -- snap parity -------------------------------------------------------------


def test_snap_lands_on_pad_grid() -> None:
    session = _session()
    ctrl = session.edit_controller
    # A perfboard pad centre sits at (col+0.5, row+0.5); nearest to (3.6,3.4).
    snapped = ctrl.snap(Point(3.6, 3.4), active_group_id=ROOT_GROUP_ID)
    assert (snapped.x, snapped.y) == (3.5, 3.5)


def test_snap_off_board_falls_back_to_half_grid() -> None:
    session = _session()
    ctrl = session.edit_controller
    # Far off the 8×8 board → half-integer fallback floor(x)+0.5.
    snapped = ctrl.snap(Point(40.2, 41.8), active_group_id=ROOT_GROUP_ID)
    assert (snapped.x, snapped.y) == (40.5, 41.5)


def test_snap_prefers_component_pin_over_far_pad() -> None:
    session = _session()
    cid = _place(session, 2.5, 2.5, "R1")
    ctrl = session.edit_controller
    # Query exactly at a pin of R1 → snaps onto that pin, not a neighbouring pad.
    pins = session.iter_pin_positions(cid)
    assert pins
    _pin_num, pin_pos = pins[0]
    snapped = ctrl.snap(pin_pos, active_group_id=ROOT_GROUP_ID)
    assert (snapped.x, snapped.y) == (pin_pos.x, pin_pos.y)
