# SPDX-License-Identifier: MIT
"""Editing helpers depend on focused read contexts, not orchestration state."""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).parents[2]
_HELPERS = (
    "src/protoboard/controller/editing/group_lock.py",
    "src/protoboard/controller/editing/wire_nets.py",
    "src/protoboard/controller/editing/placement_conflict.py",
    "src/protoboard/controller/editing/drag_commit.py",
    "src/protoboard/controller/selection/queries.py",
    "src/protoboard/controller/selection/wire_ops.py",
    "src/protoboard/controller/tools/simplify_tool.py",
)


def test_editing_helpers_do_not_depend_on_composite_context() -> None:
    offenders: list[str] = []

    for relative_path in _HELPERS:
        tree = ast.parse(
            (_ROOT / relative_path).read_text(encoding="utf-8"),
            filename=relative_path,
        )
        if any(
            isinstance(node, ast.Name) and node.id == "EditingContext"
            for node in ast.walk(tree)
        ):
            offenders.append(relative_path)

    assert not offenders, (
        "editing helpers still depend on the composite EditingContext: " f"{offenders}"
    )
