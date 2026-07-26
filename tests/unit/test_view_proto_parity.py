# SPDX-License-Identifier: MIT
"""Parity guard: the gui view surface satisfies the controller's ``_ViewProto``.

``controller.tools.base._ViewProto`` re-declares (in the Qt-free controller
layer) exactly the view members the controller tools call, because the
controller must not import ``gui`` (see ``test_controller_no_gui_import``).
The real ``gui.board_view_tool.AbstractBoardView`` (which the concrete
``BoardView`` implements) must therefore expose every one of those members —
otherwise a tool typed against ``_ViewProto`` would call a method the live view
does not have. This test fails loudly if the two drift apart.
"""
from __future__ import annotations

import inspect

from protoboard.controller.tools.base import _ToolHostProto, _ViewProto
from protoboard.gui.board_view_tool import AbstractBoardView


def _protocol_members(proto: type) -> set[str]:
    """Public member names a Protocol declares (methods + annotated attrs).

    Excludes anything underscore-prefixed — that drops both dunders and the
    Protocol machinery (``_is_protocol``, ``_abc_impl``) while keeping the
    real declared surface.
    """
    members = {name for name in vars(proto) if not name.startswith("_")}
    members |= {
        name
        for name in getattr(proto, "__annotations__", {})
        if not name.startswith("_")
    }
    return members


def test_abstract_board_view_covers_view_proto() -> None:
    required = _protocol_members(_ViewProto)
    available = set(dir(AbstractBoardView)) | _protocol_members(AbstractBoardView)
    missing = required - available
    assert not missing, (
        "AbstractBoardView is missing members the controller's _ViewProto "
        f"requires (drift): {sorted(missing)}"
    )


def test_tool_host_proto_covered_by_abstract_view_tools() -> None:
    # ``_ViewProto.tools`` is typed ``_ToolHostProto``; the gui side declares
    # ``AbstractBoardView.tools: ToolHost`` — assert ToolHost carries every
    # member the controller drives through _ToolHostProto.
    from protoboard.gui.tool_host import ToolHost

    required = _protocol_members(_ToolHostProto)
    available = {name for name, _ in inspect.getmembers(ToolHost)}
    missing = required - available
    assert not missing, (
        "ToolHost is missing members the controller's _ToolHostProto "
        f"requires (drift): {sorted(missing)}"
    )
