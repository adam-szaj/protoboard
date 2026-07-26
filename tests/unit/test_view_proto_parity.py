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


def _headless_session():
    """A minimal Qt-free session (empty library) for member-coverage checks."""
    from protoboard.controller.editing.headless_session import HeadlessSession
    from protoboard.domain.board.board import Board
    from protoboard.domain.board.boxes import (
        BoardGrid,
        PadAtom,
        PadLayers,
        SizePolicy,
    )
    from protoboard.services.library.loader import ComponentLibrary

    grid = BoardGrid(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        ),
        rows=3,
        cols=3,
        row_gap=0.0,
        col_gap=0.0,
    )
    return HeadlessSession(board=Board(root=grid), library=ComponentLibrary())


def test_headless_session_covers_edit_state() -> None:
    # EditState (controller/editing/controller.py) is the state contract every
    # EditController verb + tool reads. The Qt-free HeadlessSession is its base
    # implementation, so it must expose every EditState member — otherwise a
    # headless driver hits an AttributeError the GUI session masks. Check a live
    # instance: several members are __init__-assigned attrs, invisible on the class.
    from protoboard.controller.editing.controller import EditState

    required = _protocol_members(EditState)
    available = set(dir(_headless_session()))
    missing = required - available
    assert not missing, (
        "HeadlessSession is missing EditState members (drift): "
        f"{sorted(missing)}"
    )


def test_preview_state_covers_edit_state() -> None:
    # PreviewState is a read-through EditState proxy; growing EditState means it
    # must delegate the new members too, else a preview-mode drag hits an
    # AttributeError. Guard it against the same drift.
    from protoboard.controller.editing.controller import EditState
    from protoboard.controller.editing.preview_state import PreviewState

    live = _headless_session()
    preview = PreviewState(live, live.layout)  # type: ignore[arg-type]
    required = _protocol_members(EditState)
    available = set(dir(preview))
    missing = required - available
    assert not missing, (
        "PreviewState is missing EditState members (drift): "
        f"{sorted(missing)}"
    )
