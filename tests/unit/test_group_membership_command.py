# SPDX-License-Identifier: MIT
"""Tests for the GroupMembershipCommand base class.

GroupMembershipCommand factors out the shared skeleton of AddToGroupCommand
and RemoveFromGroupCommand: look the group up (raise if missing), snapshot it
for undo, swap in a group whose membership the subclass recomputes, recompute
the origin, and on revert restore the snapshot. Subclasses supply only the
membership-recompute hook (| vs -) and an optional pre-mutation validation
hook (Add checks for cycles; Remove does not).

These tests pin the base-class contract directly via a minimal concrete
subclass, independent of the real Add/Remove commands.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.commands.group_cmds import GroupMembershipCommand
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.group import ComponentGroup
from protoboard.domain.layout.layout import Layout
from protoboard.domain.schematic.schematic import Schematic


class _State:
    def __init__(self) -> None:
        self.board = Board(
            root=BoardGrid(
                width_policy=SizePolicy.HUG,
                height_policy=SizePolicy.HUG,
                cell=PadAtom(
                    width_policy=SizePolicy.HUG,
                    height_policy=SizePolicy.HUG,
                    layers=PadLayers(),
                ),
                rows=1,
                cols=1,
                row_gap=0.0,
                col_gap=0.0,
            )
        )
        self.layout = Layout()
        self.instances: dict[uuid.UUID, ComponentInstance] = {}
        self.components: dict[str, ComponentDefinition] = {}
        self.schematic = Schematic(nets={})


def _make_group(name: str) -> ComponentGroup:
    return ComponentGroup(id=uuid.uuid4(), name=name)


@dataclass
class _AddOne(GroupMembershipCommand):
    """Minimal concrete membership command: unions in component_ids."""

    extra: frozenset[uuid.UUID] = frozenset()
    validated: bool = False

    def _new_membership(
        self, group: ComponentGroup
    ) -> tuple[frozenset[uuid.UUID], frozenset[uuid.UUID]]:
        return (group.component_ids | self.extra, group.child_group_ids)

    def _validate(self, state: object, group: ComponentGroup) -> None:
        self.validated = True


def test_apply_missing_group_raises_keyerror() -> None:
    state = _State()
    cmd = _AddOne(group_id=uuid.uuid4(), extra=frozenset({uuid.uuid4()}))
    with pytest.raises(KeyError):
        cmd.apply(state)


def test_apply_recomputes_membership_via_hook() -> None:
    state = _State()
    g = _make_group("G")
    state.layout.groups[g.id] = g
    c1 = uuid.uuid4()
    cmd = _AddOne(group_id=g.id, extra=frozenset({c1}))
    cmd.apply(state)
    assert c1 in state.layout.groups[g.id].component_ids


def test_validate_hook_runs_before_mutation() -> None:
    state = _State()
    g = _make_group("G")
    state.layout.groups[g.id] = g
    cmd = _AddOne(group_id=g.id, extra=frozenset({uuid.uuid4()}))
    cmd.apply(state)
    assert cmd.validated is True


def test_revert_restores_snapshot() -> None:
    state = _State()
    g = _make_group("G")
    state.layout.groups[g.id] = g
    c1 = uuid.uuid4()
    cmd = _AddOne(group_id=g.id, extra=frozenset({c1}))
    cmd.apply(state)
    cmd.revert(state)
    assert c1 not in state.layout.groups[g.id].component_ids
    # Exact pre-apply group object is restored.
    assert state.layout.groups[g.id] is g


def test_revert_before_apply_is_noop() -> None:
    state = _State()
    g = _make_group("G")
    state.layout.groups[g.id] = g
    cmd = _AddOne(group_id=g.id, extra=frozenset({uuid.uuid4()}))
    # No apply → _old_group is None → revert must not touch the group.
    cmd.revert(state)
    assert state.layout.groups[g.id] is g
