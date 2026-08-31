# SPDX-License-Identifier: MIT
from __future__ import annotations

from protoboard.controller.tools.route_tool import RouteTool


def test_route_tool_routes_option_changes_and_status_to_callbacks() -> None:
    insulated: list[bool] = []
    sides: list[str] = []
    statuses: list[tuple[bool, str]] = []
    tool = RouteTool(
        set_insulated=insulated.append,
        set_side=sides.append,
        update_status=lambda value, side: statuses.append((value, side)),
    )

    tool._set_insulated(True)
    tool._set_side("top")
    tool._notify_status()

    assert insulated == [True]
    assert sides == ["top"]
    assert statuses == [(True, "top")]
