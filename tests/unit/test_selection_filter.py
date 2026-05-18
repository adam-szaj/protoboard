# SPDX-License-Identifier: MIT
"""Unit tests for SelectTool selection filter state (no Qt required)."""

from protoboard.gui.selection_axes import SelectionFilter
from protoboard.gui.tools.select_tool import SelectTool


def test_filter_defaults_to_all() -> None:
    t = SelectTool()
    assert t.filter == SelectionFilter.ALL
    assert t._filter_components() is True
    assert t._filter_wires() is True
    assert t._filter_groups() is True


def test_set_filter_components_only() -> None:
    t = SelectTool()
    t.set_filter(SelectionFilter.COMPONENTS)
    assert t._filter_components() is True
    assert t._filter_wires() is False
    assert t._filter_groups() is False


def test_set_filter_drop_wires() -> None:
    t = SelectTool()
    t.set_filter(SelectionFilter.COMPONENTS | SelectionFilter.GROUPS)
    assert t._filter_wires() is False
    assert t._filter_components() is True
    assert t._filter_groups() is True


def test_set_filter_none_then_restore() -> None:
    t = SelectTool()
    t.set_filter(SelectionFilter.NONE)
    assert t.filter == SelectionFilter.NONE
    t.set_filter(SelectionFilter.ALL)
    assert t.filter == SelectionFilter.ALL
