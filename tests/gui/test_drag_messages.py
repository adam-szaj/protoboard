# SPDX-License-Identifier: MIT
"""Presentation mapping for structured drag-conflict reasons."""

from __future__ import annotations

import pytest

from protoboard.domain.layout.drag_analysis import DragReason, DragReasonCode


@pytest.mark.parametrize(
    ("reason", "expected"),
    (
        (
            DragReason(
                DragReasonCode.BODY_OVERLAP,
                refdes="R1",
                related_refdes="C1",
            ),
            "R1 body overlap with C1",
        ),
        (
            DragReason(DragReasonCode.DIFFERENT_NETS, refdes="R1", pin_number=2),
            "R1 pin 2: different-net conflict",
        ),
        (
            DragReason(DragReasonCode.SHARED_PAD, refdes="R1", pin_number=2),
            "R1 pin 2: shared pad (will join net)",
        ),
        (
            DragReason(DragReasonCode.SAME_NET_WIRE, refdes="R1", pin_number=2),
            "R1 pin 2: on same-net wire "
            "(will remove — re-route through a separate hub)",
        ),
        (
            DragReason(DragReasonCode.FOREIGN_WIRE, refdes="R1", pin_number=2),
            "R1 pin 2: on foreign wire (will remove)",
        ),
        (
            DragReason(DragReasonCode.ALIEN_PAD_CROSSING, count=3),
            "3 wire(s) cross alien pads (will remove)",
        ),
        (
            DragReason(DragReasonCode.SKEWED_WIRE, count=2),
            "2 wire(s) became skewed (will remove)",
        ),
        (
            DragReason(DragReasonCode.LOCKED_GROUP_WIRE, count=1),
            "1 locked-group wire pad(s) block the drop",
        ),
        (
            DragReason(DragReasonCode.ROUTE_ALIEN_PAD_CROSSING),
            "Cannot route: wire crosses a pad of another net",
        ),
    ),
)
def test_drag_reason_has_one_user_facing_message(
    reason: DragReason, expected: str
) -> None:
    from protoboard.gui.tools.drag_messages import format_drag_reasons

    assert format_drag_reasons((reason,)) == expected


def test_drag_reason_messages_keep_detector_order() -> None:
    from protoboard.gui.tools.drag_messages import format_drag_reasons

    reasons = (
        DragReason(DragReasonCode.SHARED_PAD, refdes="U1", pin_number=4),
        DragReason(DragReasonCode.SKEWED_WIRE, count=2),
    )

    assert format_drag_reasons(reasons) == (
        "U1 pin 4: shared pad (will join net); " "2 wire(s) became skewed (will remove)"
    )
