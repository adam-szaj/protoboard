# SPDX-License-Identifier: MIT
"""Unit tests for the SelectTool selection filter (state + hit-test enforcement).

v2 holds the :class:`SelectionFilter` (state contract below) AND now enforces it:
``_filters()`` translates the held flags into the hit-test's per-kind gate, so a
component-only filter excludes wire/group hits, etc.
"""

from protoboard.gui.selection_axes import SelectionFilter
from protoboard.gui.tools.gui_tools import SelectTool


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


# -- enforcement: _filters() gates which kinds the hit-test may return ---------


def test_filters_all_admits_every_kind() -> None:
    f = SelectTool()._filters()
    assert f.components and f.wires and f.groups


def test_filters_components_only_excludes_wires_and_groups() -> None:
    t = SelectTool()
    t.set_filter(SelectionFilter.COMPONENTS)
    f = t._filters()
    assert f.components and not f.wires and not f.groups


def test_filters_wires_only_excludes_components() -> None:
    t = SelectTool()
    t.set_filter(SelectionFilter.WIRES)
    f = t._filters()
    assert f.wires and not f.components and not f.groups


def test_filters_none_admits_nothing() -> None:
    t = SelectTool()
    t.set_filter(SelectionFilter.NONE)
    f = t._filters()
    assert not f.components and not f.wires and not f.groups
