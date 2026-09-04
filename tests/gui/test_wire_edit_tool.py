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
        [
            Point(1.5, 1.5),
            Point(2.5, 1.5),
            Point(3.5, 1.5),
            Point(3.5, 2.5),
            Point(3.5, 3.5),
        ],
        insulated=True,
        side="top",
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
        path=[
            Point(1.5, 1.5),
            Point(2.5, 1.5),
            Point(3.5, 1.5),
            Point(3.5, 2.5),
            Point(3.5, 3.5),
        ],
        insulated=True,
        side="top",
    )
    session.layout.wires[wire.id] = wire
    session.invalidate_graph()
    session.layout_changed.emit()
    assert wire.a is None and wire.b is None  # the knot-less precondition
    return wire


def _arm(view: HeadlessBoardView, wire: Wire) -> WireEditTool:
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


def _arm_range(
    view: HeadlessBoardView, wire: Wire, start_idx: int, end_idx: int
) -> WireEditTool:
    """Arm a WireEditTool on a SUB-RANGE ``[start_idx, end_idx]`` of *wire*
    (not the full span), through the ToolHost so activate() runs."""
    select = SelectTool()
    view.tools.set_tool(select)
    tool = WireEditTool(
        WireRange(
            start_wire_id=wire.id,
            start_idx=start_idx,
            end_wire_id=wire.id,
            end_idx=end_idx,
        ),
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

        assert view.active_tool is tool  # re-armed on the mutated wire
        assert view.active_tool is tool

    def test_flip_stays_armed(self, session: EditorSession) -> None:
        view = _headless(session)
        wire = _detour_wire(session)
        tool = _arm(view, wire)

        tool._flip_wire_fragment()

        assert view.active_tool is tool

    def test_simplify_stays_armed(self, session: EditorSession) -> None:
        view = _headless(session)
        wire = _detour_wire(session)
        tool = _arm(view, wire)

        tool._simplify_wire_fragment()

        assert view.active_tool is tool


class TestFlipPreservesSubRange:
    """Regression: flipping a picked SUB-RANGE must keep that sub-range armed,
    not grow it to the whole wire.

    Bug: ``_commit_and_rearm`` always called ``_rearm_full_span``, so after one
    flip of ``[1,3]`` the armed range became the whole 5-point wire ``[0,4]``.
    Combined with the intentional double-dispatch of ``[F]`` (Qt ShortcutOverride
    veto + re-delivered KeyPress), the SECOND flip then hit the whole wire instead
    of re-toggling the fragment -- flipping segments the user never selected, and
    the highlight span visibly grew to cover the entire wire.
    """

    def test_flip_keeps_sub_range_armed(self, session: EditorSession) -> None:
        view = _headless(session)
        wire = _detour_wire(session)  # 5-point wire, idx 0..4
        tool = _arm_range(view, wire, 1, 3)

        tool._flip_wire_fragment()

        state = tool._wire_edit
        assert state is not None
        # The armed range must still be the picked sub-range, NOT the full span.
        assert (state.start_idx, state.end_idx) == (1, 3), (
            f"flip grew the armed range to "
            f"({state.start_idx}, {state.end_idx}); expected (1, 3)"
        )

    def test_double_flip_sub_range_is_net_zero(self, session: EditorSession) -> None:
        """Two flips of the same sub-range (as one physical [F] double-dispatches)
        return every segment to its original side -- no whole-wire corruption."""
        view = _headless(session)
        wire = _detour_wire(session)  # all segments start on "top"
        tool = _arm_range(view, wire, 1, 3)

        def _sides() -> list[str]:
            w = session.layout.wires.get(wire.id)
            assert w is not None
            return [seg.side for seg in w.fragments]

        before = _sides()
        tool._flip_wire_fragment()  # dispatch 1
        tool._flip_wire_fragment()  # dispatch 2 (same physical [F])
        after = _sides()

        assert after == before, (
            f"double-flip of sub-range must be net-zero; "
            f"sides went {before} -> {after}"
        )


class TestWireEditToolKnotlessWire:
    """Regression: a wire loaded without endpoint knots (``a``/``b`` None) must
    still support fragment flip / simplify.  Before the fix the ops raised
    ``ValueError: replace_wire_fragments requires a Wire with both knots`` (only
    reroute worked, because its builder backfills knots)."""

    def test_flip_on_knotless_wire_does_not_raise(self, session: EditorSession) -> None:
        view = _headless(session)
        wire = _knotless_wire(session)
        tool = _arm(view, wire)

        tool._flip_wire_fragment()  # must not raise

        after = session.layout.wires.get(wire.id)
        assert after is not None
        assert after.side == "bottom"  # top -> bottom (opposite layer)
        assert view.active_tool is tool  # stays armed

    def test_simplify_on_knotless_wire_does_not_raise(
        self, session: EditorSession
    ) -> None:
        view = _headless(session)
        wire = _knotless_wire(session)
        n_before = len(session.layout.wires.get_or_raise(wire.id).path)
        tool = _arm(view, wire)

        tool._simplify_wire_fragment()  # must not raise

        after = session.layout.wires.get(wire.id)
        assert after is not None
        assert len(after.path) < n_before  # corner-cut shortened it
        assert view.active_tool is tool


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
        assert tool.has_pending_op() is True  # a live reroute pick is pending
        assert tool.can_exit() is False  # first Esc must cancel, not exit

        tool.cancel_op()  # Esc mid-reroute
        # The fragment (_wire_edit) is NOT dropped; only the waypoint pick was.
        assert view.active_tool is tool
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
# Overlay span slice
# --------------------------------------------------------------------------- #


class _SpyPreview:
    """Minimal ``SelectPreview`` stand-in recording ``show_wire_edit_wire``
    calls; every other method is a no-op stub."""

    def __init__(self) -> None:
        self.last_span: list[Point] | None = None

    def show_wire_edit_wire(self, path, marked, span=None) -> None:  # type: ignore[no-untyped-def]
        self.last_span = span

    def show_pivot(self, pos, *, shared) -> None: ...  # type: ignore[no-untyped-def]
    def clear_pivots(self) -> None: ...
    def show_band(self, a, b, ltr) -> None: ...  # type: ignore[no-untyped-def]
    def clear_band(self) -> None: ...
    def show_redrag_pad(self, pt) -> None: ...  # type: ignore[no-untyped-def]
    def show_reach_disc(self, centre, radius) -> None: ...  # type: ignore[no-untyped-def]
    def show_redrag_live(self, pos, ok) -> None: ...  # type: ignore[no-untyped-def]
    def clear_redrag_live(self) -> None: ...
    def clear_redrag(self) -> None: ...
    def clear_wire_edit(self) -> None: ...
    def show_reroute_path(self, points) -> None: ...  # type: ignore[no-untyped-def]
    def show_reroute_live(self, a, b) -> None: ...  # type: ignore[no-untyped-def]
    def clear_reroute_live(self) -> None: ...
    def clear_reroute(self) -> None: ...
    def on_scene_cleared(self) -> None: ...


class TestWireEditToolOverlaySpan:
    def test_overlay_receives_span_slice_for_sub_range(
        self, session: EditorSession
    ) -> None:
        """_draw_wire_edit_overlay passes wire.path[start:end+1] as the span
        for a same-wire sub-range pick."""
        view = _headless(session)
        wire = _detour_wire(session)  # 5-point path
        assert len(wire.path) >= 4
        select = SelectTool()
        view.tools.set_tool(select)
        tool = WireEditTool(
            WireRange(
                start_wire_id=wire.id,
                start_idx=1,
                end_wire_id=wire.id,
                end_idx=3,
            ),
            wire.id,
            return_to=lambda: view.tools.set_tool(select),
        )
        view.tools.set_tool(tool)
        spy = _SpyPreview()
        tool._preview = spy  # type: ignore[assignment]

        tool._draw_wire_edit_overlay()

        assert spy.last_span == list(wire.path[1:4])

    def test_overlay_receives_full_span_for_whole_wire(
        self, session: EditorSession
    ) -> None:
        """A whole-wire arm (``WireRange.full_span``) yields a span equal to
        the entire path."""
        view = _headless(session)
        wire = _detour_wire(session)
        tool = _arm(view, wire)
        spy = _SpyPreview()
        tool._preview = spy  # type: ignore[assignment]

        tool._draw_wire_edit_overlay()

        assert spy.last_span == list(wire.path)


# --------------------------------------------------------------------------- #
# Two consecutive ops, both execute without a re-arm round-trip
# --------------------------------------------------------------------------- #


class TestWireEditToolConsecutiveOps:
    def test_simplify_then_flip_both_execute(self, session: EditorSession) -> None:
        view = _headless(session)
        wire = _detour_wire(session)
        tool = _arm(view, wire)

        tool._simplify_wire_fragment()  # [S] — first op
        assert view.active_tool is tool  # still armed, no round-trip
        assert view.active_tool is tool

        tool._flip_wire_fragment()  # [F] — second op, no re-entry needed
        assert view.active_tool is tool
        assert view.active_tool is tool

        # Both ops landed real commands on the stack.
        assert session.stack.can_undo()


class TestWireEditOpsResyncSessionCaches:
    """Pin the notify contract: every wire-edit op fires ``notify_changed``.

    A wire-edit op that replaces wires ([I] on a sub-range splits one wire
    into up to three) must leave ``wire_spatial_index`` describing the NEW
    pieces — otherwise hit-test (and the hover highlight) misses them until
    some unrelated op emits ``layout_changed``.
    """

    def test_insulate_split_resyncs_wire_spatial_index(
        self, session: EditorSession
    ) -> None:
        # Mirror the production wiring: BoardView/MainWindow connect
        # layout_changed -> invalidate_graph; the headless fake does not.
        session.layout_changed.connect(session.invalidate_graph)
        view = _headless(session)
        wire = _detour_wire(session)  # insulated=True
        session.wire_spatial_index()  # warm the cache pre-op
        tool = _arm_range(view, wire, 1, 3)  # sub-range → split on [I]

        tool._set_insulated_fragment()  # toggle → bare → 3-way split

        live_ids = {w.id for w in session.layout.wires}
        assert wire.id not in live_ids, "original wire should be replaced"
        indexed = {wid for ids in session.wire_spatial_index().values() for wid in ids}
        assert (
            indexed <= live_ids
        ), f"spatial index holds dead wire ids: {indexed - live_ids}"
        assert live_ids <= indexed, (
            f"new wire pieces missing from the spatial index: " f"{live_ids - indexed}"
        )
