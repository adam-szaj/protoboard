# SPDX-License-Identifier: MIT
"""Canonical placed-component context resolution."""

from __future__ import annotations

import uuid

import pytest

from protoboard.domain.components.definition import (
    ComponentDefinition,
    MetaPin,
    PackageOption,
)
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.package.generated_families import sil_family
from protoboard.domain.components.resolution import (
    iter_placed_components,
    resolve_component,
    resolve_placed_component,
)
from protoboard.domain.layout.placement import (
    ComponentPlacement,
    iter_footprint_pin_positions,
    iter_pin_world_positions,
)
from protoboard.geometry import Point


def _context(*, bound: bool = True, package_variant: str | None = None) -> tuple[
    uuid.UUID,
    ComponentPlacement,
    ComponentInstance,
    ComponentDefinition,
    object,
]:
    family = sil_family(2)
    assert family is not None
    definition = ComponentDefinition(
        name="J2",
        meta_pins=(MetaPin(("1",)), MetaPin(("2",))),
        package_options=(
            PackageOption(
                name="SIL-2",
                ref="sil-2/std",
                pin_map={1: (1,), 2: (2,)},
            ),
        ),
    )
    instance = ComponentInstance.new(
        definition_ref="J2",
        refdes="J1",
        value="",
        package_variant=package_variant,
    )
    if bound:
        instance = instance.bind(definition, {"sil-2": family})
    placement = ComponentPlacement(
        component_id=instance.id,
        position=Point(2.5, 3.5),
        rotation=0,
        side="top",
    )
    return instance.id, placement, instance, definition, family.variants[0].package


def test_resolve_component_returns_complete_context() -> None:
    cid, placement, instance, definition, package = _context()

    resolved = resolve_component(
        cid,
        {cid: placement},
        {cid: instance},
        {definition.name: definition},
    )

    assert resolved is not None
    assert resolved.instance is instance
    assert resolved.definition is definition
    assert resolved.placement is placement
    assert resolved.package_option is definition.package_options[0]
    assert resolved.package is package


def test_resolve_placed_component_returns_structural_context() -> None:
    cid, placement, instance, definition, _ = _context(bound=False)

    resolved = resolve_placed_component(
        cid,
        {cid: placement},
        {cid: instance},
        {definition.name: definition},
    )

    assert resolved is not None
    assert resolved.placement is placement
    assert resolved.instance is instance
    assert resolved.definition is definition


def test_iter_placed_components_skips_unresolvable_records() -> None:
    cid, placement, instance, definition, _ = _context(bound=False)
    missing_id = uuid.uuid4()

    resolved = list(
        iter_placed_components(
            {cid: placement, missing_id: placement},
            {cid: instance},
            {definition.name: definition},
        )
    )

    assert resolved == [
        resolve_placed_component(
            cid,
            {cid: placement},
            {cid: instance},
            {definition.name: definition},
        )
    ]


def test_iter_pin_world_positions_uses_bound_variant_geometry() -> None:
    cid, placement, instance, definition, _ = _context()

    positions = list(
        iter_pin_world_positions(
            cid,
            placements={cid: placement},
            instances={cid: instance},
            components={definition.name: definition},
        )
    )

    assert positions == [(1, Point(2.5, 3.5)), (2, Point(2.5, 4.5))]


def test_iter_footprint_pin_positions_preserves_physical_pin_numbers() -> None:
    _cid, placement, _instance, _definition, package = _context()

    positions = list(iter_footprint_pin_positions(package, placement))

    assert positions == [(1, Point(2.5, 3.5)), (2, Point(2.5, 4.5))]


@pytest.mark.parametrize("missing", ["placement", "instance", "definition"])
def test_resolve_component_returns_none_for_missing_structural_record(
    missing: str,
) -> None:
    cid, placement, instance, definition, _ = _context()
    placements = {} if missing == "placement" else {cid: placement}
    instances = {} if missing == "instance" else {cid: instance}
    definitions = {} if missing == "definition" else {definition.name: definition}

    assert resolve_component(cid, placements, instances, definitions) is None


def test_resolve_component_keeps_unbound_package_explicit() -> None:
    cid, placement, instance, definition, _ = _context(bound=False)

    resolved = resolve_component(
        cid,
        {cid: placement},
        {cid: instance},
        {definition.name: definition},
    )

    assert resolved is not None
    assert resolved.package_option is definition.package_options[0]
    assert resolved.package is None


def test_resolve_component_does_not_hide_invalid_package_selection() -> None:
    cid, placement, instance, definition, _ = _context(package_variant="sil-2/missing")

    with pytest.raises(ValueError, match="package option"):
        resolve_component(
            cid,
            {cid: placement},
            {cid: instance},
            {definition.name: definition},
        )
