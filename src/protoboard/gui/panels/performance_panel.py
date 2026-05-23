# SPDX-License-Identifier: MIT
"""PerformancePanel — real-time rendering and routing metrics."""

from __future__ import annotations

import os
import time
import uuid

import structlog
from PySide6.QtCore import Qt, QTimer

_click_log = structlog.get_logger("protoboard.gui.panels.performance_panel.click")
_CLICK_PROFILE = os.environ.get("PROTOBOARD_CLICK_PROFILE") not in (None, "", "0")
from PySide6.QtGui import QPainter, QPen, QColor
from PySide6.QtWidgets import (
    QButtonGroup,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QRadioButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from protoboard.gui.session import EditorSession, PerfMetrics
from protoboard.services.routing.cost_analyzer import CostBreakdown, analyze_wires


class _Sparkline(QWidget):
    """Tiny bar-chart showing the last N timing samples."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._samples: list[float] = []
        self.setFixedHeight(24)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_samples(self, samples: list[float]) -> None:
        self._samples = list(samples)
        self.update()

    def paintEvent(self, _event: object) -> None:  # type: ignore[override]
        if not self._samples:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        w, h = self.width(), self.height()
        max_val = max(self._samples) or 1.0
        bar_w = max(1, w // len(self._samples))
        painter.setPen(Qt.PenStyle.NoPen)
        for i, v in enumerate(self._samples):
            bar_h = int((v / max_val) * h)
            color = QColor(0x4C, 0xAF, 0x50) if v < 16 else (
                QColor(0xFF, 0xC1, 0x07) if v < 50 else QColor(0xF4, 0x43, 0x36)
            )
            painter.fillRect(i * bar_w, h - bar_h, bar_w - 1, bar_h, color)
        # Baseline
        painter.setPen(QPen(QColor(0x88, 0x88, 0x88)))
        painter.drawLine(0, h - 1, w, h - 1)
        painter.end()


class PerformancePanel(QWidget):
    """Dock panel showing session performance metrics.

    Updated on a 1-second polling timer (reads ``session.perf`` directly).
    """

    def __init__(self, session: EditorSession, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._session = session
        # Current selection snapshot, kept in sync via selection_changed.
        self._selected_wire_ids: frozenset[uuid.UUID] = frozenset()
        self._selected_net_id: uuid.UUID | None = None
        # Debounce timer for the cost-breakdown table: selection clicks
        # fire `selection_changed` synchronously, but the cost recompute
        # walks every wire on the board — too slow to do on every click.
        # We delay the actual recompute by ~200ms so highlight latency
        # on the canvas stays under one frame.
        self._cost_refresh_timer = QTimer(self)
        self._cost_refresh_timer.setSingleShot(True)
        # 500 ms gives Qt comfortably enough time to paint the click's
        # highlight before we start the (still sync-on-main-thread)
        # cost recomputation.  Anything shorter risks the timer firing
        # in the same event-loop turn as the click and blocking the
        # paint that the user is waiting to see.
        self._cost_refresh_timer.setInterval(500)
        self._cost_refresh_timer.timeout.connect(self._refresh_cost)
        # Set to True when the panel is hidden during a refresh request;
        # `showEvent` consumes it to bring the table up to date.
        self._cost_refresh_pending = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(8)

        # ── Timing metrics (existing) ──────────────────────────────────
        timing = QGroupBox("Timing & counts")
        layout = QFormLayout(timing)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        def _lbl() -> QLabel:
            l = QLabel("—")
            l.setAlignment(Qt.AlignmentFlag.AlignRight)
            return l

        self._refresh_lbl = _lbl()
        self._drc_lbl = _lbl()
        self._route_lbl = _lbl()
        self._graph_lbl = _lbl()
        self._items_lbl = _lbl()
        self._wires_lbl = _lbl()
        self._comps_lbl = _lbl()
        self._drc_issues_lbl = _lbl()

        self._refresh_spark = _Sparkline()
        self._route_spark = _Sparkline()

        layout.addRow("Refresh:", self._refresh_lbl)
        layout.addRow("", self._refresh_spark)
        layout.addRow("DRC:", self._drc_lbl)
        layout.addRow("Route:", self._route_lbl)
        layout.addRow("", self._route_spark)
        layout.addRow("Graph build:", self._graph_lbl)
        layout.addRow("Scene items:", self._items_lbl)
        layout.addRow("Wires:", self._wires_lbl)
        layout.addRow("Components:", self._comps_lbl)
        layout.addRow("DRC issues:", self._drc_issues_lbl)
        outer.addWidget(timing)

        # ── Cost breakdown for selected wires / net / everything ───────
        cost_grp = QGroupBox("Cost breakdown")
        cost_box = QVBoxLayout(cost_grp)
        cost_box.setContentsMargins(6, 6, 6, 6)
        cost_box.setSpacing(4)

        scope_row = QHBoxLayout()
        scope_row.setSpacing(8)
        self._scope_sel = QRadioButton("Selection")
        self._scope_net = QRadioButton("Net")
        self._scope_all = QRadioButton("All")
        self._scope_sel.setChecked(True)
        self._scope_grp = QButtonGroup(self)
        for rb in (self._scope_sel, self._scope_net, self._scope_all):
            self._scope_grp.addButton(rb)
            rb.toggled.connect(self._schedule_cost_refresh)
            scope_row.addWidget(rb)
        scope_row.addStretch()
        cost_box.addLayout(scope_row)

        self._cost_summary = QLabel("—")
        self._cost_summary.setStyleSheet("font-weight: bold;")
        cost_box.addWidget(self._cost_summary)

        self._cost_table = QTableWidget(0, 4)
        self._cost_table.setHorizontalHeaderLabels(["Category", "Count", "Unit", "Total"])
        self._cost_table.verticalHeader().setVisible(False)
        self._cost_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._cost_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._cost_table.setAlternatingRowColors(True)
        self._cost_table.setSortingEnabled(True)
        hdr = self._cost_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        cost_box.addWidget(self._cost_table)

        outer.addWidget(cost_grp)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

        self._connect_session_signals(session)
        self._schedule_cost_refresh()

    def _refresh(self) -> None:
        p: PerfMetrics = self._session.perf
        self._refresh_lbl.setText(f"{p.refresh_ms:.1f} ms")
        self._drc_lbl.setText(f"{p.drc_ms:.1f} ms")
        self._route_lbl.setText(f"{p.route_ms:.1f} ms")
        self._graph_lbl.setText(f"{p.graph_build_ms:.1f} ms")
        self._items_lbl.setText(str(p.scene_items))
        self._wires_lbl.setText(str(p.wire_count))
        self._comps_lbl.setText(str(p.component_count))
        self._drc_issues_lbl.setText(str(p.drc_issue_count))
        self._refresh_spark.set_samples(p.refresh_history)
        self._route_spark.set_samples(p.route_history)

    # ------------------------------------------------------------------
    # Cost breakdown
    # ------------------------------------------------------------------

    def _connect_session_signals(self, session: EditorSession) -> None:
        # Cost recomputation is debounced: selection clicks should produce
        # a snappy highlight on the canvas without waiting for the panel
        # to walk every wire on the board.  The debounce timer (set up in
        # __init__) coalesces bursts of selection changes (e.g. user
        # cycling through wires) into a single recompute.
        session.selection_changed.connect(self._on_selection_changed)
        session.layout_changed.connect(self._schedule_cost_refresh)

    def _disconnect_session_signals(self, session: EditorSession) -> None:
        try:
            session.selection_changed.disconnect(self._on_selection_changed)
        except (TypeError, RuntimeError):
            pass
        try:
            session.layout_changed.disconnect(self._schedule_cost_refresh)
        except (TypeError, RuntimeError):
            pass

    def _on_selection_changed(
        self,
        comp_ids: object,
        wire_ids: object,
        net_id: object,
    ) -> None:
        self._selected_wire_ids = (
            frozenset(wire_ids) if wire_ids else frozenset()  # type: ignore[arg-type]
        )
        self._selected_net_id = net_id if isinstance(net_id, uuid.UUID) else None
        self._schedule_cost_refresh()

    def _schedule_cost_refresh(self) -> None:
        """Queue a cost recomputation.

        Skipped entirely when the panel widget isn't currently visible
        (collapsed dock, hidden tab) — there's no point walking every
        wire to populate a table the user can't see.  A pending refresh
        is flagged so the table catches up the moment the panel becomes
        visible again.
        """
        if not self.isVisible():
            self._cost_refresh_pending = True
            return
        self._cost_refresh_pending = False
        self._cost_refresh_timer.start()

    def showEvent(self, event: object) -> None:  # type: ignore[override]
        super().showEvent(event)  # type: ignore[arg-type]
        if self._cost_refresh_pending:
            self._cost_refresh_pending = False
            self._cost_refresh_timer.start()

    def _wires_for_current_scope(self):  # type: ignore[no-untyped-def]
        """Return the WireSegment list matching the active scope radio."""
        layout = self._session.layout
        all_wires = list(layout.wires)
        if self._scope_all.isChecked():
            return all_wires, "All wires"
        if self._scope_net.isChecked() and self._selected_net_id is not None:
            wire_net = self._session.cached_wire_net_map()
            net_wires = [
                w for w in all_wires
                if wire_net.get(w.id) == self._selected_net_id
            ]
            net_name = ""
            net = self._session.schematic.nets.get(self._selected_net_id)
            if net is not None:
                net_name = net.name
            return net_wires, f"Net {net_name!r}" if net_name else "Net (selection)"
        # Default / Selection scope.
        sel = [w for w in all_wires if w.id in self._selected_wire_ids]
        return sel, f"Selection ({len(sel)} wire{'s' if len(sel) != 1 else ''})"

    def _refresh_cost(self) -> None:
        _t0 = time.monotonic() if _CLICK_PROFILE else 0.0
        try:
            self._refresh_cost_impl()
        finally:
            if _CLICK_PROFILE:
                _click_log.info(
                    "click_profile.performance_panel.refresh_cost",
                    ms=round((time.monotonic() - _t0) * 1000, 1),
                )

    def _refresh_cost_impl(self) -> None:
        # Second guard: a refresh that was scheduled while visible may
        # fire after the dock got hidden.  Bail out and let `showEvent`
        # pick it back up.
        if not self.isVisible():
            self._cost_refresh_pending = True
            return
        wires, label = self._wires_for_current_scope()
        if not wires:
            self._cost_summary.setText(f"{label}: no wires")
            self._cost_table.setRowCount(0)
            return
        # Pad-walk sets come from the session cache — invalidated on
        # layout_changed, so a selection click is essentially free here.
        cm = self._session.routing_settings.cost_model()
        under_pads = (
            self._session.cached_under_component_pads()
            if cm.get("under_component_top", 0.0) > 0.0 else set()
        )
        plated = (
            self._session.cached_plated_through_pads()
            if cm.get("layer_change", 0.0) > 0.0 else set()
        )
        # Pass every wire on the board so crossings with wires *outside*
        # the analysed selection (e.g. when scope is "Selection" and the
        # user picked one wire) are still detected and charged.  Without
        # this list `_count_crossings` falls back to silently skipping.
        breakdown: CostBreakdown = analyze_wires(
            wires,
            cost_model=cm,
            under_component_pads=under_pads,
            plated_through_pads=plated,
            all_board_wires=list(self._session.layout.wires),
            # PadIndex feeds at-pad crossing detection — its occupancy
            # records every wire vertex / mid-segment crossing on each
            # pad, so the analyzer doesn't need to reverse-engineer that
            # from geometry.  Session caches the index and rebuilds it
            # lazily after layout_changed.
            pad_index=self._session.pad_index(),
        )
        self._cost_summary.setText(
            f"{label}: total {breakdown.total:.2f}  "
            f"({breakdown.step_count} steps, {breakdown.wire_count} wires)"
        )
        # Disable sorting during populate to avoid row-shuffle while filling.
        self._cost_table.setSortingEnabled(False)
        self._cost_table.setRowCount(len(breakdown.categories))
        for row, cat in enumerate(breakdown.categories):
            self._cost_table.setItem(row, 0, QTableWidgetItem(cat.label))
            count_item = QTableWidgetItem()
            count_item.setData(Qt.ItemDataRole.DisplayRole, cat.count)
            count_item.setTextAlignment(
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            )
            self._cost_table.setItem(row, 1, count_item)
            unit_item = QTableWidgetItem(f"{cat.unit_cost:.3f}")
            unit_item.setTextAlignment(
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            )
            self._cost_table.setItem(row, 2, unit_item)
            total_item = QTableWidgetItem(f"{cat.total:.2f}")
            total_item.setTextAlignment(
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            )
            self._cost_table.setItem(row, 3, total_item)
        self._cost_table.setSortingEnabled(True)

    def set_session(self, session: EditorSession) -> None:
        self._disconnect_session_signals(self._session)
        self._session = session
        self._selected_wire_ids = frozenset()
        self._selected_net_id = None
        self._connect_session_signals(session)
        self._schedule_cost_refresh()
