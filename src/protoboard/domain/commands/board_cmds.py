# SPDX-License-Identifier: MIT
"""Commands that modify the Board structure (strip cuts, resize)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import Box, HBox, SizePolicy, StripAtom, VBox
from protoboard.domain.commands.base import Command, ComponentState

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
        label=atom.label,
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
        label=atom.label,
    )


def split_strip_between(atom: StripAtom, after_pad: int) -> tuple[StripAtom, StripAtom]:
    """Cut between pad *after_pad* and pad *after_pad+1* (1-based).

    Valid range: 1 … length-1.
    """
    n = atom.resolved_length()
    if not 1 <= after_pad <= n - 1:
        raise ValueError(f"after_pad={after_pad} out of range for strip length={n}")
    return _make_segment(atom, after_pad), _make_segment(atom, n - after_pad)


def split_strip_on_pad(
    atom: StripAtom, pad_index: int
) -> tuple[StripAtom | None, StripAtom, StripAtom | None]:
    """Cut through pad *pad_index* (1-based) — pad becomes isolated.

    Returns (left | None, isolated_pad, right | None).
    """
    n = atom.resolved_length()
    if not 1 <= pad_index <= n:
        raise ValueError(f"pad_index={pad_index} out of range for strip length={n}")
    left = _make_segment(atom, pad_index - 1) if pad_index > 1 else None
    right = _make_segment(atom, n - pad_index) if pad_index < n else None
    return left, _make_isolated(atom), right


def replace_strip_in_board(
    board: Board, target: StripAtom, replacements: list[Box]
) -> Board:
    """Replace a strip in a nested row/column tree without moving its holes."""

    def replace_in_box(box: Box) -> Box:
        if not isinstance(box, (HBox, VBox)):
            return box
        children: list[Box] = []
        changed = False
        for child in box.children:
            if child is target:
                children.append(segment)
                changed = True
            else:
                new_child = replace_in_box(child)
                children.append(new_child)
                changed |= new_child is not child
        return replace(box, children=children) if changed else box

    # The original cell retains its dimensions even when the parent stacks
    # along the other axis or has gaps between children.
    container = HBox if target.orientation == "horizontal" else VBox
    segment = container(
        width_policy=_HUG,
        height_policy=_HUG,
        children=replacements,
        gap=0,
        margin=target.margin,
    )
    if board.root is target:
        new_root: Box = segment
    else:
        new_root = replace_in_box(board.root)
    if new_root is board.root:
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
