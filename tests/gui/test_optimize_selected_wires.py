# SPDX-License-Identifier: MIT
"""PPM "Optimize with…" action: drives WireOptimizer plugins on selected wires.

The action is exposed in the context menu when one or more wires are
selected.  It runs synchronously (the operation is small-scope), wraps
the diff in commands so the change is undoable, and never touches the
session unless the optimizer actually produced a different layout.
"""

from __future__ import annotations

import uuid
from typing import cast

import pytest

from protoboard.domain.commands.base import ComponentState
from protoboard.domain.layout.wire import Wire
from protoboard.geometry import Point
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.select_tool import SelectTool
from protoboard.services.routing.autorouter import AutorouteResult
from protoboard.services.routing.optimizer import (
    OptimizerCapabilities,
    registry as optimizer_registry,
)


def _wire(p0: Point, p1: Point, *, side: str = "top", insulated: bool = True) -> Wire:
    return Wire(
        id=uuid.uuid4(),
        path=[p0, p1],
        insulated=insulated,
        side=side,  # type: ignore[arg-type]
    )


@pytest.fixture()
def select_tool(board_view) -> SelectTool:
    tool = SelectTool()
    board_view.set_tool(tool)
    return tool


class _IdentityOptimizer:
    """Returns the input result unchanged — used to test no-op path."""

    name = "test-identity"
    display_name = "Identity (test)"

    def optimize(self, request, result, sink):
        return result

    def capabilities(self) -> OptimizerCapabilities:
        return OptimizerCapabilities()


class _ShiftSidesOptimizer:
    """Flips every wire's side from top↔bottom."""

    name = "test-shift-sides"
    display_name = "Flip side (test)"

    def optimize(self, request, result, sink):
        flipped: list[Wire] = []
        for w in result.wires:
            other = "bottom" if w.side == "top" else "top"
            flipped.append(Wire(
                id=w.id,                # preserve id so handler emits Replace, not Add
                path=list(w.path),
                insulated=w.insulated,
                side=other,             # type: ignore[arg-type]
            ))
        return AutorouteResult(wires=flipped)

    def capabilities(self) -> OptimizerCapabilities:
        return OptimizerCapabilities(rebalances_sides=True)


class _DropOneOptimizer:
    """Drops the first wire in the input.  Used to test deletion path."""

    name = "test-drop-one"
    display_name = "Drop first (test)"

    def optimize(self, request, result, sink):
        return AutorouteResult(wires=list(result.wires[1:]))

    def capabilities(self) -> OptimizerCapabilities:
        return OptimizerCapabilities()


class _AddOneOptimizer:
    """Adds a fresh wire alongside the inputs.  Tests the "new wire" branch."""

    name = "test-add-one"
    display_name = "Add one (test)"

    def optimize(self, request, result, sink):
        new = _wire(Point(3.5, 3.5), Point(3.5, 4.5), side="top")
        return AutorouteResult(wires=list(result.wires) + [new])

    def capabilities(self) -> OptimizerCapabilities:
        return OptimizerCapabilities()


class TestOptimizeSelectedWires:
    def test_identity_optimizer_is_no_op(self, session: EditorSession, select_tool: SelectTool) -> None:
        optimizer_registry.register(_IdentityOptimizer())
        try:
            w = _wire(Point(0.5, 0.5), Point(1.5, 0.5))
            session.layout.wires.append(w)
            select_tool._optimize_selected_wires({w.id}, "test-identity")
            # No commands executed → wire still present, unchanged.
            assert len(session.layout.wires) == 1
            assert session.layout.wires[0].path == [Point(0.5, 0.5), Point(1.5, 0.5)]
        finally:
            optimizer_registry.optimizers.pop("test-identity", None)

    def test_side_flip_replaces_wires_via_remove_add(
        self, session: EditorSession, select_tool: SelectTool,
    ) -> None:
        optimizer_registry.register(_ShiftSidesOptimizer())
        try:
            w_top = _wire(Point(0.5, 0.5), Point(1.5, 0.5), side="top")
            session.layout.wires.append(w_top)

            select_tool._optimize_selected_wires({w_top.id}, "test-shift-sides")

            assert len(session.layout.wires) == 1
            after = session.layout.wires[0]
            assert after.side == "bottom"
        finally:
            optimizer_registry.optimizers.pop("test-shift-sides", None)

    def test_drop_one_removes_wire(
        self, session: EditorSession, select_tool: SelectTool,
    ) -> None:
        optimizer_registry.register(_DropOneOptimizer())
        try:
            w1 = _wire(Point(0.5, 0.5), Point(1.5, 0.5))
            w2 = _wire(Point(0.5, 1.5), Point(1.5, 1.5))
            session.layout.wires.append(w1)
            session.layout.wires.append(w2)

            select_tool._optimize_selected_wires({w1.id, w2.id}, "test-drop-one")

            # The optimizer dropped the first wire of its input — but the
            # input order matches the layout's selected_wires sweep.  We
            # require exactly one remaining wire.
            assert len(session.layout.wires) == 1
        finally:
            optimizer_registry.optimizers.pop("test-drop-one", None)

    def test_add_one_creates_new_wire(
        self, session: EditorSession, select_tool: SelectTool,
    ) -> None:
        optimizer_registry.register(_AddOneOptimizer())
        try:
            w = _wire(Point(0.5, 0.5), Point(1.5, 0.5))
            session.layout.wires.append(w)

            before = len(session.layout.wires)
            select_tool._optimize_selected_wires({w.id}, "test-add-one")
            assert len(session.layout.wires) == before + 1
        finally:
            optimizer_registry.optimizers.pop("test-add-one", None)

    def test_unknown_optimizer_does_not_mutate_layout(
        self, session: EditorSession, select_tool: SelectTool,
    ) -> None:
        w = _wire(Point(0.5, 0.5), Point(1.5, 0.5))
        session.layout.wires.append(w)
        before_wires = list(session.layout.wires)
        select_tool._optimize_selected_wires({w.id}, "nonexistent-plugin")
        assert session.layout.wires == before_wires

    def test_empty_selection_does_not_crash(
        self, session: EditorSession, select_tool: SelectTool,
    ) -> None:
        optimizer_registry.register(_IdentityOptimizer())
        try:
            # Should be a silent no-op (status message only).
            select_tool._optimize_selected_wires(set(), "test-identity")
        finally:
            optimizer_registry.optimizers.pop("test-identity", None)

    def test_undo_redo_round_trip(
        self, session: EditorSession, select_tool: SelectTool,
    ) -> None:
        """The MacroCommand must allow a single undo to revert the full change."""
        optimizer_registry.register(_ShiftSidesOptimizer())
        try:
            w = _wire(Point(0.5, 0.5), Point(1.5, 0.5), side="top")
            session.layout.wires.append(w)
            select_tool._optimize_selected_wires({w.id}, "test-shift-sides")
            assert session.layout.wires[0].side == "bottom"

            session.stack.undo(cast(ComponentState, session))
            assert session.layout.wires[0].side == "top"

            session.stack.redo(cast(ComponentState, session))
            assert session.layout.wires[0].side == "bottom"
        finally:
            optimizer_registry.optimizers.pop("test-shift-sides", None)
