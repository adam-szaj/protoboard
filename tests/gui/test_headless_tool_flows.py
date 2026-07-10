# SPDX-License-Identifier: MIT
"""Headless tool-flow tests unlocked by the qt-ports Protocol swap.

Everything here drives the real tools (``PlaceTool`` / ``SelectTool``) through
the input-DTO seam (``pointer`` / ``key_inp``) against a
:class:`~tests.fakes.board_view.HeadlessBoardView` — no realized ``QWidget``,
no event loop, no ``qtbot``.  These are the user journeys that could only be
exercised through the live widget before ``AbstractBoardView`` became a
Qt-free Protocol (commits tagged *qt-ports*):

* selecting components, wires and pads (pin-scoped hits);
* moving a component by drag and undoing it;
* rotating a selected component through the armed float-transform;
* collision / conflict classification while a component is being moved;
* PlaceTool confirm vs. cancel — including cancel *before the first element
  is placed*, which lives in the tool host, not the tool.

The board fixture is the shared 5x5 perfboard (``tests/conftest.py``): pad
centres at ``(col+0.5, row+0.5)``, bounds ``Rect(0, 0, 5, 5)``.  The fixture
library part ``"R"`` is a generic 2-pin resistor with footprint ``SIL(2)`` —
pin 1 at the anchor, pin 2 one raster unit below (``position + (0, 1)``).
"""

from __future__ import annotations

import dataclasses as dc
import uuid
from typing import cast

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent

from protoboard.domain.commands.base import ComponentState
from protoboard.domain.layout.placement import ComponentPlacement, move_blocked
from protoboard.domain.layout.wire import Wire
from protoboard.geometry import Point
from protoboard.gui.placement_conflict import classify_drag_commit
from protoboard.gui.ports import Mods
from protoboard.gui.selection.adapters import (
    ComponentSelectable,
    WireSelectable,
)
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.drag_commit_decision import (
    DragCommitDecision,
    DragSeverity,
)
from protoboard.gui.tools.place_tool import PlaceTool
from protoboard.gui.tools.select_tool import SelectTool
from tests.fakes.board_view import HeadlessBoardView
from tests.gui.input_helpers import key_inp, pointer

# --------------------------------------------------------------------------- #
# Helpers — mirror the widget-suite idioms (`_place`, `_v2`, `_key`) but land
# every call on the HeadlessBoardView instead of a realized BoardView.
# --------------------------------------------------------------------------- #


def _headless(session: EditorSession) -> HeadlessBoardView:
    return HeadlessBoardView(session)


def _place(view: HeadlessBoardView, x: float, y: float, kind: str = "R"):
    """Place *kind* at scene ``(x, y)`` and return the fresh instance.

    A generic 2-pin part commits on the first click (``auto_accept_placement``
    defaults to True); a bendable part would loop through ``drag_pin`` clicks,
    so drain that phase defensively.
    """
    view.tools.set_tool(PlaceTool(kind))
    tool = view.active_tool
    assert isinstance(tool, PlaceTool)
    tool.on_press(pointer(x, y))
    while tool._phase == "drag_pin":
        tool.on_press(pointer(x, y))
    return list(view.session.instances.values())[-1]


def _select_tool(view: HeadlessBoardView, **kwargs: object) -> SelectTool:
    tool = SelectTool(**kwargs)  # type: ignore[arg-type]
    view.tools.set_tool(tool)
    return tool


def _selected_kinds(tool: SelectTool) -> set[str]:
    return {s.to_hit().kind for s in tool._selection.selected()}


def _escape_event() -> QKeyEvent:
    ev = QKeyEvent(
        QEvent.Type.KeyPress,
        Qt.Key.Key_Escape,
        Qt.KeyboardModifier.NoModifier,
    )
    ev.ignore()
    return ev


# --------------------------------------------------------------------------- #
# 1. Selection — components, wires, pads (pin-scoped hits)
# --------------------------------------------------------------------------- #


class TestSelectComponent:
    def test_click_selects_component(self, session: EditorSession) -> None:
        view = _headless(session)
        inst = _place(view, 0.5, 0.5)
        tool = _select_tool(view)
        tool.on_press(pointer(0.5, 0.5))
        assert _selected_kinds(tool) == {"component"}
        assert inst.id in tool._selected_ids

    def test_click_empty_space_selects_nothing(
        self, session: EditorSession
    ) -> None:
        view = _headless(session)
        _place(view, 0.5, 0.5)
        tool = _select_tool(view)
        # Press+release on a bare pad far from the part is an empty click.
        tool.on_press(pointer(4.5, 4.5))
        tool.on_release(pointer(4.5, 4.5))
        assert tool._selection.is_empty()

    def test_component_hit_carries_pin_number(
        self, session: EditorSession
    ) -> None:
        # Clicking a pad centre where a pin sits is a pin-scoped component hit
        # (a "pad" is not a distinct Hit.kind — it is a component + pin_number).
        view = _headless(session)
        _place(view, 0.5, 0.5)  # pin 1 at (0.5, 0.5), pin 2 at (0.5, 1.5)
        tool = _select_tool(view)
        tool.on_press(pointer(0.5, 1.5))
        hit = next(iter(tool._selection.selected())).to_hit()
        assert hit.kind == "component"
        assert hit.pin_number == 2


class TestSelectWire:
    @staticmethod
    def _add_wire(session: EditorSession) -> Wire:
        w = Wire(
            id=uuid.uuid4(),
            path=[Point(2.5, 0.5), Point(2.5, 2.5)],
            insulated=True,
            side="top",
        )
        session.layout.wires.append(w)
        session.invalidate_graph()
        session.layout_changed.emit()
        return w

    def test_wire_selectable_reports_wire_kind(
        self, session: EditorSession
    ) -> None:
        # Pin beats wire in hit priority, so wires are selected through the
        # service (or a rubber band) rather than a click on a shared pad.
        view = _headless(session)
        wire = self._add_wire(session)
        tool = _select_tool(view)
        tool._selection.set(WireSelectable(wire=wire))
        assert _selected_kinds(tool) == {"wire"}
        assert wire.id in tool._selected_wire_ids

    def test_wire_click_midspan_selects_wire(
        self, session: EditorSession
    ) -> None:
        # A point on the wire body that is NOT a component pin resolves to the
        # wire (nothing else is there on the empty board).
        view = _headless(session)
        self._add_wire(session)
        tool = _select_tool(view)
        tool.on_press(pointer(2.5, 1.5))
        assert _selected_kinds(tool) == {"wire"}


class TestSelectPad:
    def test_pad_pick_is_pin_scoped(self, session: EditorSession) -> None:
        # A pad occupied by a pin selects that component's pin.
        view = _headless(session)
        inst = _place(view, 1.5, 1.5)
        tool = _select_tool(view)
        tool.on_press(pointer(1.5, 1.5))
        hit = next(iter(tool._selection.selected())).to_hit()
        assert hit.kind == "component"
        assert hit.id == inst.id
        assert hit.pin_number == 1


class TestMultiSelect:
    def test_set_many_selects_all_components(
        self, session: EditorSession
    ) -> None:
        view = _headless(session)
        _place(view, 0.5, 0.5)
        _place(view, 3.5, 0.5)
        tool = _select_tool(view)
        tool._selection.set_many([
            ComponentSelectable(component_id=cid, placement=pl)
            for cid, pl in session.layout.placements.items()
        ])
        assert tool._selected_ids == set(session.layout.placements.keys())
        assert len(tool._selected_ids) == 2


# --------------------------------------------------------------------------- #
# 1b. Verb vocabulary parity (docs MVC step 1) — driving the tool through the
#     semantic verbs (default_action / secondary_action / empty_action) must
#     match driving it through the legacy on_press hooks.  These are the guard
#     tests that let the live Qt path re-route onto verbs in a later commit.
# --------------------------------------------------------------------------- #


class TestVerbParity:
    def test_default_action_selects_component_like_on_press(
        self, session: EditorSession
    ) -> None:
        view = _headless(session)
        inst = _place(view, 0.5, 0.5)
        tool = _select_tool(view)
        consumed = tool.default_action(Point(0.5, 0.5), Mods())
        assert consumed is True
        assert _selected_kinds(tool) == {"component"}
        assert inst.id in tool._selected_ids

    def test_default_action_empty_space_selects_nothing(
        self, session: EditorSession
    ) -> None:
        view = _headless(session)
        _place(view, 0.5, 0.5)
        tool = _select_tool(view)
        tool.default_action(Point(4.5, 4.5), Mods())
        tool.on_release(pointer(4.5, 4.5))
        assert tool._selection.is_empty()

    def test_secondary_action_publishes_component_context(
        self, session: EditorSession
    ) -> None:
        view = _headless(session)
        inst = _place(view, 0.5, 0.5)
        tool = _select_tool(view)
        published = tool.secondary_action(Point(0.5, 0.5), Mods())
        assert published is True  # item present → context published
        ctx = view.notifications.contexts[-1]
        assert ctx.hit == ("component", inst.id)
        assert ctx.actions  # non-empty option set

    def test_secondary_action_bare_canvas_falls_through(
        self, session: EditorSession
    ) -> None:
        view = _headless(session)
        _place(view, 0.5, 0.5)
        tool = _select_tool(view)
        # Empty pad, no item — the verb declines so the View shows the generic
        # canvas menu instead.
        assert tool.secondary_action(Point(4.5, 4.5), Mods()) is False

    def test_rmb_chord_routes_through_secondary_action(
        self, session: EditorSession
    ) -> None:
        # Sub-commit 5: the RMB mouse chord dispatches to secondary_action,
        # which publishes the item context (parity with the old on_right_press).
        from protoboard.gui.ports import RIGHT_BUTTON
        view = _headless(session)
        inst = _place(view, 0.5, 0.5)
        tool = _select_tool(view)
        consumed = tool._mouse_context_menu(
            pointer(0.5, 0.5, button=RIGHT_BUTTON)
        )
        assert consumed is True
        assert view.notifications.contexts[-1].hit == ("component", inst.id)

    def test_empty_action_reports_hovered_component(
        self, session: EditorSession
    ) -> None:
        # Sub-commit 2: empty_action hit-tests eagerly and reports the domain
        # id under the cursor.
        view = _headless(session)
        inst = _place(view, 0.5, 0.5)  # pin 1 at (0.5, 0.5)
        tool = _select_tool(view)
        ev = tool.empty_action(Point(0.5, 0.5), Mods())
        assert ev.point == Point(0.5, 0.5)
        assert ev.objects == (inst.id,)

    def test_empty_action_bare_canvas_reports_nothing(
        self, session: EditorSession
    ) -> None:
        view = _headless(session)
        _place(view, 0.5, 0.5)
        tool = _select_tool(view)
        ev = tool.empty_action(Point(4.5, 4.5), Mods())
        assert ev.objects == ()

    def test_empty_action_is_a_pure_query(
        self, session: EditorSession
    ) -> None:
        # Hovering an object must not select it or change interaction state.
        view = _headless(session)
        _place(view, 0.5, 0.5)
        tool = _select_tool(view)
        tool.empty_action(Point(0.5, 0.5), Mods())
        assert tool._selection.is_empty()
        assert tool.state == "select.idle"


# --------------------------------------------------------------------------- #
# 1c. Hover throttle (docs MVC step 1 §3) — the View collapses the move stream
#     to one empty_action per interval and publishes the reply via notify_hover.
# --------------------------------------------------------------------------- #


class TestHoverThrottle:
    def test_fire_hover_publishes_reply(
        self, session: EditorSession
    ) -> None:
        # Simulate the throttle's trailing fire: the host runs empty_action on
        # the active tool and publishes the enriched reply to notify_hover.
        view = _headless(session)
        inst = _place(view, 0.5, 0.5)
        _select_tool(view)
        view.tools._fire_hover(Point(0.5, 0.5), Mods())
        assert view.notifications.hovers[-1].objects == (inst.id,)

    def test_throttle_coalesces_to_latest_sample(self) -> None:
        # A burst of notes inside one interval yields a single trailing fire
        # carrying the most recent sample — park-latest coalescing.
        from protoboard.gui.tool_host_details import HoverThrottle

        fired: list[Point] = []
        throttle = HoverThrottle(33, lambda pt, _mods: fired.append(pt))
        throttle.note(Point(1.0, 1.0), Mods())
        throttle.note(Point(2.0, 2.0), Mods())
        throttle.note(Point(3.0, 3.0), Mods())
        throttle._flush()  # stand in for the single-shot timeout
        assert fired == [Point(3.0, 3.0)]


# --------------------------------------------------------------------------- #
# 2. Moving a component — drag, commit, undo/redo
# --------------------------------------------------------------------------- #


class TestMoveComponent:
    def test_drag_translates_and_commits_one_command(
        self, session: EditorSession
    ) -> None:
        view = _headless(session)
        inst = _place(view, 0.5, 0.5)
        tool = _select_tool(view)
        tool.on_press(pointer(0.5, 0.5))  # select + arm drag
        before = session.layout.placements[inst.id].position
        depth = len(session.stack._undo)

        tool.on_move(pointer(2.5, 0.5))  # past the 0.5 drag threshold
        tool.on_release(pointer(2.5, 0.5))

        after = session.layout.placements[inst.id].position
        assert (after.x, after.y) == (2.5, 0.5)
        assert (after.x, after.y) != (before.x, before.y)
        assert len(session.stack._undo) == depth + 1  # exactly one Move command
        assert tool._mouse is None  # drag finished, not left floating

    def test_subthreshold_press_release_is_a_click_not_a_move(
        self, session: EditorSession
    ) -> None:
        view = _headless(session)
        inst = _place(view, 0.5, 0.5)
        tool = _select_tool(view)
        depth = len(session.stack._undo)
        tool.on_press(pointer(0.5, 0.5))
        tool.on_release(pointer(0.5, 0.5))  # no move → a click
        assert session.layout.placements[inst.id].position == Point(0.5, 0.5)
        assert len(session.stack._undo) == depth  # no command

    def test_undo_restores_position_redo_reapplies(
        self, session: EditorSession
    ) -> None:
        view = _headless(session)
        inst = _place(view, 0.5, 0.5)
        tool = _select_tool(view)
        tool.on_press(pointer(0.5, 0.5))
        before = session.layout.placements[inst.id].position
        tool.on_move(pointer(2.5, 0.5))
        tool.on_release(pointer(2.5, 0.5))
        moved = session.layout.placements[inst.id].position

        session.stack.undo(cast(ComponentState, session))
        assert session.layout.placements[inst.id].position == before

        session.stack.redo(cast(ComponentState, session))
        assert session.layout.placements[inst.id].position == moved


# --------------------------------------------------------------------------- #
# 3. Rotating a component — armed float-transform
# --------------------------------------------------------------------------- #


class TestRotateComponent:
    def test_r_arms_then_steps_rotation(
        self, session: EditorSession
    ) -> None:
        view = _headless(session)
        inst = _place(view, 0.5, 0.5)
        tool = _select_tool(view)
        tool.on_press(pointer(0.5, 0.5))
        before = session.layout.placements[inst.id].rotation

        tool.on_key(key_inp(Qt.Key.Key_R))  # ARM — no rotation yet
        assert tool._armed is not None
        assert session.layout.placements[inst.id].rotation == before

        tool.on_key(key_inp(Qt.Key.Key_R))  # STEP +90 CW
        assert session.layout.placements[inst.id].rotation == (before + 90) % 360

    def test_enter_commits_rotation_as_one_command(
        self, session: EditorSession
    ) -> None:
        view = _headless(session)
        inst = _place(view, 0.5, 0.5)
        tool = _select_tool(view)
        tool.on_press(pointer(0.5, 0.5))
        tool.on_key(key_inp(Qt.Key.Key_R))  # arm
        tool.on_key(key_inp(Qt.Key.Key_R))  # +90
        depth = len(session.stack._undo)

        tool.on_key(key_inp(Qt.Key.Key_Return))  # CONFIRM

        assert tool._armed is None
        assert session.layout.placements[inst.id].rotation == 90
        assert len(session.stack._undo) == depth + 1

    def test_escape_cancels_rotation(self, session: EditorSession) -> None:
        view = _headless(session)
        inst = _place(view, 0.5, 0.5)
        tool = _select_tool(view)
        tool.on_press(pointer(0.5, 0.5))
        before = session.layout.placements[inst.id].rotation
        depth = len(session.stack._undo)

        tool.on_key(key_inp(Qt.Key.Key_R))  # arm
        tool.on_key(key_inp(Qt.Key.Key_R))  # +90 (uncommitted)
        tool.on_key(key_inp(Qt.Key.Key_Escape))  # CANCEL

        assert tool._armed is None
        assert session.layout.placements[inst.id].rotation == before
        assert len(session.stack._undo) == depth  # nothing committed

    def test_rotation_undo_restores_orientation(
        self, session: EditorSession
    ) -> None:
        view = _headless(session)
        inst = _place(view, 0.5, 0.5)
        tool = _select_tool(view)
        tool.on_press(pointer(0.5, 0.5))
        tool.on_key(key_inp(Qt.Key.Key_R))
        tool.on_key(key_inp(Qt.Key.Key_R))
        tool.on_key(key_inp(Qt.Key.Key_Return))
        assert session.layout.placements[inst.id].rotation == 90

        session.stack.undo(cast(ComponentState, session))
        assert session.layout.placements[inst.id].rotation == 0


# --------------------------------------------------------------------------- #
# 4. Collision / conflict detection while moving
# --------------------------------------------------------------------------- #


class TestMoveCollision:
    """Two layers: the pure domain predicate and the GUI drag classifier."""

    @staticmethod
    def _place_two(view: HeadlessBoardView):
        """Place a stationary R1 and return the movable R2's instance."""
        _place(view, 0.5, 0.5)  # R1 — pins (0.5,0.5),(0.5,1.5)
        r2 = _place(view, 3.5, 3.5)
        view.session.invalidate_graph()
        return r2

    def _move_blocked(
        self, session: EditorSession, comp_id: uuid.UUID,
        placement: ComponentPlacement,
    ) -> bool:
        return move_blocked(
            comp_id, placement,
            instances=dict(session.instances.items()),
            components={k: session.components[k] for k in session.components},
            layout=session.layout,
        )

    def test_body_overlap_disjoint_pins_is_blocked(
        self, session: EditorSession
    ) -> None:
        # R2 shifted half a cell onto R1's body: bodies overlap, no pad shared.
        view = _headless(session)
        r2 = self._place_two(view)
        overlap = dc.replace(
            session.layout.placements[r2.id], position=Point(0.5, 0.0)
        )
        assert self._move_blocked(session, r2.id, overlap) is True

    def test_shared_pad_is_not_blocked(
        self, session: EditorSession
    ) -> None:
        # Landing R2 exactly on R1 shares both pads — a legal soldering
        # scenario routed to the conflict resolver, never a hard block.
        view = _headless(session)
        r2 = self._place_two(view)
        shared = dc.replace(
            session.layout.placements[r2.id], position=Point(0.5, 0.5)
        )
        assert self._move_blocked(session, r2.id, shared) is False

    def test_far_move_is_not_blocked(self, session: EditorSession) -> None:
        view = _headless(session)
        r2 = self._place_two(view)
        clear = dc.replace(
            session.layout.placements[r2.id], position=Point(4.5, 4.5)
        )
        assert self._move_blocked(session, r2.id, clear) is False

    def test_classifier_reports_hard_for_body_overlap(
        self, session: EditorSession
    ) -> None:
        # The GUI-level classifier the drag consults each frame: a body
        # overlap with disjoint pins is HARD and cannot commit.
        view = _headless(session)
        r2 = self._place_two(view)
        session.layout.placements[r2.id] = dc.replace(
            session.layout.placements[r2.id], position=Point(0.5, 0.0)
        )
        session.invalidate_graph()

        decision = classify_drag_commit(
            session, {r2.id}, set(), frozenset({r2.id})
        )
        assert decision.severity is DragSeverity.HARD
        assert decision.can_commit is False
        assert decision.blocking_pad_positions

    def test_classifier_reports_clear_for_free_move(
        self, session: EditorSession
    ) -> None:
        view = _headless(session)
        r2 = self._place_two(view)
        session.layout.placements[r2.id] = dc.replace(
            session.layout.placements[r2.id], position=Point(4.5, 4.5)
        )
        session.invalidate_graph()

        decision = classify_drag_commit(
            session, {r2.id}, set(), frozenset({r2.id})
        )
        assert decision.severity is DragSeverity.CLEAR
        assert decision.can_commit is True

    def test_hard_decision_drives_red_outline_override(
        self, session: EditorSession
    ) -> None:
        # The tool's live-preview reaction to a HARD verdict: the selection
        # outline flips to red (recorded on the headless view).  Amber ARGB
        # for SOFT, red for HARD — assert the red channel dominates.
        view = _headless(session)
        tool = _select_tool(view)
        tool._show_conflict_corner(
            DragCommitDecision(severity=DragSeverity.HARD, description="overlap")
        )
        assert view.outline_overrides  # an override was pushed
        color = view.outline_overrides[-1]
        assert color is not None and color.startswith("#")

    def test_clear_decision_resets_outline_override(
        self, session: EditorSession
    ) -> None:
        view = _headless(session)
        tool = _select_tool(view)
        tool._show_conflict_corner(
            DragCommitDecision(severity=DragSeverity.HARD, description="x")
        )
        tool._show_conflict_corner(
            DragCommitDecision(severity=DragSeverity.CLEAR, description="")
        )
        # A CLEAR verdict clears the override (last recorded value is None).
        assert view.outline_overrides[-1] is None


# --------------------------------------------------------------------------- #
# 5. PlaceTool confirm vs. cancel — before the first element is placed
# --------------------------------------------------------------------------- #


class TestPlaceConfirm:
    def test_click_confirms_and_commits(self, session: EditorSession) -> None:
        view = _headless(session)
        _place(view, 0.5, 0.5)
        assert len(session.layout.placements) == 1
        assert session.stack.can_undo()

    def test_place_undo_removes_component(
        self, session: EditorSession
    ) -> None:
        view = _headless(session)
        _place(view, 0.5, 0.5)
        session.stack.undo(cast(ComponentState, session))
        assert len(session.layout.placements) == 0


class TestPlaceCancelBeforeFirstElement:
    """Cancel before anything is placed — the behaviour that lives in the
    tool host (Esc always escapes to Select) rather than in PlaceTool."""

    def test_escape_before_placing_escapes_to_select_and_places_nothing(
        self, session: EditorSession
    ) -> None:
        view = _headless(session)
        view.tools.set_tool(PlaceTool("R"))
        assert len(session.layout.placements) == 0

        view.tools.handle_key_press(_escape_event())

        # The host published an escape-to-Select; nothing landed in the layout.
        assert view.notifications.escapes == 1
        assert len(session.layout.placements) == 0
        assert not session.stack.can_undo()

    def test_place_tool_escape_in_anchor_is_a_noop_returning_false(
        self, session: EditorSession
    ) -> None:
        # PlaceTool itself does not own the Esc-in-ANCHOR cancel: on_key
        # returns False (unconsumed) and mutates nothing.  The host is what
        # turns that into escape-to-Select (asserted above).
        view = _headless(session)
        view.tools.set_tool(PlaceTool("R"))
        tool = view.active_tool
        assert isinstance(tool, PlaceTool)
        consumed = tool.on_key(key_inp(Qt.Key.Key_Escape))
        assert consumed is False
        assert tool._phase == "ancor"  # unchanged (note: enum value spelling)
        assert len(session.layout.placements) == 0

    def test_escape_with_no_active_tool_still_escapes(
        self, session: EditorSession
    ) -> None:
        # Defensive: the host's escape-to-Select fires even when no tool is
        # active, and never touches the layout.
        view = _headless(session)
        view.tools.set_tool(None)
        view.tools.handle_key_press(_escape_event())
        assert view.notifications.escapes == 1
        assert len(session.layout.placements) == 0


class TestPlaceRotationBeforeCommit:
    def test_rotate_ghost_then_place_lands_rotated(
        self, session: EditorSession
    ) -> None:
        # PlaceTool claims [R] to rotate the ghost before the first click.
        view = _headless(session)
        view.tools.set_tool(PlaceTool("R"))
        tool = view.active_tool
        assert isinstance(tool, PlaceTool)
        tool.on_key(key_inp(Qt.Key.Key_R))
        assert tool._rotation == 90
        tool.on_press(pointer(0.5, 0.5))
        placement = next(iter(session.layout.placements.values()))
        assert placement.rotation == 90
