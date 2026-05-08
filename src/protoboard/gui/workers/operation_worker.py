# SPDX-License-Identifier: MIT
"""Generic background worker + progress dialog for blocking placement operations.

Usage (busy bar, no progress)::

    result = run_with_progress(
        parent=self,
        title="Spread",
        fn=lambda: spread_components(ids, session),
    )
    if result is None:
        return  # cancelled or error

Usage (with progress, 0–100)::

    result = run_with_progress(
        parent=self,
        title="Hug",
        fn=lambda cb: hug_components(ids, state, progress_callback=cb),
        with_progress=True,
    )

When *with_progress=True* the callable receives a single positional argument:
a ``progress_callback(current: int, total: int)`` function that updates the
dialog.  The callable signature must be ``Callable[[Callable[[int, int],
None]], Any]``.

:func:`run_with_progress` blocks (via a local event loop) until the worker
finishes or the user cancels.  Returns the callable's return value on success,
or ``None`` on cancellation / error.
"""

from __future__ import annotations

import traceback
from typing import Any, Callable

from PySide6.QtCore import QEventLoop, QThread, Signal
from PySide6.QtWidgets import QProgressDialog, QWidget


# ---------------------------------------------------------------------------
# Worker thread
# ---------------------------------------------------------------------------


class _OperationWorker(QThread):
    """Runs an arbitrary callable in a background thread.

    If *with_progress* is True the callable is called as ``fn(progress_cb)``
    where ``progress_cb(current, total)`` emits the ``progress`` signal
    cross-thread.
    """

    succeeded: Signal = Signal(object)      # result value
    failed: Signal = Signal(str)            # traceback string
    progress: Signal = Signal(int, int)     # current, total

    def __init__(
        self,
        fn: Callable[..., Any],
        with_progress: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._fn = fn
        self._with_progress = with_progress

    def _report(self, current: int, total: int) -> None:
        self.progress.emit(current, total)

    def run(self) -> None:
        try:
            if self._with_progress:
                result = self._fn(self._report)
            else:
                result = self._fn()
        except Exception:  # noqa: BLE001
            self.failed.emit(traceback.format_exc())
            return
        self.succeeded.emit(result)


# ---------------------------------------------------------------------------
# Public helper
# ---------------------------------------------------------------------------


def run_with_progress(
    parent: QWidget,
    title: str,
    fn: Callable[..., Any],
    label: str = "Working…",
    with_progress: bool = False,
) -> Any | None:
    """Run *fn* in a background thread with a progress dialog.

    Parameters
    ----------
    parent:
        Owner widget for the dialog.
    title:
        Dialog window title (e.g. ``"Hug"``).
    fn:
        Callable to run in the background thread.
        - Without progress: ``fn()`` — no arguments.
        - With progress: ``fn(callback)`` — receives ``callback(current, total)``.
    label:
        Text shown inside the dialog.
    with_progress:
        If True, show a determinate progress bar (0–100 %).
        *fn* must accept a single ``callback(current, total)`` argument.

    Returns
    -------
    The return value of *fn* on success, or ``None`` if the user cancelled
    or an exception occurred.
    """
    loop = QEventLoop()
    result_holder: list[Any] = []
    error_holder: list[str] = []
    cancelled = [False]

    worker = _OperationWorker(fn, with_progress=with_progress, parent=parent)

    # Busy bar by default; switch to 0–100 when progress signals arrive.
    dlg = QProgressDialog(label, "Cancel", 0, 0, parent)
    dlg.setWindowTitle(title)
    dlg.setMinimumDuration(300)
    dlg.setAutoClose(False)
    dlg.setAutoReset(False)

    def _on_progress(current: int, total: int) -> None:
        if total > 0:
            if dlg.maximum() == 0:
                dlg.setMaximum(100)
            dlg.setValue(round(current * 100 / total))

    def _on_success(value: Any) -> None:
        result_holder.append(value)
        loop.quit()

    def _on_failure(tb: str) -> None:
        error_holder.append(tb)
        loop.quit()

    def _on_cancel() -> None:
        cancelled[0] = True
        loop.quit()

    worker.progress.connect(_on_progress)
    worker.succeeded.connect(_on_success)
    worker.failed.connect(_on_failure)
    dlg.canceled.connect(_on_cancel)

    worker.start()
    loop.exec()

    dlg.close()
    worker.wait()

    if error_holder:
        import structlog
        structlog.get_logger(__name__).error(
            "operation_worker.error", title=title, tb=error_holder[0]
        )
        return None

    if cancelled[0] or not result_holder:
        return None

    return result_holder[0]
