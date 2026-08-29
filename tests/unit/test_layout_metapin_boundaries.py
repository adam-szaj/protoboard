# SPDX-License-Identifier: MIT
"""MetaPin translation at layout and routing boundaries."""

import uuid

from protoboard.domain.board.pad_state import PinItem
from protoboard.domain.components.definition import (
    ComponentDefinition,
    MetaPin,
    PackageOption,
)
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.package.generated_families import sil_family
from protoboard.domain.layout.group import ComponentGroup
from protoboard.domain.layout.group_tree import GroupTree
from protoboard.domain.layout.group_ownership import _pin_endpoint_matches_path
from protoboard.domain.layout.knot import PinKnot
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement, PlacementCollection
from protoboard.domain.layout.wire import WireCollection
from protoboard.domain.layout.wire_utils import _pin_at, find_pin_at
from protoboard.domain.layout.pin_pad_classification import (
    ModifierShape,
    _resolve_modifier,
)
from protoboard.domain.schematic.net import PinRef
from protoboard.geometry import Point
from protoboard.services.routing.autorouter import _build_pin_positions


def _placed_component() -> tuple[
    ComponentDefinition,
    ComponentInstance,
    Layout,
]:
    family = sil_family(2)
    assert family is not None
    definition = ComponentDefinition(
        name="X",
        meta_pins=(MetaPin(("SIGNAL",)),),
        package_options=(
            PackageOption(
                name="SIL-2",
                ref="sil-2/std",
                pin_map={1: (2,)},
            ),
        ),
    )
    instance = ComponentInstance.new("X", "X1", "").bind(
        definition, {family.family_id: family}
    )
    placement = ComponentPlacement(
        component_id=instance.id,
        position=Point(0.5, 0.5),
        rotation=0,
        side="top",
    )
    layout = Layout(
        placements=PlacementCollection({instance.id: placement}),
        wires=WireCollection(),
    )
    return definition, instance, layout


def test_wire_pin_lookup_translates_package_pin_to_metapin() -> None:
    definition, instance, layout = _placed_component()

    assert _pin_at(
        Point(0.5, 1.5),
        "top",
        layout,
        {instance.id: instance},
        {definition.name: definition},
    ) == PinRef(instance.id, 1)
    assert (
        _pin_at(
            Point(0.5, 0.5),
            "top",
            layout,
            {instance.id: instance},
            {definition.name: definition},
        )
        is None
    )


def test_interactive_pin_lookup_ignores_unmapped_package_pin() -> None:
    definition, instance, layout = _placed_component()

    assert find_pin_at(
        Point(0.5, 1.5),
        layout=layout,
        instances={instance.id: instance},
        components={definition.name: definition},
    ) == PinRef(instance.id, 1)
    assert (
        find_pin_at(
            Point(0.5, 0.5),
            layout=layout,
            instances={instance.id: instance},
            components={definition.name: definition},
        )
        is None
    )


def test_group_surface_contains_metapins_not_package_pins() -> None:
    definition, instance, _layout = _placed_component()
    group = ComponentGroup(
        id=uuid.uuid4(),
        name="G",
        component_ids=frozenset({instance.id}),
    )

    surface = GroupTree({group.id: group}).exposable_surface(
        group.id,
        {instance.id: instance},
        {definition.name: definition},
    )

    assert surface == frozenset({PinRef(instance.id, 1)})


def test_autorouter_locates_metapin_on_mapped_package_pin() -> None:
    definition, instance, layout = _placed_component()

    positions = _build_pin_positions(
        layout,
        {definition.name: definition},
        {instance.id: instance},
    )

    assert positions == {PinRef(instance.id, 1): (Point(0.5, 1.5), "top")}


def test_pin_pad_modifier_uses_metapin_identity() -> None:
    component_id = uuid.uuid4()
    item = PinItem(
        id=component_id,
        pin_number=2,
        meta_pin_index=1,
        net=None,
    )

    modifier = _resolve_modifier(
        [item],
        frozenset({PinRef(component_id, 1)}),
    )

    assert modifier is ModifierShape.STAR


def test_group_ownership_resolves_pinknot_metapin_to_package_pin() -> None:
    definition, instance, layout = _placed_component()
    knot = PinKnot(id=uuid.uuid4(), pin=PinRef(instance.id, 1))

    assert _pin_endpoint_matches_path(
        knot,
        Point(0.5, 1.5),
        layout.placements,
        {instance.id: instance},
        {definition.name: definition},
    )
