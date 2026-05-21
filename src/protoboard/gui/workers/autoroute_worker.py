# SPDX-License-Identifier: MIT
"""Background QThread worker for the autorouter."""

from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QThread, Signal

from protoboard.services.routing.snapshot import RouteRequest

if TYPE_CHECKING:
    from protoboard.domain.board.board import Board
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.layout.layout import Layout
    from protoboard.domain.routing_settings import RoutingSettings
    from protoboard.domain.schematic.schematic import Schematic
    from protoboard.services.routing.autorouter import AutorouteResult


def _build_request(
    *,
    board: "Board | None",
    layout: "Layout | None",
    schematic: "Schematic | None",
    components: "dict[str, ComponentDefinition] | None",
    instances: "dict[uuid.UUID, ComponentInstance] | None",
    routing_settings: "RoutingSettings | None",
    net_ids: set[uuid.UUID] | None,
    project_path: Path | None,
) -> RouteRequest:
    """Snapshot legacy keyword-args into a RouteRequest on the caller's thread."""
    if (
        board is None or layout is None or schematic is None
        or components is None or instances is None
        or routing_settings is None
    ):
        raise TypeError(
            "AutorouteWorker requires either `request` or the full set of "
            "(board, layout, schematic, components, instances, routing_settings)"
        )
    return RouteRequest.from_session(
        board=board,
        layout=layout,
        schematic=schematic,
        components=components,
        instances=instances,
        routing_settings=routing_settings,
        net_ids=net_ids,
        report_path=(
            project_path / "autoroute-report.csv"
            if project_path is not None else None
        ),
    )


class AutorouteWorker(QThread):
    """Runs :func:`autoroute` in a background thread.

    The worker operates on an immutable ``RouteRequest`` snapshot built on the
    main thread *before* the worker starts.  This eliminates the data race
    that previously existed between the GUI thread mutating ``session.layout``
    / ``session.schematic`` and the worker reading them concurrently.

    Signals
    -------
    progress(pass_num, net_name, pct)
        Emitted at the start of each net being routed.
        ``pct`` is 0–100 across the current pass.
    finished(result)
        Emitted on successful completion with an :class:`AutorouteResult`.
    error(message)
        Emitted if an unexpected exception occurs.
    """

    progress: Signal = Signal(int, str, int)   # pass_num, net_name, pct
    optimize_progress: Signal = Signal(int, int)  # done, total
    finished: Signal = Signal(object)          # AutorouteResult
    error: Signal = Signal(str)

    def __init__(
        self,
        *,
        request: RouteRequest | None = None,
        router_name: str | None = None,
        # Legacy keyword-args kept for callers that still build a worker
        # from live session state.  When `request` is None they are
        # snapshotted into one here on the *caller's* thread (i.e. the
        # main thread, since __init__ is invoked before .start()).
        board: "Board | None" = None,
        layout: "Layout | None" = None,
        schematic: "Schematic | None" = None,
        components: "dict[str, ComponentDefinition] | None" = None,
        instances: "dict[uuid.UUID, ComponentInstance] | None" = None,
        routing_settings: "RoutingSettings | None" = None,
        net_ids: set[uuid.UUID] | None = None,
        project_path: Path | None = None,
    ) -> None:
        super().__init__()
        self._request: RouteRequest = request if request is not None else _build_request(
            board=board,
            layout=layout,
            schematic=schematic,
            components=components,
            instances=instances,
            routing_settings=routing_settings,
            net_ids=net_ids,
            project_path=project_path,
        )
        self._router_name = router_name
        self._cancel_flag = threading.Event()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def cancel(self) -> None:
        """Signal the worker to stop at the next cancellation checkpoint."""
        self._cancel_flag.set()

    # ------------------------------------------------------------------
    # QThread
    # ------------------------------------------------------------------

    def run(self) -> None:
        import threading as _t
        import structlog as _sl
        from protoboard.services.routing.autorouter import (
            AutorouteCancelled,
            AutorouteResult,
        )
        # Importing builtin ensures the default routers are registered even
        # when no plugin loader has run yet.
        import protoboard.services.routing.builtin  # noqa: F401  side-effect
        from protoboard.services.routing.protocol import ProgressSink, registry

        _sl.get_logger(__name__).info(
            "autoroute_worker.run",
            thread_id=_t.get_ident(),
            is_main=_t.current_thread() is _t.main_thread(),
            router=self._router_name or "<default>",
        )

        sink = ProgressSink(
            progress=self._report,
            optimize_progress=self._report_optimize,
            cancel_flag=self._cancel_flag,
        )

        try:
            router = (
                registry.get(self._router_name)
                if self._router_name is not None
                else registry.default()
            )
            result: AutorouteResult = router.route(self._request, sink)
        except AutorouteCancelled:
            self.finished.emit(AutorouteResult())
            return
        except Exception as exc:  # noqa: BLE001
            self.error.emit(str(exc))
            return

        self.finished.emit(result)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _report(self, pass_num: int, net_name: str, pct: int) -> None:
        """Called from the worker thread — emits cross-thread signal."""
        self.progress.emit(pass_num, net_name, pct)

    def _report_optimize(self, done: int, total: int) -> None:
        """Called from the worker thread after each optimization pass completes."""
        self.optimize_progress.emit(done, total)
