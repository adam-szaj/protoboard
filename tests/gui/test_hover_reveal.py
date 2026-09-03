# SPDX-License-Identifier: MIT
"""Hover-reveal: label tooltip (2A) and pin-name reveal (2B)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from protoboard.controller.ports.hover import HoverEvent
from protoboard.controller.ports.inputs import Mods
from protoboard.controller.view_options.layers import LayerOverrides, LayerVisibility
from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.geometry import Point
from protoboard.gui.board_view import BoardView
from protoboard.gui.hover_reveal import HoverReveal
from protoboard.gui.hover_tooltip import HoverTooltip
from protoboard.gui.session import EditorSession
from protoboard.services.library.loader import load_library

_LIBRARY_DIR = Path(__file__).parent.parent / "fixtures" / "library"


def test_tooltip_shows_and_hides(qtbot) -> None:
    from PySide6.QtWidgets import QWidget

    parent = QWidget()
    qtbot.addWidget(parent)
    parent.show()
    tip = HoverTooltip(parent)
    tip.show_at("R5 4.7k", (10, 20))
    assert tip.isVisible()
    assert "R5 4.7k" in tip.text()
    tip.hide()
    assert not tip.isVisible()


@dataclass
class _FakeInstance:
    refdes: str
    value: str


@dataclass
class _FakeSession:
    instances: dict[uuid.UUID, _FakeInstance] = field(default_factory=dict)


class _FakeView:
    """Minimal stand-in for BoardView's surface used by HoverReveal."""

    def __init__(self, qtbot, parent) -> None:
        self._viewport = parent
        self.layer_overrides = LayerOverrides()
        self.reveal_cid: uuid.UUID | None = None
        self.session = _FakeSession()
        self.refresh_calls = 0

    def viewport(self):
        return self._viewport

    def refresh(self) -> None:
        self.refresh_calls += 1

    def set_meta_pin_labels_reveal(self, component_id: uuid.UUID | None) -> None:
        if component_id != self.reveal_cid:
            self.reveal_cid = component_id
            self.refresh()


def _make_view(qtbot):
    from PySide6.QtWidgets import QWidget

    parent = QWidget()
    qtbot.addWidget(parent)
    parent.show()
    return _FakeView(qtbot, parent)


def _hover_event(cid: uuid.UUID | None, *, alt: bool) -> HoverEvent:
    objects = (cid,) if cid is not None else ()
    return HoverEvent(point=Point(1.0, 2.0), objects=objects, mods=Mods(alt=alt))


def test_dwell_labels_hidden_shows_tooltip(qtbot) -> None:
    view = _make_view(qtbot)
    view.layer_overrides.labels = LayerVisibility.HIDDEN
    cid = uuid.uuid4()
    view.session.instances[cid] = _FakeInstance(refdes="R5", value="4.7k")

    reveal = HoverReveal(cast(BoardView, view))  # _FakeView: minimal test double
    reveal.on_hover(_hover_event(cid, alt=False), (30, 40))
    reveal._on_dwell()

    assert reveal._tooltip.isVisible()
    assert "R5 4.7k" in reveal._tooltip.text()


def test_dwell_meta_pin_labels_hidden_alt_sets_reveal(qtbot) -> None:
    view = _make_view(qtbot)
    view.layer_overrides.meta_pin_labels = LayerVisibility.HIDDEN
    cid = uuid.uuid4()
    view.session.instances[cid] = _FakeInstance(refdes="U1", value="NE555")

    reveal = HoverReveal(cast(BoardView, view))  # _FakeView: minimal test double
    reveal.on_hover(_hover_event(cid, alt=True), (30, 40))
    reveal._on_dwell()

    assert view.reveal_cid == cid
    assert view.refresh_calls >= 1


def test_clear_hides_and_resets(qtbot) -> None:
    view = _make_view(qtbot)
    view.layer_overrides.meta_pin_labels = LayerVisibility.HIDDEN
    cid = uuid.uuid4()
    view.session.instances[cid] = _FakeInstance(refdes="U1", value="NE555")

    reveal = HoverReveal(cast(BoardView, view))  # _FakeView: minimal test double
    reveal.on_hover(_hover_event(cid, alt=True), (30, 40))
    reveal._on_dwell()
    assert view.reveal_cid == cid

    reveal.clear()

    assert not reveal._tooltip.isVisible()
    assert view.reveal_cid is None


def test_alt_release_clears_active_reveal(qtbot) -> None:
    view = _make_view(qtbot)
    view.layer_overrides.meta_pin_labels = LayerVisibility.HIDDEN
    cid = uuid.uuid4()
    view.session.instances[cid] = _FakeInstance(refdes="U1", value="NE555")

    reveal = HoverReveal(cast(BoardView, view))  # _FakeView: minimal test double
    reveal.on_hover(_hover_event(cid, alt=True), (30, 40))
    reveal._on_dwell()
    assert view.reveal_cid == cid

    # Same component, Alt released: the reveal must clear immediately.
    reveal.on_hover(_hover_event(cid, alt=False), (30, 40))

    assert view.reveal_cid is None


def _perfboard() -> Board:
    grid = BoardGrid(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        ),
        rows=4,
        cols=4,
        row_gap=0.0,
        col_gap=0.0,
    )
    return Board(root=grid)


def test_layer_state_threads_meta_pin_labels_reveal_cid(qtbot) -> None:
    """CRITICAL GAP: layer_state() must carry the persistent reveal cid.

    LayerState is rebuilt fresh on every refresh() via layer_state_for_tool()
    (which knows nothing about hover-reveal).  Without BoardView.layer_state()
    copying the persistent reveal id onto the freshly-built state, Task 8's
    per-component reveal is silently a no-op in production.
    """
    library = load_library([_LIBRARY_DIR])
    sess = EditorSession(board=_perfboard(), library=library)
    bv = BoardView(sess)
    qtbot.addWidget(bv)

    bv.layer_overrides.meta_pin_labels = LayerVisibility.HIDDEN
    cid = uuid.uuid4()
    bv.set_meta_pin_labels_reveal(cid)

    state = bv.layer_state()

    assert state.meta_pin_labels_reveal_cid == cid
    assert state.alpha_for_meta_pin_labels("top", cid=cid) > 0
    # A different component stays hidden.
    assert state.alpha_for_meta_pin_labels("top", cid=uuid.uuid4()) == 0.0
