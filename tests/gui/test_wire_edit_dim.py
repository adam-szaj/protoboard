# SPDX-License-Identifier: MIT
"""Wire-edit dims the board (except the edited wire) on enter, restores on exit.

Task 3 (fragment-highlight plan): entering wire-edit pushes a transient
``HighlightEntry`` that dims everything except the edited wire; ``_exit`` pops
it.  The push must survive re-arm (flip/simplify/autoroute rebuild the armed
fragment via ``_commit_and_rearm`` without popping) — pushed exactly once per
logical edit session.

Uses the headless harness from ``test_wire_edit_tool.py`` (``_headless`` +
``_arm``) since it exercises ``WireEditTool.activate()``/``_exit()`` directly
without needing qtbot key/click plumbing.
"""
from __future__ import annotations

from protoboard.controller.selection.wire_edit import WireRange
from protoboard.gui.session import EditorSession
from tests.gui.test_wire_edit_tool import _arm, _detour_wire, _headless


def test_dim_pushed_on_enter(session: EditorSession) -> None:
    view = _headless(session)
    wire = _detour_wire(session)
    _arm(view, wire)

    active = session.highlight_stack.active()

    assert active is not None
    assert active.label == "wire-edit"
    assert wire.id in active.wire_ids  # edited wire exempt from dim


def test_dim_popped_on_exit(session: EditorSession) -> None:
    view = _headless(session)
    wire = _detour_wire(session)
    tool = _arm(view, wire)
    assert session.highlight_stack.active() is not None

    tool._exit()

    assert session.highlight_stack.active() is None


def test_dim_not_stacked_on_rearm(session: EditorSession) -> None:
    view = _headless(session)
    wire = _detour_wire(session)
    tool = _arm(view, wire)

    tool._flip_wire_fragment()  # commit + re-arm on the full span; activate() not re-run

    assert tool._wire_edit is not None  # stays armed (not exited)
    # Pop once -> empty. Proves the dim was pushed exactly once (no
    # double-push across the commit/re-arm handoff).
    session.highlight_stack.pop_transient()
    assert session.highlight_stack.active() is None


def test_dim_not_double_pushed_on_repick_reactivate(session: EditorSession) -> None:
    """activate() can re-fire on a pick->edit / re-pick->re-arm handoff
    (e.g. WireEditTool re-installed via ToolHost).  ``_dim_active`` must
    guard against a second push."""
    view = _headless(session)
    wire = _detour_wire(session)
    tool = _arm(view, wire)

    # Simulate a second activate() call on the SAME tool instance, as would
    # happen on a re-pick handoff that re-installs it via the ToolHost.
    tool.activate(session, view)

    session.highlight_stack.pop_transient()
    assert session.highlight_stack.active() is None


def test_dim_popped_on_deactivate_external_switch(session: EditorSession) -> None:
    """Guards a leak where switching tools via the toolbar / double-clicking a
    library part (``ToolHost.set_tool``) calls ``deactivate()`` directly --
    NOT ``_exit()`` -- leaving the transient wire-edit dim permanently stuck
    on the ``HighlightStack`` (board never brightens again).  ``deactivate()``
    must pop the dim itself."""
    view = _headless(session)
    wire = _detour_wire(session)
    tool = _arm(view, wire)
    assert session.highlight_stack.active() is not None

    tool.deactivate()  # exactly what ToolHost.set_tool does on an external switch

    assert session.highlight_stack.active() is None


def test_exit_then_deactivate_does_not_double_pop(session: EditorSession) -> None:
    """The internal exit route (_exit -> _pop_dim) followed by deactivate()'s
    own _pop_dim() must not double-pop / underflow the stack -- _pop_dim is
    idempotent via ``_dim_active``."""
    view = _headless(session)
    wire = _detour_wire(session)
    tool = _arm(view, wire)

    tool._exit()
    tool.deactivate()

    assert session.highlight_stack.active() is None
