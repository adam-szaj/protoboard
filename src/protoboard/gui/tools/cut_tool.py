# SPDX-License-Identifier: MIT
"""Cut strip tool — click on a strip atom to record a cut position."""

from __future__ import annotations

import uuid

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent

from protoboard.domain.board.boxes import StripAtom
from protoboard.domain.layout.cut import StripCut
from protoboard.gui.board_view import _Tool


class CutTool(_Tool):
    """Click near a strip atom to add a StripCut at that position."""

    def on_press(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        # Find nearest strip atom.
        board = self._session.board
        best_strip: StripAtom | None = None
        best_pos_idx = 0
        best_d = float("inf")

        for atom, pos in board.atoms():
            if not isinstance(atom, StripAtom):
                continue
            dx = scene_pt.x() - pos.x
            dy = scene_pt.y() - pos.y
            d = dx * dx + dy * dy
            if d < best_d:
                best_d = d
                best_strip = atom
                # Approximate position index (1-based) within the strip.
                if atom.orientation == "horizontal":
                    best_pos_idx = max(1, round(scene_pt.x() - (pos.x - pos.x)) + 1)
                else:
                    best_pos_idx = max(1, round(scene_pt.y()) + 1)

        if best_strip is None or best_d > 1.0:
            return

        cut = StripCut(
            id=uuid.uuid4(),
            strip_atom_ref=str(id(best_strip)),
            position=best_pos_idx,
        )
        self._session.layout.cuts.append(cut)
        self._session.layout_changed.emit()
        self._view.refresh()
