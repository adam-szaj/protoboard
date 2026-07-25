# SPDX-License-Identifier: MIT
"""The wire-edit overlay draws a bright span polyline over the picked range."""
from __future__ import annotations

from protoboard.geometry import Point


def test_show_wire_edit_wire_draws_span_items(qtbot) -> None:
    from PySide6.QtWidgets import QGraphicsScene

    from protoboard.gui.tools.select_preview import _WireEditOverlay

    scene = QGraphicsScene()
    ov = _WireEditOverlay(lambda: scene)
    path = [Point(0, 0), Point(1, 0), Point(2, 0), Point(3, 0)]
    # No span -> today's behavior: cyan segs (3) + dots (4) = 7 items.
    ov.show_wire(path, marked={0, 3})
    n_no_span = len(ov.items)
    ov.clear()
    # With a span over the middle two vertices -> extra bright polyline items.
    ov.show_wire(path, marked={1, 2}, span=[Point(1, 0), Point(2, 0)])
    assert len(ov.items) > n_no_span, "span should add bright polyline items"


def test_show_wire_edit_wire_empty_span_matches_legacy(qtbot) -> None:
    from PySide6.QtWidgets import QGraphicsScene

    from protoboard.gui.tools.select_preview import _WireEditOverlay

    scene = QGraphicsScene()
    ov = _WireEditOverlay(lambda: scene)
    path = [Point(0, 0), Point(1, 0)]
    ov.show_wire(path, marked=set())  # span defaults to []
    # 1 cyan seg + 2 dots = 3 items, no span polyline.
    assert len(ov.items) == 3
