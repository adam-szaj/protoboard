# SPDX-License-Identifier: MIT
"""Pure context-menu option builders."""

from __future__ import annotations

import uuid

from protoboard.controller.selection.context_options import (
    build_highlight_options,
    build_net_selection_options,
)


def test_net_options_for_one_net_include_all_selection_scopes() -> None:
    net_id = uuid.uuid4()
    options = build_net_selection_options((net_id,), lambda _id: "VCC")

    assert [(option.action_id, option.label) for option in options] == [
        ("select-tool.ctx.select_net_components", "Components in net 'VCC'"),
        ("select-tool.ctx.select_net_wires", "Wires of net 'VCC'"),
        ("select-tool.ctx.select_net_all", "Wires + components of net 'VCC'"),
    ]
    assert all(option.group == "Select" for option in options)
    assert all(option.data == {"net_ids": (net_id,)} for option in options)


def test_net_options_for_multiple_nets_add_per_net_submenu_entries() -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    options = build_net_selection_options(
        (first, second), lambda net_id: "A" if net_id == first else "B"
    )

    assert [option.label for option in options] == [
        "Components in same nets",
        "Wires of same nets",
        "Wires + components of all nets",
        "A",
        "B",
    ]
    assert options[3].submenu == options[4].submenu == "Net…"
    assert options[3].data == {"net_ids": (first,)}
    assert options[4].data == {"net_ids": (second,)}


def test_net_options_for_empty_input_are_empty() -> None:
    assert build_net_selection_options((), lambda _id: "unused") == []


def test_highlight_options_reflect_persistent_state() -> None:
    active = build_highlight_options(True)
    inactive = build_highlight_options(False)

    assert (
        active[0].action_id
        == inactive[0].action_id
        == "select-tool.ctx.highlight_selection"
    )
    assert active[1].enabled is True
    assert inactive[1].enabled is False
    assert active[1].data == inactive[1].data == {"kind": "clear"}
