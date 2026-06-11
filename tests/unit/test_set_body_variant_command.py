# SPDX-License-Identifier: MIT
"""``SetComponentBodyVariantCommand`` — per-instance body picker undo/redo."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.commands.component_cmds import (
    SetComponentBodyVariantCommand,
)
from protoboard.domain.components.definition import (
    ComponentClass,
    ComponentDefinition,
)
from protoboard.domain.components.footprints import TwoPin
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout


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
    """Minimal ComponentState stub."""

    board: Board = field(default_factory=_board)
    layout: Layout = field(default_factory=lambda: Layout(placements={}, wires=[]))
    instances: dict[uuid.UUID, ComponentInstance] = field(default_factory=dict)
    components: dict[str, ComponentDefinition] = field(default_factory=dict)


def _make_state(body_variant: str | None = None) -> tuple[_State, uuid.UUID]:
    state = _State()
    defn = ComponentDefinition(
        name="R", footprint=TwoPin(),
        pin_names={}, component_class=ComponentClass.RESISTOR,
        default_body_variant="DIN-0207/typ",
    )
    state.components["R"] = defn
    inst = ComponentInstance(
        id=uuid.uuid4(), definition_ref="R", refdes="R1", value="10k",
        body_variant=body_variant,
    )
    state.instances[inst.id] = inst
    return state, inst.id


class TestApply:
    def test_setting_variant_updates_instance(self) -> None:
        state, comp_id = _make_state()
        assert state.instances[comp_id].body_variant is None
        cmd = SetComponentBodyVariantCommand(
            component_id=comp_id, new_variant="DIN-0411/typ",
        )
        cmd.apply(state)
        assert state.instances[comp_id].body_variant == "DIN-0411/typ"

    def test_clearing_variant_restores_default(self) -> None:
        state, comp_id = _make_state(body_variant="DIN-0411/typ")
        cmd = SetComponentBodyVariantCommand(
            component_id=comp_id, new_variant=None,
        )
        cmd.apply(state)
        assert state.instances[comp_id].body_variant is None

    def test_other_instance_fields_untouched(self) -> None:
        state, comp_id = _make_state()
        original = state.instances[comp_id]
        cmd = SetComponentBodyVariantCommand(
            component_id=comp_id, new_variant="DIN-0617/typ",
        )
        cmd.apply(state)
        updated = state.instances[comp_id]
        assert updated.refdes == original.refdes
        assert updated.value == original.value
        assert updated.definition_ref == original.definition_ref
        # ``id`` stays stable so wires keep their references.
        assert updated.id == original.id


class TestRevert:
    def test_revert_after_setting_restores_none(self) -> None:
        state, comp_id = _make_state()
        cmd = SetComponentBodyVariantCommand(
            component_id=comp_id, new_variant="DIN-0411/typ",
        )
        cmd.apply(state)
        cmd.revert(state)
        assert state.instances[comp_id].body_variant is None

    def test_revert_after_clearing_restores_previous(self) -> None:
        state, comp_id = _make_state(body_variant="DIN-0411/typ")
        cmd = SetComponentBodyVariantCommand(
            component_id=comp_id, new_variant=None,
        )
        cmd.apply(state)
        cmd.revert(state)
        assert state.instances[comp_id].body_variant == "DIN-0411/typ"

    def test_revert_overwrites_explicit_prior_value(self) -> None:
        state, comp_id = _make_state(body_variant="DIN-0207/typ")
        cmd = SetComponentBodyVariantCommand(
            component_id=comp_id, new_variant="DIN-0617/typ",
        )
        cmd.apply(state)
        cmd.revert(state)
        assert state.instances[comp_id].body_variant == "DIN-0207/typ"


class TestDescription:
    def test_description_when_setting(self) -> None:
        cmd = SetComponentBodyVariantCommand(
            component_id=uuid.uuid4(), new_variant="DO-35/typ",
        )
        assert "DO-35/typ" in cmd.description

    def test_description_when_clearing(self) -> None:
        cmd = SetComponentBodyVariantCommand(
            component_id=uuid.uuid4(), new_variant=None,
        )
        assert "default" in cmd.description.lower()
