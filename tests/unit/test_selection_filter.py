# SPDX-License-Identifier: MIT
"""Unit tests for the SelectTool selection-filter *state* shim (no Qt required).

v2 holds the :class:`SelectionFilter` and mirrors it to the status-bar widget,
but does not yet *enforce* it in the hit-test (deferred — see ``docs/todo.md``).
These tests pin the state contract (``filter`` / ``set_filter``); enforcement
tests arrive with the enforcement work.
"""

from protoboard.gui.selection_axes import SelectionFilter
from protoboard.gui.tools.select_tool import SelectTool


def test_filter_defaults_to_all() -> None:
    assert SelectTool().filter == SelectionFilter.ALL


def test_set_filter_components_only() -> None:
    t = SelectTool()
    t.set_filter(SelectionFilter.COMPONENTS)
    assert t.filter == SelectionFilter.COMPONENTS


def test_set_filter_drop_wires() -> None:
    t = SelectTool()
    f = SelectionFilter.COMPONENTS | SelectionFilter.GROUPS
    t.set_filter(f)
    assert t.filter == f
    assert not (t.filter & SelectionFilter.WIRES)


def test_set_filter_none_then_restore() -> None:
    t = SelectTool()
    t.set_filter(SelectionFilter.NONE)
    assert t.filter == SelectionFilter.NONE
    t.set_filter(SelectionFilter.ALL)
    assert t.filter == SelectionFilter.ALL
