# SPDX-License-Identifier: MIT
"""Tests for net_color.build_net_color_map."""

from __future__ import annotations

import uuid

from protoboard.domain.schematic.net import Net
from protoboard.gui.renderers.net_color import build_net_color_map
from protoboard.controller.theme import DEFAULT_THEME


def _make_net(name: str, color: str | None = None) -> Net:
    return Net(id=uuid.uuid4(), name=name, color=color)


def test_empty_nets_returns_empty_map() -> None:
    assert build_net_color_map({}, theme=DEFAULT_THEME) == {}


def test_manual_color_wins() -> None:
    net = _make_net("VCC", color="#FF8800")
    result = build_net_color_map({net.id: net}, theme=DEFAULT_THEME)
    assert result[net.id] == "#FF8800"


def test_first_n_nets_use_palette() -> None:
    n = len(DEFAULT_THEME.net_colors)
    nets = {n_obj.id: n_obj for n_obj in (_make_net(f"N{i}") for i in range(n))}
    result = build_net_color_map(nets, theme=DEFAULT_THEME)
    # Every net gets a colour from the palette; no duplicates.
    palette_set = set(DEFAULT_THEME.net_colors)
    assigned = set(result.values())
    assert assigned == palette_set
    assert len(result) == n


def test_overflow_nets_get_extended_palette() -> None:
    n = len(DEFAULT_THEME.net_colors)
    total = n + 10  # 10 nets past the palette
    nets = {nt.id: nt for nt in (_make_net(f"N{i:03d}") for i in range(total))}
    result = build_net_color_map(nets, theme=DEFAULT_THEME)
    assert len(result) == total
    # Overflow colours are unique within the overflow set.
    overflow_colors = [
        c for net_id, c in result.items() if c not in set(DEFAULT_THEME.net_colors)
    ]
    assert len(overflow_colors) == 10
    assert len(set(overflow_colors)) == 10


def test_assignment_is_stable_across_calls() -> None:
    nets = {nt.id: nt for nt in (_make_net(f"N{i:03d}") for i in range(20))}
    a = build_net_color_map(nets, theme=DEFAULT_THEME)
    b = build_net_color_map(nets, theme=DEFAULT_THEME)
    assert a == b
