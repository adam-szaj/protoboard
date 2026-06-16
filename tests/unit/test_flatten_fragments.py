# SPDX-License-Identifier: MIT
"""Tests for the canonical ``flatten_fragments`` (wire.py).

This is the single home for fragment-path flattening — ``wire_utils`` and
``layout`` import it as ``_flatten_fragments``. The test pins the boundary
behaviour (shared via vertex dropped once) and that the aliases are identical.
"""

from __future__ import annotations

from protoboard.domain.layout.wire import Fragment, flatten_fragments
from protoboard.geometry import Point


def _frag(*pts: tuple[float, float], side: str = "top") -> Fragment:
    return Fragment(path=[Point(x, y) for x, y in pts], side=side)  # type: ignore[arg-type]


def test_empty_is_empty() -> None:
    assert flatten_fragments([]) == []


def test_single_fragment_is_its_path() -> None:
    f = _frag((0, 0), (1, 0))
    assert flatten_fragments([f]) == [Point(0, 0), Point(1, 0)]


def test_shared_via_vertex_dropped_once() -> None:
    # Fragment B starts at A's last point (the via on a plated-through pad).
    a = _frag((0, 0), (1, 0), side="top")
    b = _frag((1, 0), (2, 0), side="bottom")
    assert flatten_fragments([a, b]) == [Point(0, 0), Point(1, 0), Point(2, 0)]


def test_three_fragments_chain() -> None:
    a = _frag((0, 0), (1, 0), side="top")
    b = _frag((1, 0), (1, 1), side="bottom")
    c = _frag((1, 1), (2, 1), side="top")
    assert flatten_fragments([a, b, c]) == [
        Point(0, 0), Point(1, 0), Point(1, 1), Point(2, 1),
    ]


def test_aliases_are_the_same_object() -> None:
    # wire_utils and layout must reuse the one canonical function, not copies.
    from protoboard.domain.layout import layout as layout_mod
    from protoboard.domain.layout import wire_utils

    assert wire_utils._flatten_fragments is flatten_fragments
    assert layout_mod._flatten_fragments is flatten_fragments
