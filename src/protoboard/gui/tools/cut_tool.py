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

        board = self._session.board
        best_strip: StripAtom | None = None
        best_pos_idx = 0
        best_d = float("inf")

        for atom, pos in board.atoms():
            if not isinstance(atom, StripAtom):
                continue
            # pos is the center of the whole strip.
            # Compute half-extent and the start of the strip.
            if atom.orientation == "horizontal":
                half = (atom.length if isinstance(atom.length, (int, float)) else 1) / 2
                # Strip spans [pos.x - half + 0.5, pos.x + half - 0.5]
                dx = scene_pt.x() - pos.x
                dy = scene_pt.y() - pos.y
                if abs(dy) <= 0.5:
                    # Project onto strip axis
                    clamped = max(-half + 0.5, min(half - 0.5, dx))
                    d = abs(dx - clamped) + abs(dy)
                    if d < best_d:
                        best_d = d
                        best_strip = atom
                        # 1-based index from left end of strip.
                        n = int(atom.length) if isinstance(atom.length, int) else 1
                        best_pos_idx = max(1, min(n, int(dx + half) + 1))
            else:
                half = (atom.length if isinstance(atom.length, (int, float)) else 1) / 2
                dx = scene_pt.x() - pos.x
                dy = scene_pt.y() - pos.y
                if abs(dx) <= 0.5:
                    clamped = max(-half + 0.5, min(half - 0.5, dy))
                    d = abs(dy - clamped) + abs(dx)
                    if d < best_d:
                        best_d = d
                        best_strip = atom
                        n = int(atom.length) if isinstance(atom.length, int) else 1
                        best_pos_idx = max(1, min(n, int(dy + half) + 1))

        if best_strip is None or best_d > 1.0:
            return

        cut = StripCut(
            id=uuid.uuid4(),
            strip_atom_ref=str(id(best_strip)),
            position=best_pos_idx,
        )
        self._session.layout.cuts.append(cut)
        self._session.layout_changed.emit()
