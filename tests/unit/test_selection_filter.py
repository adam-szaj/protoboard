# SPDX-License-Identifier: MIT
"""Unit tests for SelectTool selection filter state (no Qt required)."""

from protoboard.gui.tools.select_tool import SelectTool


def test_filter_defaults_all_true() -> None:
    t = SelectTool()
    assert t._filter_components is True
    assert t._filter_wires is True
    assert t._filter_groups is True


def test_set_filter_components() -> None:
    t = SelectTool()
    t.set_filter_components(False)
    assert t._filter_components is False
    t.set_filter_components(True)
    assert t._filter_components is True


def test_set_filter_wires() -> None:
    t = SelectTool()
    t.set_filter_wires(False)
    assert t._filter_wires is False
    t.set_filter_wires(True)
    assert t._filter_wires is True


def test_set_filter_groups() -> None:
    t = SelectTool()
    t.set_filter_groups(False)
    assert t._filter_groups is False
    t.set_filter_groups(True)
    assert t._filter_groups is True
