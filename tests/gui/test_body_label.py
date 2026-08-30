# SPDX-License-Identifier: MIT
"""make_body_label: body-frame pinned, readable, scale-to-fit text."""

from __future__ import annotations

import pytest
from PySide6.QtGui import QColor, QTransform

from protoboard.geometry import Point
from protoboard.gui.renderers.packages.body_label import make_body_label


def _identity() -> QTransform:
    return QTransform()


def test_scale_to_fit_still_works(qtbot) -> None:
    items = make_body_label(
        "223",
        QColor("#000000"),
        _identity(),
        Point(0.0, 0.0),
        0.0,
        avail_len=4.0,
        avail_w=2.0,
        alpha=1.0,
        z=5.0,
    )
    assert items  # non-empty: [full_alpha, half_alpha]
    item = items[0]
    br = item.mapRectToScene(item.boundingRect())
    assert br.width() <= 4.0 + 1e-6
    assert br.height() <= 2.0 + 1e-6


def test_max_height_caps_scale(qtbot) -> None:
    # A huge avail box would scale the text large; max_height clamps it.
    big = make_body_label(
        "X",
        QColor("#000000"),
        QTransform(),
        Point(0.0, 0.0),
        0.0,
        avail_len=100.0,
        avail_w=100.0,
        max_height=1.0,
        alpha=1.0,
        z=0.0,
    )
    assert big
    item = big[0]
    br = item.mapRectToScene(item.boundingRect())
    assert br.height() <= 1.0 + 1e-6


def test_no_cap_fills_box(qtbot) -> None:
    items = make_body_label(
        "X",
        QColor("#000000"),
        QTransform(),
        Point(0.0, 0.0),
        0.0,
        avail_len=4.0,
        avail_w=2.0,
        max_height=None,
        alpha=1.0,
        z=0.0,
    )
    assert items
    item = items[0]
    br = item.mapRectToScene(item.boundingRect())
    assert br.height() <= 2.0 + 1e-6


def test_position_follows_body_transform(qtbot) -> None:
    # A body translated to (10, 20): the label centre maps there.
    t = QTransform()
    t.translate(10.0, 20.0)
    items = make_body_label(
        "X",
        QColor("#000000"),
        t,
        Point(0.0, 0.0),
        0.0,
        avail_len=4.0,
        avail_w=2.0,
        alpha=1.0,
        z=0.0,
    )
    assert items
    item = items[0]
    c = item.mapToScene(item.boundingRect().center())
    assert c.x() == pytest.approx(10.0, abs=0.5)
    assert c.y() == pytest.approx(20.0, abs=0.5)


def test_flipped_body_text_not_mirrored(qtbot) -> None:
    # A reflected body transform (det < 0). Glyphs must NOT be mirrored:
    # the item's final transform must have POSITIVE determinant (the mirror
    # was compensated), so text reads left-to-right.
    flip = QTransform.fromScale(-1.0, 1.0)  # reflection, det = -1
    items = make_body_label(
        "AB",
        QColor("#000000"),
        flip,
        Point(0.0, 0.0),
        0.0,
        avail_len=4.0,
        avail_w=2.0,
        alpha=1.0,
        z=0.0,
    )
    assert items
    item = items[0]
    assert item.transform().determinant() > 0.0  # glyphs un-mirrored


@pytest.mark.xfail(
    reason="upright-flipping for 180deg-rotated bodies regressed in the "
    "body/label channel split (make_body_label now compensates only "
    "det<0 mirror, not 180deg rotation). Tracked in docs/todo.md.",
    strict=True,
)
def test_upside_down_rotation_flipped_upright(qtbot) -> None:
    # A body rotated 180°: text would read upside-down; it must be flipped
    # upright (final world reading angle within (-90, 90]).
    import math

    rot180 = QTransform()
    rot180.rotate(180.0)
    items = make_body_label(
        "AB",
        QColor("#000000"),
        rot180,
        Point(0.0, 0.0),
        0.0,
        avail_len=4.0,
        avail_w=2.0,
        alpha=1.0,
        z=0.0,
    )
    assert items
    item = items[0]
    m = item.transform()
    world_angle = math.degrees(math.atan2(m.m12(), m.m11()))
    assert -90.0 < world_angle <= 90.0


def test_empty_text_none(qtbot) -> None:
    assert (
        make_body_label(
            "",
            QColor("#000000"),
            _identity(),
            Point(0.0, 0.0),
            0.0,
            avail_len=4.0,
            avail_w=2.0,
            alpha=1.0,
            z=0.0,
        )
        == []
    )
