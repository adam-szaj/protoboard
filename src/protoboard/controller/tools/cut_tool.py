# SPDX-License-Identifier: MIT
"""Cut strip tool — click on/between strip holes to split a segment."""

from __future__ import annotations

from typing import TYPE_CHECKING

from protoboard.controller.ports import LEFT_BUTTON
from protoboard.controller.tools.base import ControllerTool
from protoboard.domain.board.boxes import StripAtom
from protoboard.domain.commands.board_cmds import SplitStripCommand
from protoboard.geometry import Point

if TYPE_CHECKING:
    from protoboard.controller.ports import PointerInput

# Maximum scene-unit distance to accept a click as targeting a strip.
_MAX_DIST = 0.65


class CutTool(ControllerTool):
    """Click on or between strip pads to split a StripAtom segment.

    - Click within 0.3 ru of a pad centre → cut ON that pad (isolated pad).
    - Click between two pads → cut BETWEEN them (two clean segments).
    """

    def on_press(self, inp: PointerInput) -> bool:
        if inp.button != LEFT_BUTTON:
            return False

        result = _find_cut_target(self._session.board, inp.scene_pos)
        if result is None:
            return False

        atom, cut_index, cut_between = result
        cmd = SplitStripCommand(
            target=atom,
            cut_index=cut_index,
            cut_between=cut_between,
        )
        if not self._execute_command(cmd):
            return False
        # SplitStripCommand rebuilds Board.root, so the board layer (pads /
        # strips) has to be repainted on the next refresh.  ``layout_changed``
        # alone only re-renders components and wires.
        view = self._view
        if view is not None:
            view.mark_board_layer_dirty()
        return True


# ---------------------------------------------------------------------------
# Hit detection (pure, no Qt state)
# ---------------------------------------------------------------------------


def _find_cut_target(
    board: object, scene_pt: Point
) -> tuple[StripAtom, int, bool] | None:
    """Return (atom, index, cut_between) for the closest strip target.

    *cut_between=True*: cut after pad *index* (between *index* and *index+1*).
    *cut_between=False*: cut ON pad *index* (isolated pad).

    Returns None if no strip is close enough.
    """
    from protoboard.domain.board.board import Board

    assert isinstance(board, Board)

    best: tuple[StripAtom, int, bool] | None = None
    best_d = float("inf")

    for atom, pos in board.atoms():
        if not isinstance(atom, StripAtom):
            continue
        if not atom.cuttable:
            continue
        n = int(atom.length) if isinstance(atom.length, int) else 1
        if n < 2:
            # A single-hole segment can't be split further.
            continue

        if atom.orientation == "horizontal":
            half = n / 2.0
            dy = abs(scene_pt.y - pos.y)
            if dy > _MAX_DIST:
                continue
            dx_from_start = scene_pt.x - (pos.x - half + 0.5)
            # Pad positions relative to strip start: 0.0, 1.0, 2.0, …, n-1
            # Gap positions: 0.5, 1.5, …, n-1.5
            pad_idx, is_on_pad, dist = _nearest_feature(dx_from_start, n)
            d = dist + dy
        else:
            half = n / 2.0
            dx = abs(scene_pt.x - pos.x)
            if dx > _MAX_DIST:
                continue
            dy_from_start = scene_pt.y - (pos.y - half + 0.5)
            pad_idx, is_on_pad, dist = _nearest_feature(dy_from_start, n)
            d = dist + dx

        if d < best_d and d < _MAX_DIST:
            best_d = d
            if is_on_pad:
                best = (atom, pad_idx, False)  # cut ON pad
            else:
                # pad_idx is the gap after pad pad_idx → cut BETWEEN
                best = (atom, pad_idx, True)

    return best


def _nearest_feature(coord: float, n: int) -> tuple[int, bool, float]:
    """Find the nearest pad or gap along a 1D strip.

    *coord* is position measured from pad-1 centre (0.0 = pad 1, 1.0 = pad 2…).
    Returns (1-based index, is_on_pad, distance).

    For a pad:  index = pad number (1…n).
    For a gap:  index = pad number before the gap (1…n-1).
    """
    # Pad centres at integer coords 0, 1, …, n-1.
    # Gap centres at half-integer coords 0.5, 1.5, …, n-1.5 (n-1 gaps).

    # Nearest pad
    pad_0 = max(0, min(n - 1, round(coord)))
    pad_dist = abs(coord - pad_0)

    # Nearest gap
    gap_0 = max(0.0, min(n - 1.0, round(coord * 2) / 2.0))
    # gap_0 is a half-integer; valid gaps are 0.5, 1.5, …
    if gap_0 != int(gap_0):  # is half-integer → valid gap
        gap_dist = abs(coord - gap_0)
        gap_after_pad = int(gap_0)  # 0-based pad before the gap
    else:
        gap_dist = float("inf")
        gap_after_pad = 0

    if pad_dist <= gap_dist:
        return pad_0 + 1, True, pad_dist  # 1-based pad index
    return gap_after_pad + 1, False, gap_dist  # 1-based: after pad gap_after_pad+1
