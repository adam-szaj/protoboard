# SPDX-License-Identifier: MIT
"""Qt-free construction of selection context-menu options."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence

from protoboard.controller.ports import ActionOption

__all__ = ["build_highlight_options", "build_net_selection_options"]


def build_net_selection_options(
    net_ids: Sequence[uuid.UUID], net_name: Callable[[uuid.UUID], str]
) -> list[ActionOption]:
    """Build options for selecting components and wires from logical nets."""
    if not net_ids:
        return []

    group = "Select"
    if len(net_ids) == 1:
        net_id = net_ids[0]
        name = net_name(net_id)
        return [
            ActionOption(
                action_id="select-tool.ctx.select_net_components",
                label=f"Components in net '{name}'",
                group=group,
                data={"net_ids": (net_id,)},
            ),
            ActionOption(
                action_id="select-tool.ctx.select_net_wires",
                label=f"Wires of net '{name}'",
                group=group,
                data={"net_ids": (net_id,)},
            ),
            ActionOption(
                action_id="select-tool.ctx.select_net_all",
                label=f"Wires + components of net '{name}'",
                group=group,
                data={"net_ids": (net_id,)},
            ),
        ]

    ids = tuple(net_ids)
    options = [
        ActionOption(
            action_id="select-tool.ctx.select_net_components",
            label="Components in same nets",
            group=group,
            data={"net_ids": ids},
        ),
        ActionOption(
            action_id="select-tool.ctx.select_net_wires",
            label="Wires of same nets",
            group=group,
            data={"net_ids": ids},
        ),
        ActionOption(
            action_id="select-tool.ctx.select_net_all",
            label="Wires + components of all nets",
            group=group,
            data={"net_ids": ids},
        ),
    ]
    options.extend(
        ActionOption(
            action_id="select-tool.ctx.select_net_all",
            label=net_name(net_id),
            group=group,
            submenu="Net…",
            data={"net_ids": (net_id,)},
        )
        for net_id in net_ids
    )
    return options


def build_highlight_options(has_persistent: bool) -> list[ActionOption]:
    """Build the persistent-highlight actions for a selection context."""
    return [
        ActionOption(
            action_id="select-tool.ctx.highlight_selection",
            label="Highlight selection (Dimmed #2)",
            group="Highlight",
            data={"kind": "push"},
        ),
        ActionOption(
            action_id="select-tool.ctx.clear_highlight",
            label="Clear highlight",
            group="Highlight",
            enabled=has_persistent,
            data={"kind": "clear"},
        ),
    ]
