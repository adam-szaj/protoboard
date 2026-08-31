# SPDX-License-Identifier: MIT
"""Structural contracts for editing-session capabilities."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, PadLayers, SizePolicy
from protoboard.headless_session import HeadlessSession
from protoboard.services.library.loader import ComponentLibrary


def _headless_session() -> HeadlessSession:
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


def test_headless_session_satisfies_editing_contexts() -> None:
    try:
        contexts = importlib.import_module("protoboard.controller.editing.contexts")
    except ModuleNotFoundError:
        pytest.fail("editing context protocols are not defined")

    session = _headless_session()

    assert isinstance(session, contexts.ProjectModel)
    assert isinstance(session, contexts.SpatialQueries)
    assert isinstance(session, contexts.ConnectivityQueries)
    assert isinstance(session, contexts.EditingEffects)
    assert isinstance(session, contexts.InteractionState)
    assert isinstance(session, contexts.EditingContext)


def test_legacy_edit_state_contract_is_removed() -> None:
    legacy_name = "Edit" + "State"
    editing_dir = Path(__file__).parents[2] / "src/protoboard/controller/editing"
    class_names = {
        node.name
        for path in editing_dir.glob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.ClassDef)
    }
    editing = importlib.import_module("protoboard.controller.editing")

    assert legacy_name not in class_names
    assert not hasattr(editing, legacy_name)
