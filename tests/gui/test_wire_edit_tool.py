# SPDX-License-Identifier: MIT
"""Tests for the extracted :class:`WireEditTool` (Task 8a).

Drives the NEW WireEditTool directly (constructed with a WireRange over a wire
built through the controller so it carries real endpoint knots — required by
FlipWireFragmentCommand).  Asserts the stays-armed model:

* each id-preserving op ([A]/[F]/[S]) leaves the tool ARMED (re-armed on the
  full span of the mutated wire) — the stays-armed proof;
* delete EXITS the tool (return_to fired / active_tool back to select);
* Esc mid-reroute keeps the fragment;
* the tool conforms to :class:`ArmedSubTool`;
* two consecutive ops both execute without a re-arm round-trip.

These are NEW asserts on the NEW tool; they do NOT touch
``TestWireEditExitParity`` in test_armed_subtool_lifecycle.py, which still guards
SelectTool's CURRENT (pre-swap) behaviour and must stay green (8b flips it).
"""
from __future__ import annotations

from protoboard.controller.selection.wire_edit import WireRange
from protoboard.controller.tools.armed_subtool import ArmedSubTool
from protoboard.domain.layout.wire import Wire
from protoboard.geometry import Point
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.gui_tools import SelectTool, WireEditTool
from tests.fakes.board_view import HeadlessBoardView


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _headless(session: EditorSession) -> HeadlessBoardView:
    return HeadlessBoardView(session)


def _detour_wire(session: EditorSession) -> Wire:
    # Orthogonal L on the top layer: simplify corner-cuts it. Routed through the
    # controller (not a raw layout.wires.append) so the wire gets real endpoint
    # knots -- required by FlipWireFragmentCommand.  Mirrors
    # test_armed_subtool_lifecycle.py::_detour_wire.
    wire_id = session.edit_controller.add_wire(
        [Point(1.5, 1.5), Point(2.5, 1.5), Point(3.5, 1.5),
         Point(3.5, 2.5), Point(3.5, 3.5)],
        insulated=True, side="top",
    )
    assert wire_id is not None
    wire = session.layout.wires.get(wire_id)
    assert wire is not None
    return wire


def _knotless_wire(session: EditorSession) -> Wire:
    """An L-shaped wire inserted DIRECTLY into ``layout.wires`` -- so it carries
    NO endpoint knots (``a``/``b`` are ``None``), exactly like a wire loaded
    from a saved project / fixture that never went through ``add_wire``.  A
    fragment op (flip / simplify) via ``replace_wire_fragments`` requires both
    knots, so the tool must backfill them on arm."""
    import uuid

    wire = Wire(
        id=uuid.uuid4(),
        path=[Point(1.5, 1.5), Point(2.5, 1.5), Point(3.5, 1.5),
              Point(3.5, 2.5), Point(3.5, 3.5)],
        insulated=True, side="top",
    )
    session.layout.wires[wire.id] = wire
    session.invalidate_graph()
    session.layout_changed.emit()
    assert wire.a is None and wire.b is None  # the knot-less precondition
    return wire


def _arm(
    view: HeadlessBoardView, wire: Wire
) -> WireEditTool:
    """Set a plain SelectTool as the return target, then arm a full-span
    WireEditTool on *wire* through the ToolHost (so activate() runs)."""
    select = SelectTool()
    view.tools.set_tool(select)
    tool = WireEditTool(
        WireRange.full_span(wire),
        wire.id,
        return_to=lambda: view.tools.set_tool(select),
    )
    view.tools.set_tool(tool)
    return tool


# --------------------------------------------------------------------------- #
# Stays-armed after an id-preserving op
# --------------------------------------------------------------------------- #


class TestWireEditToolStaysArmed:
    def test_autoroute_stays_armed(self, session: EditorSession) -> None:
        view = _headless(session)
        wire = _detour_wire(session)
        tool = _arm(view, wire)

        tool._autoroute_fragment()

        assert tool._wire_edit is not None  # re-armed on the mutated wire
        assert view.active_tool is tool

    def test_flip_stays_armed(self, session: EditorSession) -> None:
        view = _headless(session)
        wire = _detour_wire(session)
        tool = _arm(view, wire)

        tool._flip_wire_fragment()

        assert tool._wire_edit is not None
        assert view.active_tool is tool

    def test_simplify_stays_armed(self, session: EditorSession) -> None:
        view = _headless(session)
        wire = _detour_wire(session)
        tool = _arm(view, wire)

        tool._simplify_wire_fragment()

        assert tool._wire_edit is not None
        assert view.active_tool is tool


class TestWireEditToolKnotlessWire:
    """Regression: a wire loaded without endpoint knots (``a``/``b`` None) must
    still support fragment flip / simplify.  Before the fix the ops raised
    ``ValueError: replace_wire_fragments requires a Wire with both knots`` (only
    reroute worked, because its builder backfills knots)."""

    def test_flip_on_knotless_wire_does_not_raise(
        self, session: EditorSession
    ) -> None:
        view = _headless(session)
        wire = _knotless_wire(session)
        tool = _arm(view, wire)

        tool._flip_wire_fragment()  # must not raise

        after = session.layout.wires.get(wire.id)
        assert after is not None
        assert after.side == "bottom"  # top -> bottom (opposite layer)
        assert tool._wire_edit is not None  # stays armed

    def test_simplify_on_knotless_wire_does_not_raise(
        self, session: EditorSession
    ) -> None:
        view = _headless(session)
        wire = _knotless_wire(session)
        n_before = len(session.layout.wires.get(wire.id).path)
        tool = _arm(view, wire)

        tool._simplify_wire_fragment()  # must not raise

        after = session.layout.wires.get(wire.id)
        assert after is not None
        assert len(after.path) < n_before  # corner-cut shortened it
        assert tool._wire_edit is not None


# --------------------------------------------------------------------------- #
# Delete exits
# --------------------------------------------------------------------------- #


class TestWireEditToolDeleteExits:
    def test_delete_exits_to_select(self, session: EditorSession) -> None:
        view = _headless(session)
        wire = _detour_wire(session)
        tool = _arm(view, wire)
        n_wires_before = len(session.layout.wires)

        tool._delete_fragment()

        # Whole-wire delete: nothing left to re-arm -> return_to fired, control
        # handed back to the plain SelectTool.
        assert view.active_tool is not tool
        assert isinstance(view.active_tool, SelectTool)
        assert len(session.layout.wires) == n_wires_before - 1


# --------------------------------------------------------------------------- #
# Reroute two-stage Esc
# --------------------------------------------------------------------------- #


class TestWireEditToolReroute:
    def test_reroute_esc_keeps_fragment(self, session: EditorSession) -> None:
        view = _headless(session)
        wire = _detour_wire(session)
        tool = _arm(view, wire)

        tool._begin_reroute()  # arms the waypoint pick
        assert tool._mouse is not None
        assert tool.has_pending_op() is True  # a live reroute pick is pending
        assert tool.can_exit() is False  # first Esc must cancel, not exit

        tool.cancel_op()  # Esc mid-reroute
        # The fragment (_wire_edit) is NOT dropped; only the waypoint pick was.
        assert tool._wire_edit is not None
        assert tool.has_pending_op() is False  # no pick in flight now
        assert tool.can_exit() is True  # a second (clean) Esc could exit


# --------------------------------------------------------------------------- #
# ArmedSubTool conformance
# --------------------------------------------------------------------------- #


class TestWireEditToolConformance:
    def test_conforms_to_armed_subtool(self, session: EditorSession) -> None:
        view = _headless(session)
        wire = _detour_wire(session)
        tool = _arm(view, wire)
        assert isinstance(tool, ArmedSubTool)


# --------------------------------------------------------------------------- #
# Two consecutive ops, both execute without a re-arm round-trip
# --------------------------------------------------------------------------- #


class TestWireEditToolConsecutiveOps:
    def test_simplify_then_flip_both_execute(
        self, session: EditorSession
    ) -> None:
        view = _headless(session)
        wire = _detour_wire(session)
        tool = _arm(view, wire)

        n_before = len(session.stack._undo)  # type: ignore[attr-defined]

        tool._simplify_wire_fragment()  # [S] — first op
        assert tool._wire_edit is not None  # still armed, no round-trip
        assert view.active_tool is tool

        tool._flip_wire_fragment()  # [F] — second op, no re-entry needed
        assert tool._wire_edit is not None
        assert view.active_tool is tool

        # Both ops landed real commands on the stack.
        assert len(session.stack._undo) == n_before + 2  # type: ignore[attr-defined]
