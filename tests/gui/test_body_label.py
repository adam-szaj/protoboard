# SPDX-License-Identifier: MIT
"""make_body_label: scaled, centred, alpha-multiplied text item."""

from __future__ import annotations

import pytest
from PySide6.QtGui import QColor

from protoboard.geometry import Point
from protoboard.gui.renderers.packages.body_label import make_body_label


def test_returns_item_scaled_within_bounds(qtbot) -> None:
    item = make_body_label(
        "223", QColor("#000000"), Point(0.0, 0.0),
        angle_deg=0.0, avail_len=4.0, avail_w=2.0, alpha=1.0, z=5.0,
    )
    assert item is not None
    br = item.mapRectToScene(item.boundingRect())
    assert br.width() <= 4.0 + 1e-6
    assert br.height() <= 2.0 + 1e-6


def test_alpha_multiplies_ink(qtbot) -> None:
    item = make_body_label(
        "223", QColor("#ffffff"), Point(0.0, 0.0),
        angle_deg=0.0, avail_len=4.0, avail_w=2.0, alpha=0.5, z=5.0,
    )
    assert item is not None
    # QColor.setAlphaF stores alpha with limited (16-bit) precision, so the
    # round-tripped value is not bit-exact — compare with a tight tolerance.
    assert item.brush().color().alphaF() == pytest.approx(0.5, abs=1e-4)


def test_empty_text_is_none(qtbot) -> None:
    assert make_body_label(
        "", QColor("#000000"), Point(0.0, 0.0),
        angle_deg=0.0, avail_len=4.0, avail_w=2.0, alpha=1.0, z=5.0,
    ) is None
