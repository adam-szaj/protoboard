# SPDX-License-Identifier: MIT
"""Commands that modify the Board structure (strip cuts, resize)."""

from __future__ import annotations

from dataclasses import dataclass, field

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import Box, HBox, SizePolicy, StripAtom, VBox
from protoboard.domain.commands.base import Command
from protoboard.domain.commands.component_cmds import ComponentState

_HUG = SizePolicy.HUG


# ---------------------------------------------------------------------------
# Strip-split helpers (pure functions, no Qt)
# ---------------------------------------------------------------------------


def _make_isolated(atom: StripAtom) -> StripAtom:
    return StripAtom(
        width_policy=_HUG,
        height_policy=_HUG,
        length=1,
        orientation=atom.orientation,
        layers=atom.layers,
        cuttable=atom.cuttable,
        isolated=True,
    )


def _make_segment(atom: StripAtom, length: int) -> StripAtom:
    return StripAtom(
        width_policy=_HUG,
        height_policy=_HUG,
        length=length,
        orientation=atom.orientation,
        layers=atom.layers,
        cuttable=atom.cuttable,
        isolated=False,
    )


def split_strip_between(
    atom: StripAtom, after_pad: int
) -> tuple[StripAtom, StripAtom]:
    """Cut between pad *after_pad* and pad *after_pad+1* (1-based).

    Valid range: 1 … length-1.
    """
    n = int(atom.length) if isinstance(atom.length, int) else 1
    if not 1 <= after_pad <= n - 1:
        raise ValueError(f"after_pad={after_pad} out of range for strip length={n}")
    return _make_segment(atom, after_pad), _make_segment(atom, n - after_pad)


def split_strip_on_pad(
    atom: StripAtom, pad_index: int
) -> tuple[StripAtom | None, StripAtom, StripAtom | None]:
    """Cut through pad *pad_index* (1-based) — pad becomes isolated.

    Returns (left | None, isolated_pad, right | None).
    """
    n = int(atom.length) if isinstance(atom.length, int) else 1
    if not 1 <= pad_index <= n:
        raise ValueError(f"pad_index={pad_index} out of range for strip length={n}")
    left = _make_segment(atom, pad_index - 1) if pad_index > 1 else None
    right = _make_segment(atom, n - pad_index) if pad_index < n else None
    return left, _make_isolated(atom), right


def _replace_in_hbox(
    hbox: HBox, old_atom: StripAtom, new_children: list[Box]
) -> HBox:
    result: list[Box] = []
    for child in hbox.children:
        if child is old_atom:
            result.extend(new_children)
        else:
            result.append(child)
    return HBox(
        width_policy=hbox.width_policy,
        height_policy=hbox.height_policy,
        children=result,
        gap=hbox.gap,
    )


def replace_strip_in_board(
    board: Board, target: StripAtom, replacements: list[Box]
) -> Board:
    """Return a new Board with *target* replaced by *replacements* in-place."""
    from dataclasses import replace as dc_replace

    root = board.root
    if isinstance(root, HBox):
        new_root: Box = _replace_in_hbox(root, target, replacements)
    elif isinstance(root, VBox):
        new_children: list[Box] = [
            _replace_in_hbox(c, target, replacements) if isinstance(c, HBox) else c
            for c in root.children
        ]
        new_root = dc_replace(root, children=new_children)
    else:
        return board
    return Board(root=new_root, raster_pitch_mm=board.raster_pitch_mm)


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


@dataclass
class SplitStripCommand(Command[ComponentState]):
    """Split a StripAtom into segments at the click position.

    *cut_between=True*  → cut between pad *cut_index* and *cut_index+1*.
    *cut_between=False* → cut through pad *cut_index* (isolated pad).
    """

    target: StripAtom
    cut_index: int
    cut_between: bool

    _old_board: Board = field(init=False, repr=False)

    description: str = "Cut strip"

    def apply(self, state: ComponentState) -> None:
        self._old_board = state.board
        if self.cut_between:
            left, right = split_strip_between(self.target, self.cut_index)
            replacements: list[Box] = [left, right]
        else:
            l_seg, iso, r_seg = split_strip_on_pad(self.target, self.cut_index)
            replacements = [s for s in (l_seg, iso, r_seg) if s is not None]
        state.board = replace_strip_in_board(state.board, self.target, replacements)

    def revert(self, state: ComponentState) -> None:
        state.board = self._old_board


@dataclass
class ResizeBoardCommand(Command[ComponentState]):
    """Replace the board with a new one of different dimensions.

    Only supports boards built from make_perfboard / make_stripboard /
    make_breadboard (the root must be a BoardGrid or a VBox of strips).
    Caller is responsible for verifying no components fall outside the
    new bounds before executing.
    """

    new_board: Board
    _old_board: Board = field(init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Resize board"

    def apply(self, state: ComponentState) -> None:
        self._old_board = state.board
        state.board = self.new_board

    def revert(self, state: ComponentState) -> None:
        state.board = self._old_board
