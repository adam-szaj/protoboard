# SPDX-License-Identifier: MIT
from __future__ import annotations

import uuid

from protoboard.controller.tools.select_tool import SelectTool


def test_select_tool_routes_parameterized_menu_actions_to_callbacks() -> None:
    component_id = uuid.uuid4()
    assigned: list[tuple[uuid.UUID, int]] = []
    autorouted: list[bool] = []
    tool = SelectTool(
        assign_pin=lambda cid, pin: assigned.append((cid, pin)),
        autoroute_group=lambda: autorouted.append(True),
    )

    tool._menu_assign_pin(component_id, 7)
    tool._menu_autoroute_group()

    assert assigned == [(component_id, 7)]
    assert autorouted == [True]
