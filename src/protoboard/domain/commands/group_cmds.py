# SPDX-License-Identifier: MIT
"""Commands for creating, disbanding and modifying component groups."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from protoboard.domain.commands.base import Command
from protoboard.domain.commands.component_cmds import ComponentState
from protoboard.domain.layout.group import ComponentGroup, would_create_cycle


@dataclass
class CreateGroupCommand(Command[ComponentState]):
    """Create a new group from a set of component IDs."""

    group: ComponentGroup

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Create group '{self.group.name}'"

    def apply(self, state: ComponentState) -> None:
        state.layout.groups[self.group.id] = self.group

    def revert(self, state: ComponentState) -> None:
        state.layout.groups.pop(self.group.id, None)


@dataclass
class DisbandGroupCommand(Command[ComponentState]):
    """Remove a group (components remain placed, child groups become top-level)."""

    group_id: uuid.UUID
    _saved_group: ComponentGroup | None = field(
        default=None, init=False, repr=False
    )
    # Parent group that contained this group (if any), saved for undo.
    _saved_parent_id: uuid.UUID | None = field(
        default=None, init=False, repr=False
    )

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Disband group"

    def apply(self, state: ComponentState) -> None:
        group = state.layout.groups.pop(self.group_id, None)
        object.__setattr__(self, "_saved_group", group)
        # Find and patch parent group that references this group.
        for gid, g in state.layout.groups.items():
            if self.group_id in g.child_group_ids:
                object.__setattr__(self, "_saved_parent_id", gid)
                state.layout.groups[gid] = ComponentGroup(
                    id=g.id,
                    name=g.name,
                    component_ids=g.component_ids,
                    child_group_ids=g.child_group_ids - {self.group_id},
                )
                break

    def revert(self, state: ComponentState) -> None:
        if self._saved_group is not None:
            state.layout.groups[self.group_id] = self._saved_group
        if self._saved_parent_id is not None:
            parent = state.layout.groups.get(self._saved_parent_id)
            if parent is not None:
                state.layout.groups[self._saved_parent_id] = ComponentGroup(
                    id=parent.id,
                    name=parent.name,
                    component_ids=parent.component_ids,
                    child_group_ids=parent.child_group_ids | {self.group_id},
                )


@dataclass
class AddToGroupCommand(Command[ComponentState]):
    """Add component IDs and/or child group IDs to an existing group."""

    group_id: uuid.UUID
    component_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)
    child_group_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)
    _old_group: ComponentGroup | None = field(
        default=None, init=False, repr=False
    )

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Add to group"

    def apply(self, state: ComponentState) -> None:
        group = state.layout.groups.get(self.group_id)
        if group is None:
            raise KeyError(f"Group {self.group_id} not found")
        # Validate no cycles.
        for child_id in self.child_group_ids:
            if would_create_cycle(state.layout.groups, self.group_id, child_id):
                raise ValueError(
                    f"Adding group {child_id} to {self.group_id} would create a cycle"
                )
        object.__setattr__(self, "_old_group", group)
        state.layout.groups[self.group_id] = ComponentGroup(
            id=group.id,
            name=group.name,
            component_ids=group.component_ids | self.component_ids,
            child_group_ids=group.child_group_ids | self.child_group_ids,
        )

    def revert(self, state: ComponentState) -> None:
        if self._old_group is not None:
            state.layout.groups[self.group_id] = self._old_group


@dataclass
class RemoveFromGroupCommand(Command[ComponentState]):
    """Remove component IDs and/or child group IDs from a group."""

    group_id: uuid.UUID
    component_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)
    child_group_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)
    _old_group: ComponentGroup | None = field(
        default=None, init=False, repr=False
    )

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Remove from group"

    def apply(self, state: ComponentState) -> None:
        group = state.layout.groups.get(self.group_id)
        if group is None:
            raise KeyError(f"Group {self.group_id} not found")
        object.__setattr__(self, "_old_group", group)
        state.layout.groups[self.group_id] = ComponentGroup(
            id=group.id,
            name=group.name,
            component_ids=group.component_ids - self.component_ids,
            child_group_ids=group.child_group_ids - self.child_group_ids,
        )

    def revert(self, state: ComponentState) -> None:
        if self._old_group is not None:
            state.layout.groups[self.group_id] = self._old_group


@dataclass
class RenameGroupCommand(Command[ComponentState]):
    group_id: uuid.UUID
    new_name: str
    _old_name: str = field(default="", init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Rename group to '{self.new_name}'"

    def apply(self, state: ComponentState) -> None:
        group = state.layout.groups.get(self.group_id)
        if group is None:
            raise KeyError(f"Group {self.group_id} not found")
        object.__setattr__(self, "_old_name", group.name)
        state.layout.groups[self.group_id] = ComponentGroup(
            id=group.id,
            name=self.new_name,
            component_ids=group.component_ids,
            child_group_ids=group.child_group_ids,
        )

    def revert(self, state: ComponentState) -> None:
        group = state.layout.groups.get(self.group_id)
        if group is not None:
            state.layout.groups[self.group_id] = ComponentGroup(
                id=group.id,
                name=self._old_name,
                component_ids=group.component_ids,
                child_group_ids=group.child_group_ids,
            )
