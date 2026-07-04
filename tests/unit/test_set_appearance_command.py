# SPDX-License-Identifier: MIT
"""``SetComponentAppearanceCommand`` — per-instance style override undo/redo."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.commands.component_cmds import (
    SetComponentAppearanceCommand,
)
from protoboard.domain.components.appearance import Appearance
from protoboard.domain.components.definition import (
    ComponentClass,
    ComponentDefinition,
)
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.wire import WireCollection


def _board() -> Board:
    return Board(root=BoardGrid(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        ),
        rows=4, cols=4, row_gap=0.0, col_gap=0.0,
    ))


@dataclass
class _State:
    board: Board = field(default_factory=_board)
    layout: Layout = field(
        default_factory=lambda: Layout(placements={}, wires=WireCollection())
    )
    instances: dict[uuid.UUID, ComponentInstance] = field(default_factory=dict)
    components: dict[str, ComponentDefinition] = field(default_factory=dict)


_BLACK = Appearance(color="#1a1a1a")
_ROUNDED = Appearance(color="#d9c7a3", corner_radius_mm=(0.5, 0.0, 0.5, 0.0))


def _make_state(appearance: Appearance | None = None) -> tuple[_State, uuid.UUID]:
    state = _State()
    defn = ComponentDefinition.from_footprint(
        name="R", footprint=SIL(n=2),
        pin_names={}, component_class=ComponentClass.RESISTOR,
    )
    state.components["R"] = defn
    inst = ComponentInstance(
        id=uuid.uuid4(), definition_ref="R", refdes="R1", value="10k",
        appearance=appearance,
    )
    state.instances[inst.id] = inst
    return state, inst.id


class TestApply:
    def test_setting_appearance_updates_instance(self) -> None:
        state, comp_id = _make_state()
        assert state.instances[comp_id].appearance is None
        cmd = SetComponentAppearanceCommand(
            component_id=comp_id, new_appearance=_BLACK,
        )
        cmd.apply(state)
        assert state.instances[comp_id].appearance == _BLACK

    def test_clearing_appearance_restores_default(self) -> None:
        state, comp_id = _make_state(appearance=_BLACK)
        cmd = SetComponentAppearanceCommand(
            component_id=comp_id, new_appearance=None,
        )
        cmd.apply(state)
        assert state.instances[comp_id].appearance is None

    def test_other_instance_fields_untouched(self) -> None:
        state, comp_id = _make_state()
        original = state.instances[comp_id]
        cmd = SetComponentAppearanceCommand(
            component_id=comp_id, new_appearance=_ROUNDED,
        )
        cmd.apply(state)
        updated = state.instances[comp_id]
        assert updated.refdes == original.refdes
        assert updated.value == original.value
        assert updated.id == original.id


class TestRevert:
    def test_revert_after_setting_restores_none(self) -> None:
        state, comp_id = _make_state()
        cmd = SetComponentAppearanceCommand(
            component_id=comp_id, new_appearance=_BLACK,
        )
        cmd.apply(state)
        cmd.revert(state)
        assert state.instances[comp_id].appearance is None

    def test_revert_after_clearing_restores_previous(self) -> None:
        state, comp_id = _make_state(appearance=_ROUNDED)
        cmd = SetComponentAppearanceCommand(
            component_id=comp_id, new_appearance=None,
        )
        cmd.apply(state)
        cmd.revert(state)
        assert state.instances[comp_id].appearance == _ROUNDED

    def test_revert_overwrites_explicit_prior_value(self) -> None:
        state, comp_id = _make_state(appearance=_BLACK)
        cmd = SetComponentAppearanceCommand(
            component_id=comp_id, new_appearance=_ROUNDED,
        )
        cmd.apply(state)
        cmd.revert(state)
        assert state.instances[comp_id].appearance == _BLACK

    def test_none_set_none_round_trip(self) -> None:
        # None → set → undo → None: the plan's canonical cycle.
        state, comp_id = _make_state()
        cmd = SetComponentAppearanceCommand(
            component_id=comp_id, new_appearance=_ROUNDED,
        )
        cmd.apply(state)
        assert state.instances[comp_id].appearance == _ROUNDED
        cmd.revert(state)
        assert state.instances[comp_id].appearance is None


class TestDescription:
    def test_description_when_setting(self) -> None:
        cmd = SetComponentAppearanceCommand(
            component_id=uuid.uuid4(), new_appearance=_BLACK,
        )
        assert "set" in cmd.description.lower()

    def test_description_when_clearing(self) -> None:
        cmd = SetComponentAppearanceCommand(
            component_id=uuid.uuid4(), new_appearance=None,
        )
        assert "clear" in cmd.description.lower()
