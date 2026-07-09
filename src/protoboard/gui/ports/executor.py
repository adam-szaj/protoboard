# SPDX-License-Identifier: MIT
"""``ExecutorPort`` — blocking background jobs behind the seam (qt-ports §3).

SelectTool's heavy wire operations (simplify / unweave) run a pure function
in a worker thread while a modal progress dialog blocks the GUI.  The port
keeps the *contract* — block until done, ``None`` means cancelled or errored
— and hides the mechanism (QThread + QProgressDialog + QEventLoop, see
``gui/workers/operation_worker.py``), so the flows run headless.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True)
class BlockingJob:
    """One blocking background job.

    ``fn`` runs off the GUI thread: called as ``fn()`` when
    ``with_progress`` is False, or ``fn(progress_cb)`` — with
    ``progress_cb(current: int, total: int)`` — when True.  It must only
    touch the read-only inputs it closed over; the caller applies the
    returned result on the GUI thread.
    """

    title: str
    label: str
    fn: Callable[..., object]
    with_progress: bool = False


class ExecutorPort(Protocol):
    """Run a blocking job to completion.

    Returns ``fn``'s result, or ``None`` when the user cancelled or the
    job raised — the exact contract of ``run_with_progress``.
    """

    def run_blocking(self, job: BlockingJob) -> object | None: ...
