# SPDX-License-Identifier: MIT
"""EditController wire/lock verbs unify the multi-entry ops (one code path)."""
import uuid

from protoboard.domain.layout.wire import Fragment, Wire
from protoboard.geometry import Point

# Reuse the session-building helper from the sibling add_wire test.
from tests.unit.test_edit_controller_add_wire import _place, _session


def _add_mixed_wire(session) -> uuid.UUID:  # type: ignore[no-untyped-def]
    """Build a two-fragment wire (top then bottom) and insert it directly.

    The public ``add_wire`` verb only builds single-side wires; a mixed wire
    is constructed directly and appended to ``session.layout.wires`` per the
    task brief's fallback (building one via the public API is not supported).
    """
    wire = Wire(
        id=uuid.uuid4(),
        insulated=True,
        fragments=[
            Fragment(path=[Point(1.0, 1.0), Point(2.0, 1.0)], side="top"),
            Fragment(path=[Point(2.0, 1.0), Point(3.0, 1.0)], side="bottom"),
        ],
    )
    session.layout.wires.append(wire)
    return wire.id


def test_flip_wires_mixed_stays_mixed() -> None:
    session = _session()
    wid = _add_mixed_wire(session)
    before = [f.side for f in session.layout.wires.get_or_raise(wid).fragments]
    session.edit_controller.flip_wires([wid])
    after = [f.side for f in session.layout.wires.get_or_raise(wid).fragments]
    assert after == ["bottom" if s == "top" else "top" for s in before]


def test_set_wires_side_flattens() -> None:
    session = _session()
    wid = _add_mixed_wire(session)
    session.edit_controller.set_wires_side([wid], side="top")
    sides = {f.side for f in session.layout.wires.get_or_raise(wid).fragments}
    assert sides == {"top"}


def test_set_wires_insulated() -> None:
    session = _session()
    wid = _add_mixed_wire(session)
    session.edit_controller.set_wires_insulated([wid], insulated=False)
    assert session.layout.wires.get_or_raise(wid).insulated is False


def test_set_locked_locks_component() -> None:
    session = _session()
    cid = _place(session, 2.0, 2.0, "R1")
    session.edit_controller.set_locked([cid], locked=True)
    assert session.layout.placements[cid].locked is True
