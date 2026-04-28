# SPDX-License-Identifier: MIT
"""Tests for EditorSession — signal wiring and state initialisation."""

from __future__ import annotations

from protoboard.gui.session import EditorSession


class TestSessionInit:
    def test_components_populated_from_library(self, session: EditorSession) -> None:
        # library has at least R, LM358, BC547
        assert len(session.components) > 0

    def test_layout_empty_on_init(self, session: EditorSession) -> None:
        assert session.layout.placements == {}
        assert session.layout.wires == []

    def test_instances_empty_on_init(self, session: EditorSession) -> None:
        assert session.instances == {}


class TestSessionSignals:
    def test_layout_changed_signal_exists(self, session: EditorSession) -> None:
        received: list[None] = []
        session.layout_changed.connect(lambda: received.append(None))
        session.layout_changed.emit()
        assert len(received) == 1

    def test_drc_changed_emitted_on_run_drc(self, session: EditorSession) -> None:
        received: list[list[object]] = []
        session.drc_changed.connect(lambda issues: received.append(issues))
        session.run_drc()
        assert len(received) == 1

    def test_run_drc_returns_list(self, session: EditorSession) -> None:
        issues = session.run_drc()
        assert isinstance(issues, list)
