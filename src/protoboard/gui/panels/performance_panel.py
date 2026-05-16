# SPDX-License-Identifier: MIT
"""PerformancePanel — real-time rendering and routing metrics."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPainter, QPen, QColor
from PySide6.QtWidgets import (
    QFormLayout,
    QLabel,
    QSizePolicy,
    QWidget,
)

from protoboard.gui.session import EditorSession, PerfMetrics


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

        layout = QFormLayout(self)
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

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

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

    def set_session(self, session: EditorSession) -> None:
        self._session = session
