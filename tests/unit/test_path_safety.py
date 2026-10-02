# SPDX-License-Identifier: MIT
"""Public routing safety checks retain legacy imports and decisions."""

from __future__ import annotations

import uuid

import pytest

from protoboard.domain.layout.wire import Wire
from protoboard.geometry import Point
from protoboard.services.routing import builtin_optimizers, path_safety, postprocess


@pytest.mark.parametrize(
    "name",
    [
        "candidate_pins_alien",
        "candidate_safe_against_others",
        "candidate_safe_via_pad_index",
        "parity_ok_for_bare",
    ],
)
def test_optimizers_use_same_public_safety_checks_as_postprocess(name: str) -> None:
    public = getattr(path_safety, name)
    assert getattr(builtin_optimizers, name) is public
    assert getattr(postprocess, f"_{name}") is public


@pytest.mark.parametrize(
    "name",
    [
        "_wire_to_segs",
        "_segs_cross_or_touch",
        "_segs_overlap_collinear",
        "_count_crossings_with_other",
        "_count_insulated_pad_contacts",
        "_seg_passes_through_pad",
    ],
)
def test_legacy_private_helper_imports_are_preserved(name: str) -> None:
    assert getattr(postprocess, name) is getattr(path_safety, name)


def test_public_safety_check_rejects_new_contact_and_accepts_existing() -> None:
    candidate = [(Point(0.5, 0.5), Point(2.5, 0.5))]
    other = Wire(
        id=uuid.uuid4(),
        path=[Point(1.5, -0.5), Point(1.5, 1.5)],
        side="top",
        insulated=False,
    )
    wire_id = uuid.uuid4()

    assert not path_safety.candidate_safe_against_others(
        candidate, [other], wire_id, wire_side="top"
    )
    assert path_safety.candidate_safe_against_others(
        candidate,
        [other],
        wire_id,
        wire_side="top",
        original_subpath_segs=candidate,
    )


def test_public_alien_pin_check_keeps_same_net_and_existing_contacts() -> None:
    vertices = [Point(0.5, 0.5), Point(2.5, 0.5)]
    candidate = [(vertices[0], vertices[1])]
    pin = Point(1.5, 0.5)
    own_net, other_net = uuid.uuid4(), uuid.uuid4()

    assert path_safety.candidate_pins_alien(
        candidate, vertices, {pin: other_net}, own_net, vertices
    )
    assert not path_safety.candidate_pins_alien(
        candidate, vertices, {pin: own_net}, own_net, vertices
    )
    assert not path_safety.candidate_pins_alien(
        candidate, vertices, {pin: other_net}, own_net, [vertices[0], pin, vertices[1]]
    )
