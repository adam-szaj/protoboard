# SPDX-License-Identifier: MIT
"""Characterization of optimization trials and sequential fallback."""

from __future__ import annotations

import pickle
import threading
import uuid
from concurrent.futures import Future

import pytest

from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.wire import Wire
from protoboard.domain.schematic.net import Net
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from protoboard.services.routing import autorouter
from protoboard.services.routing.results import PassStats
from tests.unit.test_autorouter import _perfboard


def _outcome(index: int, wire_count: int, failed: list[str]):
    wires = [
        Wire(
            id=uuid.uuid4(),
            path=[Point(index + 0.5, 0.5), Point(index + 1.5, 0.5)],
            side="top",
        )
        for _ in range(wire_count)
    ]
    stats = PassStats(index, index, 1, len(failed), wire_count, 0, 0, 0)
    return wires, failed, stats


@pytest.fixture
def route_case(monkeypatch):
    failed = Net(id=uuid.uuid4(), name="failed", pins=set())
    schematic = Schematic(nets={failed.id: failed})
    calls: list[dict] = []
    outcomes = [_outcome(0, 1, ["failed"])]

    def run_pass(**kwargs):
        calls.append(kwargs)
        return outcomes.pop(0)

    monkeypatch.setattr(autorouter, "_run_one_pass", run_pass)
    monkeypatch.setattr(autorouter, "_SHORT_DETECTION_ENABLED", False)

    def route(**options):
        options.setdefault("max_optimize_iterations", 2)
        options.setdefault("simplify_all_after_autoroute", False)
        return autorouter.autoroute(
            _perfboard(3, 3), Layout(), schematic, {}, {}, **options
        )

    return route, calls, outcomes, failed


class FakePool:
    def __init__(self):
        self.futures: list[Future] = []
        self.submissions: list[tuple] = []
        self.shutdown_calls: list[tuple[bool, bool]] = []
        self.max_workers = 0

    def submit(self, function, args):
        index = len(self.submissions)
        self.submissions.append((function, args))
        return self.futures[index]

    def shutdown(self, *, wait, cancel_futures):
        self.shutdown_calls.append((wait, cancel_futures))

    def add_result(self, outcome):
        future = Future()
        future.set_result(outcome)
        self.futures.append(future)

    def add_error(self, exception):
        future = Future()
        future.set_exception(exception)
        self.futures.append(future)


@pytest.fixture
def pool(monkeypatch):
    pool = FakePool()

    def create(*, max_workers):
        pool.max_workers = max_workers
        return pool

    monkeypatch.setattr(autorouter, "ProcessPoolExecutor", create)
    monkeypatch.setattr(autorouter, "as_completed", iter)
    return pool


def test_parallel_ranking_keeps_last_tie_and_completion_stats(route_case, pool):
    route, calls, _, failed = route_case
    candidates = [
        _outcome(1, 1, []),
        _outcome(2, 2, []),
        _outcome(3, 2, []),
        _outcome(4, 1, []),
    ]
    for outcome in candidates:
        pool.add_result(outcome)
    progress: list[tuple[int, int]] = []
    result = route(
        max_optimize_iterations=4,
        optimize_progress_callback=lambda *p: progress.append(p),
    )

    assert result.wires == candidates[2][0]
    assert result.unrouted_nets == []
    assert [stats.pass_index for stats in result.pass_stats] == [0, 1, 2, 3, 4]
    assert progress == [(0, 4), (1, 4), (2, 4), (3, 4), (4, 4)]
    assert len(calls) == 1
    assert pool.shutdown_calls == [(False, True)]
    assert [args[-1] for _, args in pool.submissions] == [0, 1, 2, 3]
    # Run the submitted callable synchronously to verify the payload retains
    # the initially failed net IDs without inspecting its representation.
    route_case[2].append(_outcome(5, 0, ["failed"]))
    function, args = pool.submissions[0]
    function(args)
    assert calls[-1]["skip_net_ids"] == {failed.id}
    assert calls[-1].get("progress_callback") is None
    assert calls[-1].get("cancel_flag") is None


def test_future_failure_is_skipped_without_sequential_fallback(route_case, pool):
    route, calls, _, _ = route_case
    winner = _outcome(2, 2, [])
    pool.add_error(ValueError("trial failed"))
    pool.add_result(winner)
    progress: list[tuple[int, int]] = []
    result = route(optimize_progress_callback=lambda *p: progress.append(p))

    assert result.wires == winner[0]
    assert [stats.pass_index for stats in result.pass_stats] == [0, 2]
    assert len(calls) == 1
    assert progress == [(0, 2), (1, 2), (2, 2)]


def test_parallel_completion_order_controls_tie_and_stats(
    route_case, pool, monkeypatch
):
    route, _, _, _ = route_case
    first, second = _outcome(1, 2, []), _outcome(2, 2, [])
    pool.add_result(first)
    pool.add_result(second)
    monkeypatch.setattr(autorouter, "as_completed", lambda futures: reversed(futures))
    result = route()
    assert result.wires == first[0]
    assert [stats.pass_index for stats in result.pass_stats] == [0, 2, 1]


def test_cancel_propagates_and_tears_down_without_fallback(route_case, pool):
    route, calls, _, _ = route_case
    flag = threading.Event()
    pool.add_result(_outcome(1, 1, []))
    pool.add_result(_outcome(2, 1, []))

    def progress(done, total):
        if done == 0:
            flag.set()

    with pytest.raises(autorouter.AutorouteCancelled):
        route(cancel_flag=flag, optimize_progress_callback=progress)
    assert len(calls) == 1
    assert pool.shutdown_calls == [(False, True)]


def test_pool_failure_falls_back_preserving_progress_and_convergence(
    route_case, monkeypatch
):
    route, calls, outcomes, failed = route_case
    next_pass = _outcome(1, 1, ["failed"])
    outcomes.extend([next_pass, next_pass])

    def unavailable(**kwargs):
        raise RuntimeError("pool unavailable")

    monkeypatch.setattr(autorouter, "ProcessPoolExecutor", unavailable)

    def callback(*args):
        pass

    result = route(
        max_optimize_iterations=4, progress_callback=callback, push_and_shove=False
    )

    assert result.wires == next_pass[0]
    assert result.unrouted_nets == ["failed"]
    assert [stats.pass_index for stats in result.pass_stats] == [0, 1, 1]
    assert [call["pass_num"] for call in calls] == [0, 1, 2]
    assert calls[0]["push_and_shove"] is False
    for call in calls[1:]:
        assert call["skip_net_ids"] == {failed.id}
        assert call["progress_callback"] is callback
        assert call["n_nets"] == 1
        assert "push_and_shove" not in call


def test_partial_parallel_winner_survives_infrastructure_fallback(
    route_case, pool, monkeypatch
):
    route, _, outcomes, _ = route_case
    winner = _outcome(1, 3, [])
    pool.add_result(winner)
    pool.add_result(_outcome(2, 2, []))
    sequential = _outcome(3, 1, ["failed"])
    outcomes.extend([sequential, sequential])

    def completed_then_broken(futures):
        yield futures[0]
        raise RuntimeError("executor broke")

    monkeypatch.setattr(autorouter, "as_completed", completed_then_broken)
    result = route()
    assert result.wires == winner[0]
    assert result.unrouted_nets == []
    assert [stats.pass_index for stats in result.pass_stats] == [0, 1, 3, 3]
    assert pool.shutdown_calls == [(False, True)]


def test_cancel_before_sequential_fallback_propagates(route_case, monkeypatch):
    route, calls, _, _ = route_case
    flag = threading.Event()

    def unavailable(**kwargs):
        flag.set()
        raise RuntimeError("pool unavailable")

    monkeypatch.setattr(autorouter, "ProcessPoolExecutor", unavailable)
    with pytest.raises(autorouter.AutorouteCancelled):
        route(cancel_flag=flag)
    assert len(calls) == 1


def test_submitted_pass_payload_survives_pickle_roundtrip(route_case, pool):
    route, calls, outcomes, failed = route_case
    pool.add_result(_outcome(1, 1, []))
    route(max_optimize_iterations=1)
    function, args = pool.submissions[0]
    restored = pickle.loads(pickle.dumps(args))
    outcomes.append(_outcome(2, 0, ["failed"]))
    function(restored)

    assert [net.name for net in calls[-1]["sorted_nets"]] == ["failed"]
    assert calls[-1]["skip_net_ids"] == {failed.id}
    assert calls[-1].get("cancel_flag") is None
    assert calls[-1].get("progress_callback") is None
    assert calls[-1].get("pass_num", 0) == 0
    assert calls[-1].get("n_nets", 0) == 0
