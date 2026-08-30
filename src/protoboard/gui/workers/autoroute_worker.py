# SPDX-License-Identifier: MIT
"""Background QThread worker for the autorouter."""

from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

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
    board: Board | None,
    layout: Layout | None,
    schematic: Schematic | None,
    components: dict[str, ComponentDefinition] | None,
    instances: dict[uuid.UUID, ComponentInstance] | None,
    routing_settings: RoutingSettings | None,
    net_ids: set[uuid.UUID] | None,
    project_path: Path | None,
) -> RouteRequest:
    """Snapshot legacy keyword-args into a RouteRequest on the caller's thread."""
    if (
        board is None
        or layout is None
        or schematic is None
        or components is None
        or instances is None
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
            project_path / "autoroute-report.csv" if project_path is not None else None
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

    progress: Signal = Signal(int, str, int)  # pass_num, net_name, pct
    optimize_progress: Signal = Signal(int, int)  # done, total
    # iter_num, max_iter, n_simplified_this_iter — per iteration of the
    # post-autoroute simplify-all fixed-point loop.  Lets MainWindow show
    # a meaningful countdown instead of a frozen-looking progress dialog.
    simplify_progress: Signal = Signal(int, int, int)
    finished: ClassVar[Signal] = Signal(
        object
    )  # AutorouteResult — overrides QThread.finished
    error: Signal = Signal(str)

    def __init__(
        self,
        *,
        request: RouteRequest | None = None,
        router_name: str | None = None,
        optimizer_names: list[str] | None = None,
        # Legacy keyword-args kept for callers that still build a worker
        # from live session state.  When `request` is None they are
        # snapshotted into one here on the *caller's* thread (i.e. the
        # main thread, since __init__ is invoked before .start()).
        board: Board | None = None,
        layout: Layout | None = None,
        schematic: Schematic | None = None,
        components: dict[str, ComponentDefinition] | None = None,
        instances: dict[uuid.UUID, ComponentInstance] | None = None,
        routing_settings: RoutingSettings | None = None,
        net_ids: set[uuid.UUID] | None = None,
        project_path: Path | None = None,
    ) -> None:
        super().__init__()
        self._request: RouteRequest = (
            request
            if request is not None
            else _build_request(
                board=board,
                layout=layout,
                schematic=schematic,
                components=components,
                instances=instances,
                routing_settings=routing_settings,
                net_ids=net_ids,
                project_path=project_path,
            )
        )
        self._router_name = router_name
        # None  → legacy path: autoroute() runs its embedded simplify pass
        #          when routing_settings.enable_simplify_pass is True.
        # []    → explicit no optimizers (also suppresses the embedded pass).
        # [...] → explicit chain; embedded pass is suppressed and the listed
        #          optimizers run in order on the router's output.
        self._optimizer_names = optimizer_names
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
        import dataclasses as _dc
        import gc as _gc
        import threading as _t

        import structlog as _sl

        # Importing the built-in modules ensures the default router /
        # optimizer are registered even when no plugin loader has run yet.
        import protoboard.services.routing.builtin  # noqa: F401  side-effect
        import protoboard.services.routing.builtin_enhanced  # noqa: F401  side-effect
        import protoboard.services.routing.builtin_optimizers  # noqa: F401  side-effect
        from protoboard.services.routing.autorouter import (
            AutorouteCancelled,
            AutoroutePreconditionError,
            AutorouteResult,
        )
        from protoboard.services.routing.optimizer import (
            registry as optimizer_registry,
        )
        from protoboard.services.routing.protocol import ProgressSink, registry

        _sl.get_logger(__name__).info(
            "autoroute_worker.run",
            thread_id=_t.get_ident(),
            is_main=_t.current_thread() is _t.main_thread(),
            router=self._router_name or "<default>",
            optimizers=(
                self._optimizer_names
                if self._optimizer_names is not None
                else "<legacy>"
            ),
        )

        sink = ProgressSink(
            progress=self._report,
            optimize_progress=self._report_optimize,
            simplify_progress=self._report_simplify,
            cancel_flag=self._cancel_flag,
        )

        # When the caller specified an explicit optimizer chain, suppress
        # autoroute()'s embedded simplify pass — the chain owns the
        # post-processing now and double-running it would either no-op
        # or produce the wrong wires.
        request = self._request
        if self._optimizer_names is not None:
            request = _dc.replace(
                self._request,
                routing_settings=_dc.replace(
                    self._request.routing_settings,
                    enable_simplify_pass=False,
                ),
            )

        # Worker-thread GC quiet zone — covers the router call AND every
        # optimizer in the chain.  CPython 3.14 has a race where cyclic
        # GC ``mark_stacks`` walking the heap from the worker thread
        # SIGSEGVs while the main thread is inside a Qt paintEvent (see
        # memory ``project_silent_crash_post_upgrade`` and
        # ``project_enhanced_router_gc_fix``).  Disabling cyclic GC here
        # protects every registered router/optimizer without each plugin
        # having to know about the workaround; refcount dealloc keeps
        # memory bounded and a single ``gc.collect()`` at the end
        # reclaims cycles created during the pass.
        _gc_was_enabled = _gc.isenabled()
        if _gc_was_enabled:
            _gc.disable()
        try:
            try:
                router = (
                    registry.get(self._router_name)
                    if self._router_name is not None
                    else registry.default()
                )
                result: AutorouteResult = router.route(request, sink)

                # Chain optimizers in declared order.  Each optimizer's output
                # feeds the next.  An empty list explicitly disables all
                # post-processing.  Cancellation is checked between optimizers
                # so a long chain remains responsive to the Cancel button.
                from protoboard.services.routing.optimizer import (
                    incompatibility_reasons,
                )

                for opt_name in self._optimizer_names or []:
                    if sink.cancelled():
                        raise AutorouteCancelled
                    try:
                        opt = optimizer_registry.get(opt_name)
                    except KeyError:
                        _sl.get_logger(__name__).warning(
                            "autoroute_worker.optimizer_missing",
                            name=opt_name,
                        )
                        continue
                    # Defense-in-depth: even if the UI accidentally lets the
                    # user check an incompatible optimizer (or routing.yaml
                    # was hand-edited), skip it at runtime so the chain
                    # remains predictable.
                    reasons = incompatibility_reasons(
                        opt.capabilities(),
                        request.routing_settings,
                    )
                    if reasons:
                        _sl.get_logger(__name__).warning(
                            "autoroute_worker.optimizer_skipped_incompatible",
                            name=opt_name,
                            reasons=reasons,
                        )
                        continue
                    result = opt.optimize(request, result, sink)
            except AutorouteCancelled:
                self.finished.emit(AutorouteResult())
                return
            except AutoroutePreconditionError as exc:
                # Format the issues into a human-readable message.  The dialog
                # caller can split on newlines for a list view.
                lines = [str(exc), ""]
                for iss in exc.issues:
                    lines.append(f"  • [{iss.code}] {iss.message}")
                self.error.emit("\n".join(lines))
                return
            except Exception as exc:  # noqa: BLE001
                self.error.emit(str(exc))
                return

            self.finished.emit(result)
        finally:
            if _gc_was_enabled:
                _gc.enable()
                _gc.collect()

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _report(self, pass_num: int, net_name: str, pct: int) -> None:
        """Called from the worker thread — emits cross-thread signal."""
        self.progress.emit(pass_num, net_name, pct)

    def _report_optimize(self, done: int, total: int) -> None:
        """Called from the worker thread after each optimization pass completes."""
        self.optimize_progress.emit(done, total)

    def _report_simplify(
        self,
        iter_num: int,
        max_iter: int,
        n_simplified: int,
    ) -> None:
        """Called from the worker thread after each simplify-all iteration.

        Cross-thread signal emission is safe — Qt queues into the slot's
        thread context.  MainWindow updates the QProgressDialog label so
        the user sees ``Simplifying… 3/50 — 12 wires simplified``.
        """
        self.simplify_progress.emit(iter_num, max_iter, n_simplified)
