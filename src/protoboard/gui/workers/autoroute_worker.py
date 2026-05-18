# SPDX-License-Identifier: MIT
"""Background QThread worker for the autorouter."""

from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QThread, Signal

if TYPE_CHECKING:
    from protoboard.domain.board.board import Board
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.layout.layout import Layout
    from protoboard.domain.routing_settings import RoutingSettings
    from protoboard.domain.schematic.schematic import Schematic
    from protoboard.services.routing.autorouter import AutorouteResult


class AutorouteWorker(QThread):
    """Runs :func:`autoroute` in a background thread.

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
        board: Board,
        layout: Layout,
        schematic: Schematic,
        components: dict[str, ComponentDefinition],
        instances: dict[uuid.UUID, ComponentInstance],
        routing_settings: RoutingSettings,
        net_ids: set[uuid.UUID] | None = None,
        project_path: Path | None = None,
    ) -> None:
        super().__init__()
        # Snapshot read-only inputs — the worker thread must not touch the
        # live session while routing.
        self._board = board
        self._layout = layout
        self._schematic = schematic
        self._components = components
        self._instances = instances
        self._rs = routing_settings
        self._net_ids = net_ids
        self._project_path = project_path
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
        _sl.get_logger(__name__).info(
            "autoroute_worker.run",
            thread_id=_t.get_ident(),
            is_main=_t.current_thread() is _t.main_thread(),
        )
        from protoboard.services.routing.autorouter import autoroute, AutorouteCancelled

        try:
            rs = self._rs
            report_path = (
                self._project_path / "autoroute-report.csv"
                if self._project_path is not None else None
            )
            result: AutorouteResult = autoroute(
                board=self._board,
                layout=self._layout,
                schematic=self._schematic,
                components=self._components,
                instances=self._instances,
                cost_model=rs.cost_model(),
                preferred_side=rs.preferred_side,
                allow_layer_change=rs.allow_layer_change,
                max_rip_up_passes=rs.max_rip_up_passes,
                max_optimize_iterations=rs.max_optimize_iterations,
                wire_mode=rs.wire_mode,
                max_detour_factor=rs.max_detour_factor,
                progress_callback=self._report,
                optimize_progress_callback=self._report_optimize,
                cancel_flag=self._cancel_flag,
                net_ids=self._net_ids,
                report_path=report_path,
                push_and_shove=rs.push_and_shove,
                enable_simplify_pass=rs.enable_simplify_pass,
                simplify_mode=rs.simplify_mode,
            )
        except AutorouteCancelled:
            # Return empty result — caller interprets as user cancellation.
            from protoboard.services.routing.autorouter import AutorouteResult
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
