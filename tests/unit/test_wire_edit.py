# SPDX-License-Identifier: MIT
"""Tests for ReplaceWireSegmentCommand and _dedupe_adjacent helper."""

from __future__ import annotations

import uuid

import pytest

from protoboard.domain.commands.component_cmds import ReplaceWireSegmentCommand
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.wire import WireSegment
from protoboard.geometry import Point
from protoboard.gui.tools.select_tool import _dedupe_adjacent


# ---------------------------------------------------------------------------
# Minimal state stub
# ---------------------------------------------------------------------------


class _State:
    def __init__(self, wires: list[WireSegment]) -> None:
        self.layout = Layout(wires=wires)


def _wire(path: list[Point]) -> WireSegment:
    return WireSegment(id=uuid.uuid4(), path=path, insulated=True, side="top")


# ---------------------------------------------------------------------------
# ReplaceWireSegmentCommand
# ---------------------------------------------------------------------------


class TestReplaceWireSegmentCommand:
    def test_apply_replaces_path(self) -> None:
        w = _wire([Point(0, 0), Point(1, 0), Point(2, 0)])
        state = _State([w])
        new_path = [Point(0, 0), Point(0, 1), Point(2, 0)]
        cmd = ReplaceWireSegmentCommand(wire_id=w.id, new_path=new_path)
        cmd.apply(state)
        assert state.layout.wires[0].path == new_path

    def test_revert_restores_original_path(self) -> None:
        original = [Point(0, 0), Point(1, 0), Point(2, 0)]
        w = _wire(original)
        state = _State([w])
        cmd = ReplaceWireSegmentCommand(
            wire_id=w.id,
            new_path=[Point(0, 0), Point(0, 1), Point(2, 0)],
        )
        cmd.apply(state)
        cmd.revert(state)
        assert state.layout.wires[0].path == original

    def test_apply_raises_if_wire_not_found(self) -> None:
        state = _State([])
        cmd = ReplaceWireSegmentCommand(
            wire_id=uuid.uuid4(),
            new_path=[Point(0, 0), Point(1, 0)],
        )
        with pytest.raises(KeyError):
            cmd.apply(state)

    def test_undo_redo_cycle(self) -> None:
        original = [Point(0, 0), Point(5, 0)]
        w = _wire(original)
        state = _State([w])
        new_path = [Point(0, 0), Point(0, 3), Point(5, 3), Point(5, 0)]
        cmd = ReplaceWireSegmentCommand(wire_id=w.id, new_path=new_path)
        cmd.apply(state)
        assert state.layout.wires[0].path == new_path
        cmd.revert(state)
        assert state.layout.wires[0].path == original
        cmd.apply(state)
        assert state.layout.wires[0].path == new_path

    def test_wire_id_unchanged_after_replace(self) -> None:
        w = _wire([Point(0, 0), Point(1, 0)])
        state = _State([w])
        cmd = ReplaceWireSegmentCommand(
            wire_id=w.id, new_path=[Point(0, 0), Point(0, 1), Point(1, 0)]
        )
        cmd.apply(state)
        assert state.layout.wires[0].id == w.id

    def test_description(self) -> None:
        cmd = ReplaceWireSegmentCommand(
            wire_id=uuid.uuid4(), new_path=[Point(0, 0), Point(1, 0)]
        )
        assert "wire" in cmd.description.lower()


# ---------------------------------------------------------------------------
# _dedupe_adjacent
# ---------------------------------------------------------------------------


class TestDedupeAdjacent:
    def test_no_duplicates_unchanged(self) -> None:
        pts = [Point(0, 0), Point(1, 0), Point(2, 0)]
        assert _dedupe_adjacent(pts) == pts

    def test_removes_consecutive_duplicate(self) -> None:
        pts = [Point(0, 0), Point(1, 0), Point(1, 0), Point(2, 0)]
        assert _dedupe_adjacent(pts) == [Point(0, 0), Point(1, 0), Point(2, 0)]

    def test_empty_list(self) -> None:
        assert _dedupe_adjacent([]) == []

    def test_single_point(self) -> None:
        pts = [Point(3, 4)]
        assert _dedupe_adjacent(pts) == pts

    def test_all_same(self) -> None:
        pts = [Point(1, 1), Point(1, 1), Point(1, 1)]
        assert _dedupe_adjacent(pts) == [Point(1, 1)]
