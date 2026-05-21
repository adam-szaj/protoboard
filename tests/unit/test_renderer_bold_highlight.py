# SPDX-License-Identifier: MIT
"""Tier-1 (bold) highlight: pen widths in fingerprints + multiplier flow."""

from __future__ import annotations

import uuid

from protoboard.domain.layout.wire import WireSegment
from protoboard.geometry import Point
from protoboard.gui.renderers.wire_renderer import wire_fingerprint
from protoboard.gui.renderers.component_renderer import comp_fingerprint
from protoboard.domain.layout.placement import ComponentPlacement


def _wire(side: str = "top") -> WireSegment:
    return WireSegment(
        id=uuid.uuid4(),
        path=[Point(0.5, 0.5), Point(1.5, 0.5)],
        insulated=True,
        side=side,  # type: ignore[arg-type]
    )


def _placement(cid: uuid.UUID) -> ComponentPlacement:
    return ComponentPlacement(
        component_id=cid, position=Point(0.5, 0.5), rotation=0, side="top",
    )


class TestWireFingerprintBold:
    def test_bold_changes_fingerprint(self) -> None:
        w = _wire()
        nid = uuid.uuid4()
        plain = wire_fingerprint(w, nid, None, 0.0, 1.0, is_bold=False)
        bold = wire_fingerprint(w, nid, None, 0.0, 1.0, is_bold=True)
        assert plain != bold, "is_bold must enter the fingerprint to force rebuild"

    def test_default_is_not_bold(self) -> None:
        w = _wire()
        nid = uuid.uuid4()
        fp_default = wire_fingerprint(w, nid, None, 0.0, 1.0)
        fp_explicit = wire_fingerprint(w, nid, None, 0.0, 1.0, is_bold=False)
        assert fp_default == fp_explicit


class TestCompFingerprintBold:
    def test_bold_pin_nets_changes_fingerprint(self) -> None:
        cid = uuid.uuid4()
        nid = uuid.uuid4()
        placement = _placement(cid)
        layers_key = ("top", "normal", False, "full", "full", "full", "full")
        plain = comp_fingerprint(
            placement, "R", {1: nid}, None, layers_key,
        )
        bold = comp_fingerprint(
            placement, "R", {1: nid}, None, layers_key,
            bold_pin_nets=frozenset({nid}),
        )
        assert plain != bold

    def test_different_bold_nets_yield_different_fingerprints(self) -> None:
        """Switching which net is highlighted between two refreshes must
        rebuild the group, so the fingerprint must distinguish bold nets."""
        cid = uuid.uuid4()
        n1, n2 = uuid.uuid4(), uuid.uuid4()
        placement = _placement(cid)
        layers_key = ("top", "normal", False, "full", "full", "full", "full")
        fp1 = comp_fingerprint(
            placement, "R", {1: n1}, None, layers_key,
            bold_pin_nets=frozenset({n1}),
        )
        fp2 = comp_fingerprint(
            placement, "R", {1: n1}, None, layers_key,
            bold_pin_nets=frozenset({n2}),
        )
        assert fp1 != fp2


