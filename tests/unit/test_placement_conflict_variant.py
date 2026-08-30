# SPDX-License-Identifier: MIT
"""Regression: placement-conflict geometry must use the *incoming* component's
chosen variant footprint, not the definition default.

Bug: placing an R as a tight SMD-0805 (pin pitch 1 hole) was blocked by a bogus
conflict because the conflict check measured the incoming part's pin-2 against
the *default* axial footprint (pitch 3.2 holes). At placement time the instance
is not yet in ``session.instances``, so the check fell back to ``defn.default_footprint()``
unless the caller passes the (bound) incoming instance.
"""  # noqa: E501

from __future__ import annotations

import uuid

from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.package.family import resolve_variant
from protoboard.domain.layout.pad_conflict import find_conflicts
from protoboard.domain.layout.placement import (
    ComponentPlacement,
    pin_world_position,
)
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point


def _resistor_and_catalogue():  # type: ignore[no-untyped-def]
    from protoboard.paths import BUILTIN_LIBRARY_DIR
    from protoboard.services.library.loader import load_library
    from protoboard.services.library.package_family_loader import (
        load_full_package_catalogue,
    )

    families = load_full_package_catalogue()
    lib = load_library([BUILTIN_LIBRARY_DIR], families)
    return lib.get("R"), families


class TestIncomingVariantNotBlocked:
    def test_smd_incoming_not_blocked_at_default_pitch(self) -> None:
        """A bound SMD-0805 incoming R must report NO conflict on a pad that only
        the *default* (3.2-pitch) footprint would have collided with."""
        defn, families = self._catalogue_check()
        if defn is None:
            return

        # Existing neighbour: a placed R whose pin-1 sits exactly where the
        # incoming part's DEFAULT pin-2 would land (3.2 from incoming pin-1),
        # but NOT where the SMD variant's pin-2 lands (1 hole away).
        existing = ComponentInstance.new(
            definition_ref="R",
            refdes="R1",
            value="",
        ).bind(defn, families)
        # Incoming, bound to the tight SMD variant.
        incoming = ComponentInstance.new(
            definition_ref="R",
            refdes="R2",
            value="",
            package_variant="chip-smd/imperial/0805",
        ).bind(defn, families)

        incoming_pl = ComponentPlacement(
            component_id=incoming.id,
            position=Point(5.0, 5.0),
            rotation=0,
            side="top",
        )
        # Place the neighbour's pin-1 on the incoming DEFAULT pin-2 position.
        default_pin2 = pin_world_position(
            defn.default_footprint(families), incoming_pl, 2
        )
        variant = resolve_variant(families, "chip-smd/imperial/0805")
        assert variant is not None
        variant_pin2 = pin_world_position(variant.package, incoming_pl, 2)
        # The variant and default pin-2 must differ, else the test is vacuous.
        assert (default_pin2.x, default_pin2.y) != (variant_pin2.x, variant_pin2.y)

        # Neighbour positioned so ITS pin-1 == incoming DEFAULT pin-2.
        existing_pl = ComponentPlacement(
            component_id=existing.id,
            position=default_pin2,
            rotation=0,
            side="top",
        )

        instances = {existing.id: existing, incoming.id: incoming}
        components = {"R": defn}
        placements = {existing.id: existing_pl}

        conflicts = find_conflicts(
            incoming_placement=incoming_pl,
            incoming_defn=defn,
            incoming_instance_id=incoming.id,
            layout_placements=placements,
            instances=instances,
            components=components,
            schematic=Schematic(nets={}),
            incoming_instance=incoming,
        )
        # The incoming SMD pin-2 is one hole away, NOT on the neighbour pad —
        # so there must be no conflict. With the default footprint it would have
        # reported a spurious pad conflict at the 3.2-pitch position.
        assert (
            conflicts == []
        ), f"variant-aware incoming should not collide; got {conflicts}"

    def test_unbound_incoming_raises(self) -> None:
        """Control: WITHOUT a bound incoming instance the check no longer
        silently falls back to the default footprint — it raises, surfacing the
        forgot-to-bind bug instead of computing geometry against the wrong body.
        """
        import pytest

        from protoboard.errors import UnboundInstanceError

        defn, families = self._catalogue_check()
        if defn is None:
            return

        existing = ComponentInstance.new(
            definition_ref="R",
            refdes="R1",
            value="",
        ).bind(defn, families)
        incoming_id = uuid.uuid4()
        incoming_pl = ComponentPlacement(
            component_id=incoming_id,
            position=Point(5.0, 5.0),
            rotation=0,
            side="top",
        )
        default_pin2 = pin_world_position(existing.resolved_footprint(), incoming_pl, 2)
        existing_pl = ComponentPlacement(
            component_id=existing.id,
            position=default_pin2,
            rotation=0,
            side="top",
        )

        with pytest.raises(UnboundInstanceError):
            find_conflicts(
                incoming_placement=incoming_pl,
                incoming_defn=defn,
                incoming_instance_id=incoming_id,
                layout_placements={existing.id: existing_pl},
                instances={existing.id: existing},
                components={"R": defn},
                schematic=Schematic(nets={}),
                # no incoming_instance, and incoming_id not in instances →
                # the incoming geometry has no bound instance → raises.
            )

    def _catalogue_check(self):  # type: ignore[no-untyped-def]
        import pytest

        defn, families = _resistor_and_catalogue()
        if resolve_variant(families, "chip-smd/imperial/0805") is None:
            pytest.skip("SMD-0805 variant not in catalogue")
        return defn, families
