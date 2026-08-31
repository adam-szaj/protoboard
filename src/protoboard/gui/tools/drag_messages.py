# SPDX-License-Identifier: MIT
"""User-facing messages for structured drag-conflict reasons."""

from __future__ import annotations

from collections.abc import Sequence
from typing import assert_never

from protoboard.domain.layout.drag_analysis import DragReason, DragReasonCode


def _format_drag_reason(reason: DragReason) -> str:
    match reason.code:
        case DragReasonCode.BODY_OVERLAP:
            return f"{reason.refdes} body overlap with {reason.related_refdes}"
        case DragReasonCode.DIFFERENT_NETS:
            return f"{reason.refdes} pin {reason.pin_number}: different-net conflict"
        case DragReasonCode.SHARED_PAD:
            return (
                f"{reason.refdes} pin {reason.pin_number}: "
                "shared pad (will join net)"
            )
        case DragReasonCode.SAME_NET_WIRE:
            return (
                f"{reason.refdes} pin {reason.pin_number}: on same-net wire "
                "(will remove — re-route through a separate hub)"
            )
        case DragReasonCode.FOREIGN_WIRE:
            return (
                f"{reason.refdes} pin {reason.pin_number}: "
                "on foreign wire (will remove)"
            )
        case DragReasonCode.ALIEN_PAD_CROSSING:
            return f"{reason.count} wire(s) cross alien pads (will remove)"
        case DragReasonCode.SKEWED_WIRE:
            return f"{reason.count} wire(s) became skewed (will remove)"
        case DragReasonCode.LOCKED_GROUP_WIRE:
            return f"{reason.count} locked-group wire pad(s) block the drop"
        case DragReasonCode.ROUTE_ALIEN_PAD_CROSSING:
            return "Cannot route: wire crosses a pad of another net"
        case _ as unreachable:
            assert_never(unreachable)


def format_drag_reasons(reasons: Sequence[DragReason]) -> str:
    return "; ".join(_format_drag_reason(reason) for reason in reasons)
