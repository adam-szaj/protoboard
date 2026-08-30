# SPDX-License-Identifier: MIT
"""Parity tests restored after the SelectTool v1→v2 swap (commit 541236f).

When the legacy SelectTool monolith was deleted, 151 tests went with it — and
with them the only guard against silently dropping the polished behaviours those
tests covered (which is exactly how the bright highlight, the context menu and
the reroute anchor regressed).  This file re-asserts the behaviours that v2
*still implements*, re-expressed against v2's public/compat surface.

Scope discipline (per CLAUDE.md "Rewrites and swaps"):
  * Restored here = behaviour the user sees AND v2 still implements.
  * NOT restored = tests of legacy private internals with no v2 analog
    (``_excise_cross_wire_range``, ``_hit_test_wire_node_any`` cycle keys), and
    tests of behaviour deliberately deferred — selection-*filter enforcement*
    is tracked in docs/todo.md item 1, so its tests stay retired, not faked.

Source files (deleted by 541626f): test_interaction_modes.py,
test_grouped_component_protection.py.
"""

from __future__ import annotations

import uuid

from PySide6.QtCore import Qt

from protoboard.controller.selection_axes import (
    RELATION_LABELS,
    RelationClass,
    SelectionFilter,
)
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.group import ComponentGroup
from protoboard.domain.layout.layout import ROOT_GROUP_ID
from protoboard.geometry import Point
from protoboard.gui.board_view import BoardView
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.gui_tools import PlaceTool, SelectTool
from tests.gui.input_helpers import key_inp, pointer


def test_select_tool_imports_from_controller() -> None:
    from protoboard.controller.tools.select_tool import SelectTool  # noqa: F401


def test_select_tool_module_is_qt_free() -> None:
    import inspect

    import protoboard.controller.tools.select_tool as st

    src = inspect.getsource(st)
    assert "QColor" not in src
    assert "from PySide6" not in src
    assert "Qt.Key" not in src


# ---------------------------------------------------------------------------
# Helpers (mirrors of the deleted files', kept local to this parity suite)
# ---------------------------------------------------------------------------


def _place(
    board_view: BoardView, session: EditorSession, x: float, y: float
) -> ComponentInstance:
    board_view.set_tool(PlaceTool("R"))
    tool = board_view._active_tool
    assert isinstance(tool, PlaceTool)
    tool.on_press(pointer(x, y))
    while tool._phase == "drag_pin":
        tool.on_press(pointer(x, y))
    return list(session.instances.values())[-1]


def _press(
    tool: SelectTool,
    x: float,
    y: float,
    modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
) -> None:
    tool.on_press(pointer(x, y, mods=modifiers))


def _v2(board_view: BoardView) -> SelectTool:
    board_view.set_tool(SelectTool())
    tool = board_view._active_tool
    assert isinstance(tool, SelectTool)
    return tool


def _make_group(session: EditorSession, member_ids: list[uuid.UUID]) -> uuid.UUID:
    """Wrap *member_ids* in a new non-root group, child of ROOT."""
    grp_id = uuid.uuid4()
    session.layout.groups[grp_id] = ComponentGroup(
        id=grp_id,
        name="G1",
        component_ids=frozenset(member_ids),
        child_group_ids=frozenset(),
        exported_pins=frozenset(),
    )
    root = session.layout.groups[ROOT_GROUP_ID]
    session.layout.groups[ROOT_GROUP_ID] = ComponentGroup(
        id=ROOT_GROUP_ID,
        name=root.name,
        component_ids=root.component_ids - frozenset(member_ids),
        child_group_ids=root.child_group_ids | {grp_id},
        exported_pins=root.exported_pins,
    )
    session.invalidate_graph()
    return grp_id


def _net(session: EditorSession, *pins: tuple[uuid.UUID, int], name: str = "N"):
    from protoboard.domain.schematic.net import Net, PinRef

    net = Net(
        id=uuid.uuid4(),
        name=name,
        pins={PinRef(cid, pn) for cid, pn in pins},
    )
    session.schematic.nets[net.id] = net
    session.invalidate_graph()
    return net


# ---------------------------------------------------------------------------
# Axis state — filter / relation (from test_interaction_modes.TestAxesState)
# ---------------------------------------------------------------------------


class TestAxesState:
    def test_defaults_on_init(self, board_view: BoardView) -> None:
        tool = _v2(board_view)
        assert tool.filter == SelectionFilter.ALL
        assert tool.relation == RelationClass.SAME_LOGICAL_NET

    def test_set_relation_updates_state(self, board_view: BoardView) -> None:
        tool = _v2(board_view)
        tool.set_relation(RelationClass.NEIGHBORS)
        assert tool.relation == RelationClass.NEIGHBORS

    def test_backtick_cycles_relations(self, board_view: BoardView) -> None:
        tool = _v2(board_view)
        relations = list(RelationClass)
        start = relations.index(tool.relation)
        tool.on_key(
            key_inp(
                Qt.Key.Key_QuoteLeft,
                Qt.KeyboardModifier.NoModifier,
            )
        )
        assert tool.relation == relations[(start + 1) % len(relations)]

    def test_set_relation_emits_label(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        tool = _v2(board_view)
        received: list[str] = []
        session.highlight_mode_changed.connect(received.append)
        tool.set_relation(RelationClass.SAME_PHYSICAL_NET)
        assert received == [RELATION_LABELS[RelationClass.SAME_PHYSICAL_NET]]

    def test_set_filter_updates_state(self, board_view: BoardView) -> None:
        tool = _v2(board_view)
        f = SelectionFilter.COMPONENTS | SelectionFilter.GROUPS
        tool.set_filter(f)
        assert tool.filter == f


# ---------------------------------------------------------------------------
# Auto-highlight on a plain click (from TestLeftClickSemantics, adapted to v2's
# _apply_selection(Selectable, SetOp, BasisScope) dispatcher).
# ---------------------------------------------------------------------------


class TestAutoHighlightOnClick:
    def test_click_selects_only_clicked(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 0.5, 0.5)
        tool = _v2(board_view)
        board_view.refresh()
        _press(tool, 0.5, 0.5)
        assert tool._selected_id == inst.id

    def test_relation_none_no_auto_entry(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        _place(board_view, session, 0.5, 0.5)
        tool = _v2(board_view)
        tool.set_relation(RelationClass.NONE)
        board_view.refresh()
        _press(tool, 0.5, 0.5)
        # RELATION.NONE → no net context published; the auto entry is just the
        # clicked component (or empty), never a whole-net highlight.
        entry = session.highlight_stack.active()
        if entry is not None:
            assert entry.net_id is None

    def test_logical_net_click_highlights_whole_net(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        from protoboard.controller.selection.adapters import ComponentSelectable
        from protoboard.controller.selection_fsm import BasisScope, SetOp

        i1 = _place(board_view, session, 0.5, 0.5)
        i2 = _place(board_view, session, 3.5, 0.5)
        _net(session, (i1.id, 1), (i2.id, 1), name="N1")
        tool = _v2(board_view)
        tool.set_relation(RelationClass.SAME_LOGICAL_NET)
        board_view.refresh()

        pl = session.layout.placements[i1.id]
        tool._apply_selection(
            ComponentSelectable(component_id=i1.id, placement=pl),
            SetOp.SET,
            BasisScope.SINGLE,
        )
        entry = session.highlight_stack.active()
        assert entry is not None
        assert entry.auto is True
        assert entry.source_id == "select_tool"
        # Both net members highlighted (i1 clicked, i2 via the net).
        assert i1.id in entry.comp_ids and i2.id in entry.comp_ids


# ---------------------------------------------------------------------------
# Alt-LMB = select the relation basis (from TestAltLeftClickSemantics, adapted
# to _apply_selection(..., BasisScope.RELATION)).
# ---------------------------------------------------------------------------


class TestRelationBasisSelection:
    def test_relation_basis_selects_all_net_components(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        from protoboard.controller.selection.adapters import ComponentSelectable
        from protoboard.controller.selection_fsm import BasisScope, SetOp

        i1 = _place(board_view, session, 0.5, 0.5)
        i2 = _place(board_view, session, 3.5, 0.5)
        _net(session, (i1.id, 1), (i2.id, 1), name="N1")
        tool = _v2(board_view)
        tool.set_relation(RelationClass.SAME_LOGICAL_NET)
        board_view.refresh()

        pl = session.layout.placements[i1.id]
        tool._apply_selection(
            ComponentSelectable(component_id=i1.id, placement=pl),
            SetOp.SET,
            BasisScope.RELATION,
        )
        selected = set(tool._selected_ids)
        if tool._selected_id is not None:
            selected.add(tool._selected_id)
        assert i1.id in selected
        assert i2.id in selected

    def test_alt_click_on_already_selected_wire_expands_to_net(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """Select a wire, THEN Alt+click it → highlight expands to the whole net.

        Regression: the plain-SET on_press branch deferred a click on an
        already-selected object to a drag/release decision and never applied the
        RELATION basis, so 'click wire to select, Alt+click to extend' silently
        no-opped.  Alt+LMB now bypasses the defer and expands immediately.
        """
        import uuid

        from protoboard.domain.layout.wire import Wire
        from protoboard.domain.schematic.net import Net, PinRef
        from protoboard.geometry import Point

        i1 = _place(board_view, session, 0.5, 0.5)
        # Two wires sharing one net via the component pin.
        net = Net(id=uuid.uuid4(), name="N1", pins={PinRef(i1.id, 1)})
        session.schematic.nets[net.id] = net
        w1 = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(2.5, 0.5)],
            insulated=False,
            side="top",
        )
        w2 = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(0.5, 2.5)],
            insulated=False,
            side="top",
        )
        session.layout.wires.append(w1)
        session.layout.wires.append(w2)
        session.invalidate_graph()
        tool = _v2(board_view)
        tool.set_relation(RelationClass.SAME_LOGICAL_NET)
        board_view.refresh()

        # 1. Plain click selects only w1 (its midpoint).
        _press(tool, 1.5, 0.5)
        assert w1.id in tool._selected_wire_ids
        assert w2.id not in tool._selected_wire_ids  # not yet

        # 2. Alt+click the *already-selected* w1 → expands to the whole net.
        _press(tool, 1.5, 0.5, modifiers=Qt.KeyboardModifier.AltModifier)
        assert w1.id in tool._selected_wire_ids
        assert w2.id in tool._selected_wire_ids  # net expansion applied

    def test_ctrl_alt_accumulates_across_nets(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """Successive Ctrl+Alt (ADD + RELATION) clicks union *different* nets.

        Regression: the FSM add() only extended within one relation and toggled,
        so the second net's click dropped the first.  ADD now rebuilds a
        relation-agnostic union.
        """
        from protoboard.controller.selection.adapters import ComponentSelectable
        from protoboard.controller.selection_fsm import BasisScope, SetOp

        a = _place(board_view, session, 0.5, 0.5)
        b = _place(board_view, session, 2.5, 0.5)
        c = _place(board_view, session, 4.5, 0.5)
        d = _place(board_view, session, 6.5, 0.5)
        _net(session, (a.id, 1), (b.id, 1), name="NA")
        _net(session, (c.id, 1), (d.id, 1), name="NB")
        tool = _v2(board_view)
        tool.set_relation(RelationClass.SAME_LOGICAL_NET)
        board_view.refresh()

        def _sel(inst):  # type: ignore[no-untyped-def]
            return ComponentSelectable(
                component_id=inst.id, placement=session.layout.placements[inst.id]
            )

        tool._apply_selection(_sel(a), SetOp.ADD, BasisScope.RELATION)
        tool._apply_selection(_sel(c), SetOp.ADD, BasisScope.RELATION)
        sel = set(tool._selected_ids)
        # Both nets accumulated: A (a,b) ∪ B (c,d).
        assert {a.id, b.id, c.id, d.id} <= sel


class TestDoubleClickEnterGroup:
    """Double-clicking a group steps into it (legacy long-press affordance)."""

    def test_double_click_enters_group(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        from protoboard.domain.layout.layout import ROOT_GROUP_ID

        inst = _place(board_view, session, 2.5, 2.5)
        gid = _make_group(session, [inst.id])
        board_view.refresh()
        tool = _v2(board_view)
        assert tool._current_group_id == ROOT_GROUP_ID

        tool.on_double_press(Point(2.5, 2.5))
        assert tool._current_group_id == gid  # stepped inside

    def test_double_click_empty_space_no_enter(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        from protoboard.domain.layout.layout import ROOT_GROUP_ID

        inst = _place(board_view, session, 2.5, 2.5)
        _make_group(session, [inst.id])
        board_view.refresh()
        tool = _v2(board_view)
        tool.on_double_press(Point(9.0, 9.0))  # far from the group
        assert tool._current_group_id == ROOT_GROUP_ID  # stayed out


# ---------------------------------------------------------------------------
# HighlightStack precedence — pure stack/session, no tool internals.
# (from test_interaction_modes.TestHighlightStackPrecedence)
# ---------------------------------------------------------------------------


class TestHighlightStackPrecedence:
    def test_persistent_beats_auto(self, session: EditorSession) -> None:
        from protoboard.gui.highlight_state import HighlightEntry

        stack = session.highlight_stack
        stack.push_auto(HighlightEntry(label="auto"), source_id="select_tool")
        stack.push(HighlightEntry(label="user"))
        active = stack.active()
        assert active is not None and active.label == "user"
        assert active.auto is False and active.transient is False

    def test_transient_beats_auto(self, session: EditorSession) -> None:
        from protoboard.gui.highlight_state import HighlightEntry

        stack = session.highlight_stack
        stack.push_auto(HighlightEntry(label="auto"), source_id="select_tool")
        stack.push_transient(HighlightEntry(label="route"))
        active = stack.active()
        assert active is not None and active.label == "route"
        assert active.transient is True

    def test_persistent_beats_transient(self, session: EditorSession) -> None:
        from protoboard.gui.highlight_state import HighlightEntry

        stack = session.highlight_stack
        stack.push_transient(HighlightEntry(label="route"))
        stack.push(HighlightEntry(label="user"))
        active = stack.active()
        assert active is not None and active.label == "user"

    def test_push_auto_replaces_same_source(self, session: EditorSession) -> None:
        from protoboard.gui.highlight_state import HighlightEntry

        stack = session.highlight_stack
        stack.push_auto(HighlightEntry(label="first"), source_id="select_tool")
        stack.push_auto(HighlightEntry(label="second"), source_id="select_tool")
        active = stack.active()
        assert active is not None and active.label == "second"

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
        stack.clear()  # clears only the persistent tier
        active = stack.active()
        assert active is not None and active.label == "auto"


class TestRoutingNetUnion:
    def test_routing_net_propagates(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        i1 = _place(board_view, session, 0.5, 0.5)
        i2 = _place(board_view, session, 3.5, 0.5)
        net = _net(session, (i1.id, 1), (i2.id, 1), name="N1")
        board_view.refresh()
        session.set_routing_net(net.id)
        board_view.highlight.apply()
        assert session.routing_net_id == net.id
        session.set_routing_net(None)
        assert session.routing_net_id is None


# ---------------------------------------------------------------------------
# Grouped-component protection — select-allowed / mutate-blocked from outside
# the group, unlocked after entering.  (from test_grouped_component_protection,
# legacy _delete_selected/_delete_multi → v2 _delete_selection.)
# ---------------------------------------------------------------------------


class TestGroupedComponentProtection:
    def test_grouped_component_is_still_selectable(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 0.5, 0.5)
        _make_group(session, [inst.id])
        tool = _v2(board_view)
        board_view.refresh()
        _press(tool, 0.5, 0.5)
        # From outside the group the component (or its group) is selectable.
        assert (
            inst.id == tool._selected_id
            or inst.id in tool._selected_ids
            or tool._selected_group_id is not None
        )

    def test_delete_vetoed_for_grouped_from_outside(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 0.5, 0.5)
        _make_group(session, [inst.id])
        tool = _v2(board_view)
        tool._selected_ids = {inst.id}
        tool._delete_selection()
        assert inst.id in session.layout.placements  # mutation blocked

    def test_rotate_vetoed_for_grouped_from_outside(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 0.5, 0.5)
        _make_group(session, [inst.id])
        rot0 = session.layout.placements[inst.id].rotation
        tool = _v2(board_view)
        tool._selected_ids = {inst.id}
        tool._rotate_multi(delta=90)
        assert session.layout.placements[inst.id].rotation == rot0

    def test_multi_delete_filters_grouped(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        free = _place(board_view, session, 0.5, 0.5)
        grouped = _place(board_view, session, 1.5, 0.5)
        _make_group(session, [grouped.id])
        tool = _v2(board_view)
        tool._selected_ids = {free.id, grouped.id}
        tool._delete_selection()
        assert free.id not in session.layout.placements  # free deleted
        assert grouped.id in session.layout.placements  # grouped survives

    def test_delete_works_after_entering_group(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 0.5, 0.5)
        gid = _make_group(session, [inst.id])
        tool = _v2(board_view)
        tool._group_stack.append(gid)  # step inside (double-click flow)
        tool._selected_ids = {inst.id}
        tool._delete_selection()
        assert inst.id not in session.layout.placements  # now mutable


class TestGroupFlipEntry:
    """_flip_multi routes a selected group to FlipGroupCommand (rigid-body
    flip folded into group.transform), mirroring the RotateGroupCommand
    dispatch — not the per-component 2-pin flip loop."""

    def test_selected_group_flips_as_rigid_body(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        i1 = _place(board_view, session, 0.5, 0.5)
        i2 = _place(board_view, session, 2.5, 0.5)
        gid = _make_group(session, [i1.id, i2.id])
        tool = _v2(board_view)
        tool._selected_group_id = gid
        tool._flip_multi()
        # The flip folded a reflection (det = -1) into the group transform;
        # member placements are untouched (compose-on-read seam).
        t = session.layout.groups[gid].transform
        assert t.determinant() < 0

    def test_group_flip_is_undoable(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        from protoboard.geometry import Transform2D

        i1 = _place(board_view, session, 0.5, 0.5)
        gid = _make_group(session, [i1.id])
        tool = _v2(board_view)
        tool._selected_group_id = gid
        tool._flip_multi()
        assert session.layout.groups[gid].transform.determinant() < 0
        session.stack.undo(session)  # type: ignore[arg-type]
        assert session.layout.groups[gid].transform == Transform2D.identity()

    def test_loose_selection_still_uses_component_flip(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        # No group selected → the historical 2-pin per-component flip path
        # runs (no group transform is touched).
        i1 = _place(board_view, session, 0.5, 0.5)
        tool = _v2(board_view)
        tool._selected_ids = {i1.id}
        tool._flip_multi()
        root = session.layout.groups[ROOT_GROUP_ID]
        assert root.transform.determinant() > 0
