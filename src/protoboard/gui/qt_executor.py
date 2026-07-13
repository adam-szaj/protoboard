# SPDX-License-Identifier: MIT
"""``QtExecutor`` — Qt adapter of :class:`ExecutorPort` (qt-ports §3).

Wraps :func:`~protoboard.gui.workers.operation_worker.run_with_progress`
(QThread + QProgressDialog + local QEventLoop).  The adapter holds the
dialog's parent widget — constructed by BoardView with itself — so callers
behind the seam never touch a QWidget.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

    from protoboard.controller.ports.executor import BlockingJob


class QtExecutor:
    """ExecutorPort over the modal progress-dialog worker harness."""

    def __init__(self, parent: QWidget) -> None:
        self._parent = parent

    def run_blocking(self, job: BlockingJob) -> object | None:
        from protoboard.gui.workers.operation_worker import run_with_progress

        return run_with_progress(
            parent=self._parent,
            title=job.title,
            fn=job.fn,
            label=job.label,
            with_progress=job.with_progress,
        )
