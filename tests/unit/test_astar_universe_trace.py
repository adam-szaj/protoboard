# SPDX-License-Identifier: MIT
"""Characterize universe trace events and worker accounting at each exit boundary."""

from __future__ import annotations

import io
import itertools
import json
from pathlib import Path
from queue import Queue

import pytest

from protoboard.domain.routing.cost_model import RoutingCostModel
from protoboard.geometry import Point
from protoboard.services.routing import astar


class _Trace(io.StringIO):
    def __init__(self, fail_event: str | None = None) -> None:
        super().__init__()
        self.fail_event = fail_event
        self.close_count = 0

    def write(self, text: str) -> int:
        if json.loads(text)["e"] == self.fail_event:
            raise OSError("trace writer failed")
        return super().write(text)

    def close(self) -> None:
        # Retain the buffer so tests can inspect the events after worker close.
        self.close_count += 1


def _run_worker(
    shared: astar._SharedState,
    *,
    target: Point = Point(2.0, 0.0),
    nodes_expanded: int = 0,
) -> None:
    source = Point(0.0, 0.0)
    heap = [
        (
            0.0,
            0.0,
            0,
            source.x,
            source.y,
            0,
            0.0,
            0.0,
            (None, (source, "top")),
            0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
        )
    ]
    astar._universe_worker(
        heap=heap,
        visited={},
        nodes_expanded=nodes_expanded,
        seq_counter=0,
        shared=shared,
        valid_nodes={"top": {source, Point(1.0, 0.0), Point(2.0, 0.0)}},
        target_set={target},
        target_coords=[(target.x, target.y)],
        cost_model=RoutingCostModel(),
        insulated=True,
        occupied={},
        same_net={},
        near_pins=set(),
        near_pin_cost=0.0,
        bare_idx={},
        vias=set(),
        forbidden_edges=set(),
        penalised={},
    )


def test_trace_file_records_initial_steps_and_goal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(astar, "_TRACE_DIR", tmp_path)
    monkeypatch.setattr(astar, "_TRACE_COUNTER", itertools.count())
    shared = astar._SharedState(result_queue=Queue(), active_count=1)
    _run_worker(shared)
    files = list(tmp_path.glob("astar_*_000000.jsonl"))
    assert len(files) == 1
    events = [json.loads(line) for line in files[0].read_text().splitlines()]
    assert events[0] == {
        "e": "init",
        "insulated": True,
        "source_parity": -1,
        "bare_diagonal_mode": False,
        "n_targets": 1,
        "n_valid_top": 3,
        "n_valid_bottom": 0,
        "n_occupied": 0,
        "n_same_net": 0,
        "n_bare_segs": 0,
        "first_target": [2.0, 0.0],
    }
    step = events[1]
    assert step["e"] == "step"
    assert step["pos"] == [0.0, 0.0, "top"]
    assert step["g"] == 0.0
    assert step["from_dir"] == [0.0, 0.0]
    assert step["queued"] == 1
    assert step["via"] is False
    assert step["nodes_expanded"] == 1
    assert [entry[2] for entry in step["neighbours"]].count("off_grid") == 7
    assert events[-1]["e"] == "goal"
    result = shared.result_queue.get_nowait()
    assert events[-1]["path"] == [[p.x, p.y, s] for p, s in result.path]
    assert events[-1]["g"] == result.cost
    assert shared.active_count == 0
    assert shared.total_nodes_expanded == result.nodes_expanded


@pytest.mark.parametrize("exit_reason", ["goal", "heap_empty", "over_budget"])
def test_trace_exit_lifecycle_and_worker_accounting(
    monkeypatch: pytest.MonkeyPatch, exit_reason: str
) -> None:
    trace = _Trace()
    monkeypatch.setattr(astar, "_open_trace", lambda: trace)
    shared = astar._SharedState(result_queue=Queue(), active_count=1)
    target = Point(2.0, 0.0) if exit_reason == "goal" else Point(3.0, 0.0)
    if exit_reason == "over_budget":
        shared.first_found_nodes = 1
        shared.extra_budget_ratio = 0.0
    _run_worker(
        shared, target=target, nodes_expanded=2 if exit_reason == "over_budget" else 0
    )
    events = [json.loads(line) for line in trace.getvalue().splitlines()]
    last = events[-1]
    if exit_reason == "goal":
        assert last["e"] == "goal"
    else:
        assert last["e"] == "end"
        assert last["reason"] == exit_reason
    # Budget exits currently leave the trace open; preserve this refactor's scope.
    assert trace.close_count == (0 if exit_reason == "over_budget" else 1)
    assert shared.active_count == 0
    assert shared.total_nodes_expanded == last["nodes_expanded"]


@pytest.mark.parametrize("fail_event", ["init", "step", "goal", "end"])
def test_trace_writer_error_preserves_original_accounting_boundary(
    monkeypatch: pytest.MonkeyPatch, fail_event: str
) -> None:
    trace = _Trace(fail_event)
    monkeypatch.setattr(astar, "_open_trace", lambda: trace)
    shared = astar._SharedState(result_queue=Queue(), active_count=1)
    with pytest.raises(OSError, match="trace writer failed"):
        _run_worker(shared, target=Point(3.0 if fail_event == "end" else 2.0, 0.0))
    assert shared.active_count == (1 if fail_event == "init" else 0)
    assert (shared.total_nodes_expanded == 0) is (fail_event == "init")
    assert trace.close_count == 0


def test_tracing_disabled_still_finishes_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(astar, "_TRACE_DIR", None)
    assert astar._open_trace() is None
    shared = astar._SharedState(result_queue=Queue(), active_count=1)
    _run_worker(shared)
    assert shared.active_count == 0
    assert not shared.result_queue.empty()
