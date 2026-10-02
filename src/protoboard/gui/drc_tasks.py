# SPDX-License-Identifier: MIT
"""Ownership and cancellation of background DRC runs."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from PySide6.QtCore import SIGNAL, SLOT, QObject, Qt, Signal, Slot

from protoboard.controller.ports.drc import DRCIssue
from protoboard.gui.workers.drc_worker import DRCWorker
from protoboard.services.routing.snapshot import LayoutSnapshot, SchematicSnapshot

if TYPE_CHECKING:
    from protoboard.gui.session import EditorSession


class DRCTasks(QObject):
    """Publish only the latest run and keep threads alive until they finish."""

    finished = Signal(list)
    error = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._active: DRCWorker | None = None
        self._workers: set[DRCWorker] = set()
        self._shutting_down = False

    def run(self, session: EditorSession) -> None:
        """Snapshot the session and supersede the active run without waiting."""
        if self._shutting_down:
            return
        previous = self._active
        if previous is not None and previous.isRunning():
            previous.cancel()

        worker = DRCWorker(
            board=session.board,
            layout_snap=LayoutSnapshot.freeze(session.layout),
            schematic_snap=SchematicSnapshot.freeze(session.schematic),
            components=dict(session.components),
            instances=dict(session.instances),
            axis_labels=session.axis_label_settings,
        )
        worker.finished.connect(self._on_result)
        worker.error.connect(self._on_error)
        # DRCWorker.finished(list) shadows QThread.finished() in Python.
        # Connect the inherited Qt signal explicitly: worker.exited is emitted
        # in run()'s finally, before the thread has actually stopped.
        QObject.connect(
            worker,
            SIGNAL("finished()"),
            self,
            SLOT("_on_thread_finished()"),
            Qt.ConnectionType.QueuedConnection,
        )
        self._workers.add(worker)
        self._active = worker
        worker.start()

    @Slot(list)
    def _on_result(self, issues: list[DRCIssue]) -> None:
        worker = self.sender()
        if worker is not self._active or worker is None:
            return
        self._active = None
        self.finished.emit(issues)

    @Slot(str)
    def _on_error(self, message: str) -> None:
        worker = self.sender()
        if worker is not self._active or worker is None:
            return
        self._active = None
        self.error.emit(message)

    @Slot()
    def _on_thread_finished(self) -> None:
        worker = cast(DRCWorker, self.sender())
        if worker is self._active:
            self._active = None
        self._workers.discard(worker)

    def shutdown(self) -> None:
        """Cancel all workers and wait up to two seconds for each thread."""
        self._shutting_down = True
        self._active = None
        workers = list(self._workers)
        for worker in workers:
            try:
                worker.cancel()
            except Exception:
                pass
        for worker in workers:
            try:
                if worker.wait(2000):
                    self._workers.discard(worker)
            except Exception:
                pass
