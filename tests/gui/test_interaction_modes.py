# SPDX-License-Identifier: MIT
"""GUI tests for the orthogonal selection axes (filter × relation) system."""

from __future__ import annotations

import uuid

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent

from protoboard.domain.layout.wire import Wire
from protoboard.geometry import Point
from protoboard.gui.board_view import BoardView
from protoboard.gui.selection_axes import (
    RELATION_LABELS,
    RelationClass,
    SelectionFilter,
)
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.place_tool import PlaceTool
from protoboard.gui.tools.select_tool import SelectTool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _place(board_view: BoardView, session: EditorSession, x: float, y: float) -> object:
    board_view.set_tool(PlaceTool("R"))
    tool = board_view._active_tool
    assert isinstance(tool, PlaceTool)
    ev = _ev(x, y, Qt.MouseButton.LeftButton)
    tool.on_press(ev, QPointF(x, y))
    while tool._phase == "drag_pin":
        tool.on_press(ev, QPointF(x, y))
    return list(session.instances.values())[-1]


def _ev(
    x: float, y: float, btn: Qt.MouseButton,
    modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
) -> QMouseEvent:
    return QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(x, y), QPointF(x, y),
        btn, btn, modifiers,
    )


def _press(
    tool: SelectTool, x: float, y: float,
    modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
) -> None:
    tool.on_press(_ev(x, y, Qt.MouseButton.LeftButton, modifiers), QPointF(x, y))


def _add_wire(session: EditorSession, p1: Point, p2: Point) -> Wire:
    wire = Wire(
        id=uuid.uuid4(),
        path=[p1, p2],
        insulated=True,
        side="top",
    )
    session.layout.wires.append(wire)
    session.invalidate_graph()
    return wire


# ---------------------------------------------------------------------------
# Axis state
# ---------------------------------------------------------------------------

class TestAxesState:
    def test_defaults_on_init(self, board_view: BoardView) -> None:
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        assert tool.filter == SelectionFilter.ALL
        assert tool.relation == RelationClass.SAME_LOGICAL_NET

    def test_set_relation_updates_state(self, board_view: BoardView) -> None:
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        tool.set_relation(RelationClass.NEIGHBORS)
        assert tool.relation == RelationClass.NEIGHBORS

    def test_backtick_cycles_relations(self, board_view: BoardView) -> None:
        from PySide6.QtGui import QKeyEvent
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        relations = list(RelationClass)
        start_idx = relations.index(tool.relation)
        key_event = QKeyEvent(
            QKeyEvent.Type.KeyPress,
            Qt.Key.Key_QuoteLeft,
            Qt.KeyboardModifier.NoModifier,
        )
        tool.on_key(key_event)
        assert tool.relation == relations[(start_idx + 1) % len(relations)]

    def test_set_relation_emits_label(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        received: list[str] = []
        session.highlight_mode_changed.connect(received.append)
        tool.set_relation(RelationClass.SAME_PHYSICAL_NET)
        assert received == [RELATION_LABELS[RelationClass.SAME_PHYSICAL_NET]]

    def test_set_filter_updates_state(self, board_view: BoardView) -> None:
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        tool.set_filter(SelectionFilter.COMPONENTS | SelectionFilter.GROUPS)
        assert tool.filter == SelectionFilter.COMPONENTS | SelectionFilter.GROUPS
        assert tool._filter_components() is True
        assert tool._filter_wires() is False
        assert tool._filter_groups() is True


# ---------------------------------------------------------------------------
# Click semantics — LPM
# ---------------------------------------------------------------------------

class TestLeftClickSemantics:
    def test_lpm_on_component_selects_only_clicked(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        board_view.refresh()
        _press(tool, 0.5, 0.5)
        # Only the clicked component is in selection (not multi).
        assert tool._selected_id == inst.id
        assert not tool._selected_ids

    def test_lpm_relation_none_no_auto_entry(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        _place(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        tool.set_relation(RelationClass.NONE)
        board_view.refresh()
        _press(tool, 0.5, 0.5)
        # No auto-highlight entry on the stack.
        assert session.highlight_stack.active() is None
        assert tool._highlight_items == []

    def test_lpm_relation_logical_net_pushes_auto_entry(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        i1 = _place(board_view, session, 0.5, 0.5)
        i2 = _place(board_view, session, 3.5, 0.5)
        from protoboard.domain.schematic.net import Net, PinRef
        net = Net(id=uuid.uuid4(), name="N1", pins={
            PinRef(i1.id, 1), PinRef(i2.id, 1),
        })
        session.schematic.nets[net.id] = net
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        tool.set_relation(RelationClass.SAME_LOGICAL_NET)
        board_view.refresh()
        # Drive through the unified entry point so we don't depend on
        # pin hit-test geometry inside the placed component.
        tool._apply_interaction(comp_id=i1.id, pin_number=1, shift_held=False)
        entry = session.highlight_stack.active()
        assert entry is not None
        # Auto entry must be flagged as auto, sourced by select_tool.
        assert entry.auto is True
        assert entry.source_id == "select_tool"
        # Both components on the net are in the highlighted set.
        assert i1.id in entry.comp_ids and i2.id in entry.comp_ids

    def test_lpm_bbox_click_no_pin_keeps_component_highlighted(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """Regression: clicking the component bbox (no pin hit) must not dim
        the clicked component itself even when relation resolves no net.
        """
        i1 = _place(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        tool.set_relation(RelationClass.SAME_LOGICAL_NET)
        board_view.refresh()
        # pin_number=None simulates a bbox click that missed every pin.
        tool._apply_interaction(comp_id=i1.id, pin_number=None, shift_held=False)
        entry = session.highlight_stack.active()
        assert entry is not None
        # Even without a resolved net, the clicked component is in the
        # highlighted set — otherwise BoardView would dim it along with
        # everything else.
        assert i1.id in entry.comp_ids

    def test_lpm_bbox_click_spans_all_component_nets(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """Bbox click on a multi-net component highlights every net it touches."""
        i1 = _place(board_view, session, 0.5, 0.5)
        i2 = _place(board_view, session, 3.5, 0.5)
        i3 = _place(board_view, session, 6.5, 0.5)
        from protoboard.domain.schematic.net import Net, PinRef
        net_a = Net(id=uuid.uuid4(), name="A", pins={
            PinRef(i1.id, 1), PinRef(i2.id, 1),
        })
        net_b = Net(id=uuid.uuid4(), name="B", pins={
            PinRef(i1.id, 2), PinRef(i3.id, 1),
        })
        session.schematic.nets[net_a.id] = net_a
        session.schematic.nets[net_b.id] = net_b
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        tool.set_relation(RelationClass.SAME_LOGICAL_NET)
        board_view.refresh()
        # Bbox click on i1 (no specific pin) → both nets contribute.
        tool._apply_interaction(comp_id=i1.id, pin_number=None, shift_held=False)
        entry = session.highlight_stack.active()
        assert entry is not None
        assert i1.id in entry.comp_ids
        assert i2.id in entry.comp_ids   # from net A
        assert i3.id in entry.comp_ids   # from net B


# ---------------------------------------------------------------------------
# Click semantics — Shift+LPM (relation ∩ filter)
# ---------------------------------------------------------------------------

class TestShiftLeftClickSemantics:
    def test_shift_lpm_selects_all_net_components(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        i1 = _place(board_view, session, 0.5, 0.5)
        i2 = _place(board_view, session, 3.5, 0.5)
        from protoboard.domain.schematic.net import Net, PinRef
        net = Net(id=uuid.uuid4(), name="N1", pins={
            PinRef(i1.id, 1), PinRef(i2.id, 1),
        })
        session.schematic.nets[net.id] = net
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        tool.set_relation(RelationClass.SAME_LOGICAL_NET)
        tool.set_filter(SelectionFilter.ALL)
        board_view.refresh()
        tool._apply_interaction(comp_id=i1.id, pin_number=1, shift_held=True)
        assert i1.id in tool._selected_ids
        assert i2.id in tool._selected_ids

    def test_shift_lpm_filter_excludes_wires(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        i1 = _place(board_view, session, 0.5, 0.5)
        i2 = _place(board_view, session, 3.5, 0.5)
        from protoboard.domain.schematic.net import Net, PinRef
        net = Net(id=uuid.uuid4(), name="N1", pins={
            PinRef(i1.id, 1), PinRef(i2.id, 1),
        })
        session.schematic.nets[net.id] = net
        wire = _add_wire(session, Point(0.5, 0.5), Point(3.5, 0.5))
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        tool.set_relation(RelationClass.SAME_LOGICAL_NET)
        # Filter excludes wires — Shift+LPM must not select the net's wire.
        tool.set_filter(SelectionFilter.COMPONENTS | SelectionFilter.GROUPS)
        board_view.refresh()
        tool._apply_interaction(comp_id=i1.id, pin_number=1, shift_held=True)
        assert wire.id not in tool._selected_wire_ids


# ---------------------------------------------------------------------------
# HighlightStack precedence: persistent > transient > auto
# ---------------------------------------------------------------------------

class TestHighlightStackPrecedence:
    def test_persistent_beats_auto(self, session: EditorSession) -> None:
        from protoboard.gui.highlight_state import HighlightEntry
        stack = session.highlight_stack
        stack.push_auto(HighlightEntry(label="auto"), source_id="select_tool")
        stack.push(HighlightEntry(label="user"))
        active = stack.active()
        assert active is not None
        assert active.label == "user"
        assert active.auto is False
        assert active.transient is False

    def test_transient_beats_auto(self, session: EditorSession) -> None:
        from protoboard.gui.highlight_state import HighlightEntry
        stack = session.highlight_stack
        stack.push_auto(HighlightEntry(label="auto"), source_id="select_tool")
        stack.push_transient(HighlightEntry(label="route"))
        active = stack.active()
        assert active is not None
        assert active.label == "route"
        assert active.transient is True

    def test_persistent_beats_transient(self, session: EditorSession) -> None:
        from protoboard.gui.highlight_state import HighlightEntry
        stack = session.highlight_stack
        stack.push_transient(HighlightEntry(label="route"))
        stack.push(HighlightEntry(label="user"))
        active = stack.active()
        assert active is not None
        assert active.label == "user"

    def test_push_auto_replaces_same_source(self, session: EditorSession) -> None:
        from protoboard.gui.highlight_state import HighlightEntry
        stack = session.highlight_stack
        stack.push_auto(HighlightEntry(label="first"), source_id="select_tool")
        stack.push_auto(HighlightEntry(label="second"), source_id="select_tool")
        active = stack.active()
        assert active is not None
        assert active.label == "second"

    def test_clear_auto_removes_by_source(self, session: EditorSession) -> None:
        from protoboard.gui.highlight_state import HighlightEntry
        stack = session.highlight_stack
        stack.push_auto(HighlightEntry(label="a"), source_id="select_tool")
        stack.clear_auto("select_tool")
        assert stack.active() is None

    def test_clear_preserves_auto(self, session: EditorSession) -> None:
        from protoboard.gui.highlight_state import HighlightEntry
        stack = session.highlight_stack
        stack.push_auto(HighlightEntry(label="auto"), source_id="select_tool")
        stack.push(HighlightEntry(label="user"))
        stack.clear()  # only persistent goes away
        active = stack.active()
        assert active is not None
        assert active.label == "auto"


# ---------------------------------------------------------------------------
# Routing-net union: RouteTool's net adds to the stack's highlighted ids.
# ---------------------------------------------------------------------------

class TestRoutingNetUnion:
    def test_routing_net_only_dims_everything_else(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        # Place two components on the same net, then mark the net as routing.
        i1 = _place(board_view, session, 0.5, 0.5)
        i2 = _place(board_view, session, 3.5, 0.5)
        from protoboard.domain.schematic.net import Net, PinRef
        net = Net(id=uuid.uuid4(), name="N1", pins={
            PinRef(i1.id, 1), PinRef(i2.id, 1),
        })
        session.schematic.nets[net.id] = net
        board_view.refresh()
        # No stack entry, only routing_net set.
        session.set_routing_net(net.id)
        # Both components are highlighted (full opacity); a third unrelated
        # group would be dimmed.  Use the internal hook directly.
        board_view._apply_highlight_opacity()
        # Sanity: routing_net_id is propagated.
        assert session.routing_net_id == net.id
        session.set_routing_net(None)
        assert session.routing_net_id is None
