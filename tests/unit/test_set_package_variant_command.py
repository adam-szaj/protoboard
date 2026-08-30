# SPDX-License-Identifier: MIT
"""``SetComponentPackageVariantCommand`` — per-instance package picker undo/redo."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.commands.component_cmds import (
    SetComponentPackageVariantCommand,
)
from protoboard.domain.components.definition import (
    ComponentClass,
    ComponentDefinition,
    MetaPin,
    PackageOption,
)
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import PlacementCollection
from protoboard.domain.layout.wire import WireCollection


def _board() -> Board:
    return Board(
        root=BoardGrid(
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
    )


@dataclass
class _State:
    board: Board = field(default_factory=_board)
    layout: Layout = field(
        default_factory=lambda: Layout(
            placements=PlacementCollection({}), wires=WireCollection()
        )
    )
    instances: dict[uuid.UUID, ComponentInstance] = field(default_factory=dict)
    components: dict[str, ComponentDefinition] = field(default_factory=dict)


def _make_state(package_variant: str | None = None) -> tuple[_State, uuid.UUID]:
    state = _State()
    defn = ComponentDefinition(
        name="R",
        meta_pins=(MetaPin(("1",)), MetaPin(("2",))),
        component_class=ComponentClass.RESISTOR,
        package_options=(PackageOption(name="r-tht/DIN/0207", ref="r-tht/DIN/0207"),),
    )
    state.components["R"] = defn
    inst = ComponentInstance(
        id=uuid.uuid4(),
        definition_ref="R",
        refdes="R1",
        value="10k",
        package_variant=package_variant,
    )
    state.instances[inst.id] = inst
    return state, inst.id


class TestApply:
    def test_setting_variant_updates_instance(self) -> None:
        state, comp_id = _make_state()
        assert state.instances[comp_id].package_variant is None
        cmd = SetComponentPackageVariantCommand(
            component_id=comp_id,
            new_variant="DIN-0411/typ",
        )
        cmd.apply(state)
        assert state.instances[comp_id].package_variant == "DIN-0411/typ"

    def test_clearing_variant_restores_default(self) -> None:
        state, comp_id = _make_state(package_variant="DIN-0411/typ")
        cmd = SetComponentPackageVariantCommand(
            component_id=comp_id,
            new_variant=None,
        )
        cmd.apply(state)
        assert state.instances[comp_id].package_variant is None

    def test_other_instance_fields_untouched(self) -> None:
        state, comp_id = _make_state()
        original = state.instances[comp_id]
        cmd = SetComponentPackageVariantCommand(
            component_id=comp_id,
            new_variant="r-tht/DIN/0617",
        )
        cmd.apply(state)
        updated = state.instances[comp_id]
        assert updated.refdes == original.refdes
        assert updated.value == original.value
        assert updated.id == original.id


class TestRevert:
    def test_revert_after_setting_restores_none(self) -> None:
        state, comp_id = _make_state()
        cmd = SetComponentPackageVariantCommand(
            component_id=comp_id,
            new_variant="DIN-0411/typ",
        )
        cmd.apply(state)
        cmd.revert(state)
        assert state.instances[comp_id].package_variant is None

    def test_revert_after_clearing_restores_previous(self) -> None:
        state, comp_id = _make_state(package_variant="DIN-0411/typ")
        cmd = SetComponentPackageVariantCommand(
            component_id=comp_id,
            new_variant=None,
        )
        cmd.apply(state)
        cmd.revert(state)
        assert state.instances[comp_id].package_variant == "DIN-0411/typ"

    def test_revert_overwrites_explicit_prior_value(self) -> None:
        state, comp_id = _make_state(package_variant="r-tht/DIN/0207")
        cmd = SetComponentPackageVariantCommand(
            component_id=comp_id,
            new_variant="r-tht/DIN/0617",
        )
        cmd.apply(state)
        cmd.revert(state)
        assert state.instances[comp_id].package_variant == "r-tht/DIN/0207"


class TestDescription:
    def test_description_when_setting(self) -> None:
        cmd = SetComponentPackageVariantCommand(
            component_id=uuid.uuid4(),
            new_variant="d-tht/DO/35",
        )
        assert "d-tht/DO/35" in cmd.description

    def test_description_when_clearing(self) -> None:
        cmd = SetComponentPackageVariantCommand(
            component_id=uuid.uuid4(),
            new_variant=None,
        )
        assert "default" in cmd.description.lower()
