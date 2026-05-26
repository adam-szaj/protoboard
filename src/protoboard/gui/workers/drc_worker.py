# SPDX-License-Identifier: MIT
"""Background QThread worker for DRC.

Splits the previously-synchronous ``Session.run_drc()`` into a snapshot
half (runs on the main thread, cheap) and a compute half (runs on the
worker, can be cancelled).

Why a worker at all?  On larger projects the dominant DRC cost is
``_check_bare_wire_interior_cross`` — O(N²·M²) on bare-wire paths per
side.  Running it inline on the main thread froze the GUI for hundreds
of ms after every layout change, even with the 600 ms debounce in
``MainWindow``: the debounce only deferred firing, the compute itself
still blocked the event loop.  This worker moves the compute off the
GUI thread; ``MainWindow`` supersedes in-flight workers when a fresh
layout edit arrives by setting the cancel flag.
"""

from __future__ import annotations

import threading
import uuid
from typing import TYPE_CHECKING

from PySide6.QtCore import QThread, Signal

if TYPE_CHECKING:
    from protoboard.domain.axis_labels_settings import AxisLabelSettings
    from protoboard.domain.board.board import Board
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.services.routing.snapshot import (
        LayoutSnapshot,
        SchematicSnapshot,
    )


class DRCWorker(QThread):
    """Runs the DRC engine in a background thread.

    The constructor takes **immutable snapshots** of session state built
    on the main thread; the worker is structurally unable to see later
    GUI edits.  The connectivity graph is rebuilt inside ``run`` from
    those snapshots — keeps the worker free of references to any
    shared mutable graph cache on the session.

    Signals
    -------
    finished(list[DRCIssue])
        Emitted when the compute completes.  Not emitted if the worker
        was cancelled before completion — the caller treats absence of
        the signal as "this run is irrelevant, the next worker will
        produce the authoritative result".
    error(str)
        Emitted on uncaught exception.  Same semantics as
        :class:`AutorouteWorker.error` — the message is logged by the
        receiver; the cancel flag is left alone.
    """

    finished: Signal = Signal(list)
    error: Signal = Signal(str)
    # Emitted unconditionally from ``run()``'s finally — including when the
    # worker bails out on cancel.  Lets MainWindow release the strong ref
    # it has to keep on cancelled workers (otherwise Python may GC them
    # mid-flight and Qt aborts with "QThread destroyed while running").
    exited: Signal = Signal()

    def __init__(
        self,
        *,
        board: "Board",
        layout_snap: "LayoutSnapshot",
        schematic_snap: "SchematicSnapshot",
        components: dict[str, "ComponentDefinition"],
        instances: dict[uuid.UUID, "ComponentInstance"],
        axis_labels: "AxisLabelSettings | None" = None,
    ) -> None:
        super().__init__()
        self._board = board
        self._layout_snap = layout_snap
        self._schematic_snap = schematic_snap
        # Defensive copies — the dicts referenced on the main thread
        # could be mutated under us while we run.  Values are
        # immutable-by-convention (ComponentDefinition / Instance) so
        # shallow copies suffice.
        self._components = dict(components)
        self._instances = dict(instances)
        self._axis_labels = axis_labels
        self._cancel_flag = threading.Event()

    def cancel(self) -> None:
        """Signal the worker to bail out at the next checkpoint.

        Cheap; the worker checks the flag between checks and inside the
        O(N²) interior-cross loop.  Safe to call from any thread.
        """
        self._cancel_flag.set()

    def run(self) -> None:  # type: ignore[override]
        try:
            from protoboard.domain.connectivity.graph import ConnectivityGraph
            from protoboard.services.drc.engine import DRCEngine

            layout = self._layout_snap.to_layout()
            schematic = self._schematic_snap.to_schematic()

            if self._cancel_flag.is_set():
                return  # cancelled before any work — drop silently

            # Build a fresh graph inside the worker thread.  This is
            # the safest contract (no shared mutable cache); on real
            # projects graph construction is small relative to DRC.
            graph = ConnectivityGraph(
                board=self._board,
                layout=layout,
                components=self._components,
                instances=self._instances,
            )
            # Register existing wires so DRC checks that consult the
            # graph (electric-set membership) see the routed state.
            for wire in layout.wires:
                graph.add_wire(wire)

            if self._cancel_flag.is_set():
                return

            issues = DRCEngine().check_with_graph(
                schematic=schematic,
                layout=layout,
                components=self._components,
                instances=self._instances,
                graph=graph,
                cancel_flag=self._cancel_flag,
                axis_labels=self._axis_labels,
            )

            if self._cancel_flag.is_set():
                # The engine may have returned a partial list; the
                # caller has already moved on, so suppress the signal.
                return

            self.finished.emit(issues)
        except Exception as exc:  # noqa: BLE001 — surface anything via signal
            if not self._cancel_flag.is_set():
                self.error.emit(str(exc))
        finally:
            # Always notify — including the cancel-bail path that
            # returns silently above.  MainWindow uses this to release
            # its strong reference once run() is provably done.
            self.exited.emit()


__all__ = ["DRCWorker"]
