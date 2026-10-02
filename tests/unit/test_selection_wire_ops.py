# SPDX-License-Identifier: MIT
"""Selection wire-operation preparation preserves command and snapshot semantics."""

from __future__ import annotations

import uuid
from copy import deepcopy
from types import SimpleNamespace
from typing import cast

import pytest

from protoboard.controller.editing.contexts import EditingReadContext
from protoboard.controller.ports.routing import SimplifyRequest
from protoboard.controller.selection.wire_ops import (
    build_merge_commands,
    build_optimizer_commands,
    build_simplify_commands,
    build_unweave_commands,
    prepare_simplify_worker,
)
from protoboard.domain.commands.base import MacroCommand
from protoboard.domain.commands.component_cmds import (
    AddWireCommand,
    FuseTouchingWiresCommand,
    RemoveWireCommand,
    ReplaceWireFragmentsCommand,
)
from protoboard.domain.layout.wire import Fragment, Wire
from protoboard.domain.routing.simplify_mode import SimplifyMode
from protoboard.geometry import Point
from protoboard.gui.session import EditorSession


def _wire(*points: tuple[float, float]) -> Wire:
    return Wire(
        id=uuid.uuid4(),
        path=[Point(x, y) for x, y in points],
        side="top",
        insulated=True,
    )


def test_simplify_worker_captures_inputs_and_reports_missing_wire_progress(
    session: EditorSession,
) -> None:
    target = _wire((0.5, 0.5), (0.5, 2.5), (2.5, 2.5))
    other = _wire((3.5, 0.5), (3.5, 1.5))
    session.layout.wires.extend([target, other])
    requests: list[SimplifyRequest] = []

    def simplify(request: SimplifyRequest) -> Wire:
        requests.append(request)
        return Wire(id=request.wire.id, path=[target.path[0], target.path[-1]])

    reads = SimpleNamespace(
        board=session.board,
        layout=session.layout,
        schematic=session.schematic,
        iter_pin_positions=session.iter_pin_positions,
        get_pin_position=session.get_pin_position,
        cached_wire_net_map=session.cached_wire_net_map,
        pad_index=session.pad_index,
    )
    assert not hasattr(reads, "edit_controller")
    worker = prepare_simplify_worker(
        cast(EditingReadContext, reads),
        [target.id, uuid.uuid4()],
        SimplifyMode.CHEBYSHEV,
        simplify_wire=simplify,
    )
    session.layout.wires.append(_wire((4.5, 0.5), (4.5, 1.5)))
    progress: list[tuple[int, int]] = []
    changes = worker(lambda current, total: progress.append((current, total)))

    assert progress == [(1, 2), (2, 2)]
    assert requests[0].other_wires == (other,)
    assert requests[0].mode is SimplifyMode.CHEBYSHEV
    assert changes == [(target.id, [target.path[0], target.path[-1]], target.path)]
    assert changes[0][2] is not target.path


def test_simplify_commands_skip_missing_wire_and_restore_original_path(
    session: EditorSession,
) -> None:
    wire = _wire((0.5, 0.5), (0.5, 2.5), (2.5, 2.5))
    session.layout.wires.append(wire)
    original = list(wire.path)
    shortened = [original[0], original[-1]]
    enriched = [original[0], Point(1.5, 1.5), original[-1]]
    commands = build_simplify_commands(
        session.layout,
        [(wire.id, shortened, original), (uuid.uuid4(), shortened, original)],
    )
    assert len(commands) == 1
    assert session.layout.wires[0].path == original

    session.stack.execute(MacroCommand(commands), session)
    assert session.layout.wires[0].id == wire.id
    assert session.layout.wires[0].path == enriched
    session.stack.undo(session)
    assert session.layout.wires[0].path == original
    session.stack.redo(session)
    assert session.layout.wires[0].path == enriched


def test_unweave_no_op_ignores_new_identity_but_replacement_removes_before_adding(
    session: EditorSession,
) -> None:
    first = _wire((0.5, 0.5), (1.5, 0.5))
    second = _wire((2.5, 0.5), (3.5, 0.5))
    targets = [first, second]
    same_geometry = [deepcopy(first), deepcopy(second)]
    for wire in same_geometry:
        wire.id = uuid.uuid4()
    assert build_unweave_commands(targets, same_geometry) == []
    output = _wire((0.5, 0.5), (3.5, 0.5))
    commands = build_unweave_commands(targets, [output])
    assert [type(c) for c in commands] == [
        RemoveWireCommand,
        RemoveWireCommand,
        AddWireCommand,
    ]
    session.layout.wires.extend(targets)
    session.stack.execute(MacroCommand(commands), session)
    assert [w.id for w in session.layout.wires] == [output.id]
    session.stack.undo(session)
    assert {w.id for w in session.layout.wires} == {first.id, second.id}
    session.stack.redo(session)
    assert [w.id for w in session.layout.wires] == [output.id]


def test_optimizer_diff_keeps_unchanged_identity_and_orders_replacements() -> None:
    unchanged = _wire((0.5, 0.5), (1.5, 0.5))
    changed = _wire((0.5, 1.5), (1.5, 1.5))
    deleted = _wire((0.5, 2.5), (1.5, 2.5))
    added = _wire((0.5, 3.5), (1.5, 3.5))
    replacement = deepcopy(changed)
    replacement.side = "bottom"
    commands = build_optimizer_commands(
        [unchanged, changed, deleted], [deepcopy(unchanged), replacement, added]
    )
    assert [type(c) for c in commands] == [
        RemoveWireCommand,
        AddWireCommand,
        RemoveWireCommand,
        AddWireCommand,
    ]
    assert commands[0].wire_id == changed.id
    assert commands[1].wire is replacement
    assert commands[2].wire_id == deleted.id
    assert commands[3].wire is added
    assert build_optimizer_commands([unchanged], [deepcopy(unchanged)]) == []


@pytest.mark.parametrize("whole_layout", [False, True])
def test_merge_plan_restores_layout_then_fuses_and_collapses_atomically(
    session: EditorSession, whole_layout: bool
) -> None:
    first = _wire((0.5, 0.5), (1.5, 0.5), (2.5, 0.5))
    first.fragments = [
        Fragment(path=first.path[:2], side="top"),
        Fragment(path=first.path[1:], side="top"),
    ]
    second = _wire((2.5, 0.5), (3.5, 0.5))
    for wire in (first, second):
        session.layout.ensure_wire_knots(wire)
        session.layout.add_wire(wire)
    before = deepcopy(session.layout.knots)
    ids = frozenset({first.id, second.id})

    commands, fused, collapsed, removed = build_merge_commands(
        session, None if whole_layout else ids
    )
    assert (fused, collapsed, removed) == (1, 1, 1)
    assert {w.id for w in session.layout.wires} == ids
    assert session.layout.knots == before
    assert not session.stack.can_undo()
    assert [type(c) for c in commands] == [
        FuseTouchingWiresCommand,
        ReplaceWireFragmentsCommand,
    ]

    session.stack.execute(MacroCommand(commands), session)
    assert len(session.layout.wires) == 1
    merged = session.layout.wires[0]
    assert merged.id == min(ids)
    assert len(merged.fragments) == 1
    session.stack.undo(session)
    assert {w.id for w in session.layout.wires} == ids
    assert session.layout.knots == before
    assert len(session.layout.wires[first.id].fragments) == 2
    assert not session.stack.can_undo()
    session.stack.redo(session)
    assert len(session.layout.wires) == 1
    assert session.layout.wires[0].id == merged.id
    assert len(session.layout.wires[0].fragments) == 1


def test_merge_selection_does_not_collapse_unselected_fragments(
    session: EditorSession,
) -> None:
    wire = _wire((0.5, 0.5), (1.5, 0.5), (2.5, 0.5))
    wire.fragments = [
        Fragment(path=wire.path[:2], side="top"),
        Fragment(path=wire.path[1:], side="top"),
    ]
    session.layout.ensure_wire_knots(wire)
    session.layout.add_wire(wire)
    assert build_merge_commands(session, frozenset()) == ([], 0, 0, 0)
    assert len(session.layout.wires[wire.id].fragments) == 2
