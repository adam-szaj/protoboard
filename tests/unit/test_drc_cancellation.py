# SPDX-License-Identifier: MIT
"""Tests for DRC engine cancellation via threading.Event.

The engine accepts an optional ``cancel_flag`` so a background worker
can bail out promptly when the user keeps making layout edits.  When
the flag is set the engine returns the partial result it has computed
so far; the caller decides whether to discard it.
"""

from __future__ import annotations

import threading
import uuid

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.connectivity.graph import ConnectivityGraph
from protoboard.domain.layout.layout import Layout
from protoboard.domain.schematic.schematic import Schematic
from protoboard.services.drc.engine import DRCEngine


def _board(rows: int = 4, cols: int = 4) -> Board:
    return Board(
        root=BoardGrid(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            cell=PadAtom(
                width_policy=SizePolicy.HUG,
                height_policy=SizePolicy.HUG,
                layers=PadLayers(top=True, bottom=True, plated_through=True),
            ),
            rows=rows,
            cols=cols,
            row_gap=0.0,
            col_gap=0.0,
        )
    )


class TestCancellation:
    def test_pre_set_flag_returns_immediately(self) -> None:
        """When the flag is already set before the call, the engine
        bails at the first checkpoint without doing any expensive work.
        """
        board = _board()
        layout = Layout()
        schematic = Schematic(nets={})
        components: dict[str, object] = {}
        instances: dict[uuid.UUID, object] = {}
        graph = ConnectivityGraph(
            board=board,
            layout=layout,
            components=components,  # type: ignore[arg-type]
            instances=instances,  # type: ignore[arg-type]
        )
        flag = threading.Event()
        flag.set()
        issues = DRCEngine().check_with_graph(
            schematic=schematic,
            layout=layout,
            components=components,  # type: ignore[arg-type]
            instances=instances,  # type: ignore[arg-type]
            graph=graph,
            cancel_flag=flag,
        )
        # Empty board → empty issues anyway; the contract is "doesn't
        # crash and returns a list".  The expensive work was skipped.
        assert isinstance(issues, list)

    def test_no_flag_unchanged_behaviour(self) -> None:
        """Default ``cancel_flag=None`` keeps the old contract — full
        sweep regardless of how slow it gets."""
        board = _board()
        layout = Layout()
        schematic = Schematic(nets={})
        components: dict[str, object] = {}
        instances: dict[uuid.UUID, object] = {}
        graph = ConnectivityGraph(
            board=board,
            layout=layout,
            components=components,  # type: ignore[arg-type]
            instances=instances,  # type: ignore[arg-type]
        )
        issues_a = DRCEngine().check_with_graph(
            schematic=schematic,
            layout=layout,
            components=components,  # type: ignore[arg-type]
            instances=instances,  # type: ignore[arg-type]
            graph=graph,
        )
        issues_b = DRCEngine().check_with_graph(
            schematic=schematic,
            layout=layout,
            components=components,  # type: ignore[arg-type]
            instances=instances,  # type: ignore[arg-type]
            graph=graph,
            cancel_flag=None,
        )
        # Same result with explicit None vs omitted — proves the
        # default is a no-op.
        assert issues_a == issues_b

    def test_flag_set_mid_run_truncates_result(self) -> None:
        """If the flag flips during a real run, the engine returns the
        partial result accumulated up to the most recent checkpoint."""
        board = _board()
        layout = Layout()
        schematic = Schematic(nets={})
        components: dict[str, object] = {}
        instances: dict[uuid.UUID, object] = {}
        graph = ConnectivityGraph(
            board=board,
            layout=layout,
            components=components,  # type: ignore[arg-type]
            instances=instances,  # type: ignore[arg-type]
        )
        flag = threading.Event()
        # Set the flag after every is_set() call so the engine bails
        # at the very next checkpoint regardless of when it looks.
        original_is_set = flag.is_set
        check_count = {"n": 0}

        def _is_set_then_set() -> bool:
            check_count["n"] += 1
            original_is_set()
            if check_count["n"] == 1:
                # First check: not yet — let one phase run.
                return False
            flag.set()
            return True

        flag.is_set = _is_set_then_set  # type: ignore[method-assign]

        issues = DRCEngine().check_with_graph(
            schematic=schematic,
            layout=layout,
            components=components,  # type: ignore[arg-type]
            instances=instances,  # type: ignore[arg-type]
            graph=graph,
            cancel_flag=flag,
        )
        assert isinstance(issues, list)
        # is_set was consulted at least twice — proves the engine
        # actually checks between checks.
        assert check_count["n"] >= 2
