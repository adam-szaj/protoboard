# SPDX-License-Identifier: MIT
"""FlipWireSideCommand toggles each fragment's side (mixed wire stays mixed)."""
import uuid

from protoboard.domain.commands.component_cmds import FlipWireSideCommand
from protoboard.domain.layout.wire import Fragment, Wire, WireCollection
from protoboard.geometry import Point


class _State:
    """Minimal ComponentState stand-in: only .layout.wires is touched."""

    class _Layout:
        def __init__(self, wires: WireCollection) -> None:
            self.wires = wires

    def __init__(self, wires: WireCollection) -> None:
        self.layout = _State._Layout(wires)


def _mixed_wire() -> Wire:
    return Wire(
        id=uuid.uuid4(),
        fragments=[
            Fragment(path=[Point(0, 0), Point(1, 0)], side="top"),
            Fragment(path=[Point(1, 0), Point(2, 0)], side="bottom"),
            Fragment(path=[Point(2, 0), Point(3, 0)], side="top"),
        ],
        insulated=True,
    )


def test_flip_mixed_wire_toggles_each_fragment() -> None:
    w = _mixed_wire()
    state = _State(WireCollection([w]))
    FlipWireSideCommand(wire_id=w.id).apply(state)  # type: ignore[arg-type]
    assert [f.side for f in w.fragments] == ["bottom", "top", "bottom"]


def test_flip_revert_restores_original_sides() -> None:
    w = _mixed_wire()
    state = _State(WireCollection([w]))
    cmd = FlipWireSideCommand(wire_id=w.id)
    cmd.apply(state)  # type: ignore[arg-type]
    cmd.revert(state)  # type: ignore[arg-type]
    assert [f.side for f in w.fragments] == ["top", "bottom", "top"]


def test_flip_uniform_wire() -> None:
    w = Wire(
        id=uuid.uuid4(),
        fragments=[Fragment(path=[Point(0, 0), Point(1, 0)], side="top")],
        insulated=False,
    )
    state = _State(WireCollection([w]))
    FlipWireSideCommand(wire_id=w.id).apply(state)  # type: ignore[arg-type]
    assert [f.side for f in w.fragments] == ["bottom"]
    assert w.side == "bottom"


def test_flip_missing_wire_raises_keyerror() -> None:
    state = _State(WireCollection([]))
    import pytest

    with pytest.raises(KeyError):
        FlipWireSideCommand(wire_id=uuid.uuid4()).apply(state)  # type: ignore[arg-type]
