# SPDX-License-Identifier: MIT
"""DRC task ownership, supersession and shutdown across Qt signals."""

from __future__ import annotations

import gc
import threading
import weakref

import pytest
from PySide6.QtCore import SIGNAL, QThread, Signal
from PySide6.QtTest import QSignalSpy

from protoboard.gui import drc_tasks
from protoboard.gui.drc_tasks import DRCTasks


class FakeWorker(QThread):
    finished = Signal(list)
    error = Signal(str)
    exited = Signal()

    def __init__(self, **snapshots):
        super().__init__()
        self.snapshots = snapshots
        self.running = False
        self.cancelled = False
        self.waits: list[int] = []

    def start(self):
        self.running = True

    def isRunning(self):
        return self.running

    def cancel(self):
        self.cancelled = True

    def wait(self, timeout):
        self.waits.append(timeout)
        self.running = False
        return True

    def finish_thread(self):
        self.running = False
        self.emit(SIGNAL("finished()"))


@pytest.fixture
def fake_workers(monkeypatch):
    workers: list[FakeWorker] = []

    def create(**snapshots):
        worker = FakeWorker(**snapshots)
        workers.append(worker)
        return worker

    monkeypatch.setattr(drc_tasks, "DRCWorker", create)
    return workers


def test_superseded_worker_cannot_publish_results_or_errors(
    qtbot, session, fake_workers
) -> None:
    tasks = DRCTasks()
    results = QSignalSpy(tasks.finished)
    errors = QSignalSpy(tasks.error)
    tasks.run(session)
    previous = fake_workers[0]
    tasks.run(session)
    current = fake_workers[1]

    assert previous.cancelled
    assert previous.waits == []
    previous.finished.emit([])
    previous.error.emit("stale")
    assert results.count() == errors.count() == 0
    current.finished.emit([])
    assert results.count() == 1
    current.error.emit("already finished")
    assert errors.count() == 0
    previous.finish_thread()
    current.finish_thread()
    qtbot.waitUntil(lambda: not tasks._workers)


def test_result_and_exited_keep_worker_alive_until_thread_finishes(
    qtbot, session, fake_workers
) -> None:
    tasks = DRCTasks()
    tasks.run(session)
    worker = fake_workers.pop()
    ref = weakref.ref(worker)
    worker.finished.emit([])
    worker.exited.emit()
    del worker
    gc.collect()
    qtbot.wait(1)
    assert ref() is not None
    assert ref().isRunning()

    ref().finish_thread()
    qtbot.waitUntil(lambda: not tasks._workers)
    gc.collect()
    assert ref() is None


def test_shutdown_cancels_and_waits_for_active_and_superseded_workers(
    qtbot, session, fake_workers
) -> None:
    tasks = DRCTasks()
    results = QSignalSpy(tasks.finished)
    tasks.run(session)
    tasks.run(session)
    previous, current = fake_workers
    tasks.shutdown()

    for worker in (previous, current):
        assert worker.cancelled
        assert worker.waits == [2000]
        worker.finished.emit([])
    assert results.count() == 0
    assert not tasks._workers
    tasks.run(session)
    assert len(fake_workers) == 2


def test_current_error_is_forwarded_once(qtbot, session, fake_workers) -> None:
    tasks = DRCTasks()
    errors = QSignalSpy(tasks.error)
    tasks.run(session)
    worker = fake_workers[0]
    worker.error.emit("boom")
    worker.error.emit("duplicate")
    assert errors.count() == 1
    assert errors.at(0) == ["boom"]
    assert worker in tasks._workers
    worker.finish_thread()
    qtbot.waitUntil(lambda: not tasks._workers)


def test_real_worker_publishes_and_drains(qtbot, session) -> None:
    tasks = DRCTasks()
    try:
        with qtbot.waitSignal(tasks.finished, timeout=5000) as result:
            tasks.run(session)
        assert isinstance(result.args[0], list)
        qtbot.waitUntil(lambda: not tasks._workers)
    finally:
        tasks.shutdown()


def test_real_thread_exited_signal_does_not_end_retention(
    qtbot, session, monkeypatch
) -> None:
    release = threading.Event()

    class PausedWorker(drc_tasks.DRCWorker):
        def run(self):
            self.finished.emit([])
            self.exited.emit()
            release.wait(5)

    monkeypatch.setattr(drc_tasks, "DRCWorker", PausedWorker)
    tasks = DRCTasks()
    try:
        with qtbot.waitSignal(tasks.finished, timeout=5000):
            tasks.run(session)
        worker = next(iter(tasks._workers))
        qtbot.wait(1)
        assert worker.isRunning()
        assert worker in tasks._workers
        release.set()
        qtbot.waitUntil(lambda: not tasks._workers)
        assert not worker.isRunning()
    finally:
        release.set()
        tasks.shutdown()


def test_shutdown_timeout_retains_worker_until_thread_finishes(
    qtbot, session, fake_workers, monkeypatch
) -> None:
    tasks = DRCTasks()
    results = QSignalSpy(tasks.finished)
    tasks.run(session)
    worker = fake_workers[0]
    monkeypatch.setattr(worker, "wait", lambda timeout: False)
    tasks.shutdown()

    assert worker.cancelled
    assert worker in tasks._workers
    worker.finished.emit([])
    assert results.count() == 0
    worker.finish_thread()
    qtbot.waitUntil(lambda: not tasks._workers)


def test_main_window_applies_results_and_presents_status(
    qtbot, session, fake_workers
) -> None:
    from protoboard.controller.app_settings import AppSettings
    from protoboard.controller.ports.drc import DRCIssue
    from protoboard.gui.main_window import MainWindow

    window = MainWindow(session, AppSettings())
    qtbot.addWidget(window)
    window._drc_timer.stop()
    window._run_drc()
    assert window.statusBar().currentMessage() == "DRC running…"
    issue = DRCIssue("warning", "example", "Example issue", [], [])
    worker = fake_workers[0]
    worker.finished.emit([issue])
    assert session.drc_issues == [issue]
    assert window.statusBar().currentMessage() == "DRC: 1 issue"
    worker.finish_thread()

    window._run_drc_silent()
    worker = fake_workers[1]
    worker.error.emit("boom")
    assert window.statusBar().currentMessage() == "DRC failed: boom"
    assert session.drc_issues == [issue]
    worker.finish_thread()
    qtbot.waitUntil(lambda: not window._drc_tasks._workers)
