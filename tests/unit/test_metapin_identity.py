# SPDX-License-Identifier: MIT
"""Instance-level PinRef identity follows MetaPins, never labels."""

import uuid

from protoboard.domain.components.definition import (
    ComponentDefinition,
    MetaPin,
    PackageOption,
)
from protoboard.domain.components.instance import ComponentInstance


def _definition() -> ComponentDefinition:
    return ComponentDefinition(
        name="U",
        meta_pins=(MetaPin(("GND",)), MetaPin(("GND",))),
        package_options=(
            PackageOption(
                name="default",
                ref="u/std",
                pin_map={1: (1, 3), 2: (2,)},
            ),
        ),
    )


def test_each_metapin_has_its_own_instance_identity_despite_equal_labels() -> None:
    instance = ComponentInstance(
        id=uuid.UUID(int=1),
        definition_ref="U",
        refdes="U1",
        value="U",
    )

    instance.build_pin_identities(_definition())
    first = instance.pin_ref(1)
    second = instance.pin_ref(2)

    assert first.meta_pin_index == 1
    assert second.meta_pin_index == 2
    assert first.identity is not None
    assert second.identity is not None
    assert first.identity is not second.identity


def test_rebuilding_metapin_identities_is_idempotent() -> None:
    instance = ComponentInstance(
        id=uuid.UUID(int=1),
        definition_ref="U",
        refdes="U1",
        value="U",
    )

    instance.build_pin_identities(_definition())
    instance.build_pin_identities(_definition())

    assert sorted(instance.pin_identities) == [1, 2]
