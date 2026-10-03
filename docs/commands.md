# Undo / redo — the command pattern

## Interface

```python
class Command(ABC, Generic[State]):
    @abstractmethod
    def apply(self, state: State) -> None: ...
    @abstractmethod
    def revert(self, state: State) -> None: ...
    description: str   # "Place R1 at (3, 5)"

class CommandStack:
    def execute(self, cmd: Command) -> None: ...   # apply + push, clear redo
    def undo(self) -> None: ...
    def redo(self) -> None: ...
    def can_undo(self) -> bool: ...
    def can_redo(self) -> bool: ...
```

## State protocols

Commands are parameterised over the state they mutate:

- `ComponentState` — placements, wires, knots, groups.
- `SchematicState(ComponentState)` — the above **plus** `schematic: Schematic`.
- `NetState(SchematicState)` — net-only mutations.

## Command inventory

### Components and placements (`component_cmds.py`)

| Command | Notes |
|---|---|
| `PlaceComponentCommand` | Place a fresh instance. |
| `MoveComponentCommand` | Move one component. With `placement_only=True` (the default since the unified-drag refactor) it only mutates `placement.position`; wire translation is delegated to `TranslateAnchoredWiresCommand`. |
| `RotateComponentCommand` | Rotate ±90° around the component origin. |
| `RemoveComponentCommand` | Delete a component (also removes orphan knots). |
| `LockComponentCommand` | Toggle `placement.locked`. |
| `SetPinVariantCommand` | Choose a discrete `pin_offsets` variant (bendable leads). |
| `SetComponentAppearanceCommand` | Set/clear the per-instance visual `style:` override (colour + corner radius); visual only. |
| `DisconnectPinCommand` | Remove a `PinRef` from its net; drops empty nets. |

### Wires (`component_cmds.py`)

| Command | Notes |
|---|---|
| `AddWireCommand` | Bare add (used by `load_project`). |
| `AddWireWithNetsCommand` | Atomic: adds the wire and applies the net diff (create / extend / merge). Used by `RouteTool`. |
| `RemoveWireCommand` | Remove a wire (drops empty knots). |
| `TranslateAnchoredWiresCommand` | Translate every wire anchored to a moved pin by the drag delta. Internal step of the unified drag pipeline. |
| `DropWiresCommand` | Remove a batch of wires (used by drag commit and by simplify). |
| `SetWireInsulatedCommand` | Toggle `wire.insulated`. |
| `SetWireSideCommand` | Move the whole wire to the other side (single fragment only). |
| `FlipWireFragmentCommand` | Move a `[start_idx, end_idx]` slice to the other side; a partial flip creates fragment boundaries and requires plated-through pads at the cut points. |
| `ReplaceWireFragmentsCommand` | Replace a wire's fragments tuple while preserving its id. Used by simplify, unweave, merge, and the drag pipeline. |

### Drag (`drag_cmds.py`)

| Command | Notes |
|---|---|
| `MoveDragSetCommand` | Atomic move of a drag set (single component, multi-selection, or one or more groups). Translates internal wires by one delta vector; drops external wires that the floating UX has already classified. Replaces the legacy `MoveComponentCommand(placement_only=False)` path. |

### Board (`board_cmds.py`)

| Command | Notes |
|---|---|
| `SplitStripCommand` | Cut a `StripAtom` between pads into two segments, or remove copper at one pad with an `isolated=True` hole between the remaining segments. |
| `ResizeBoardCommand` | Resize the board grid (warns if components fall off the new edge). |

### Groups (`group_cmds.py`)

| Command | Notes |
|---|---|
| `CreateGroupCommand` | New group containing the given component / sub-group ids. |
| `DisbandGroupCommand` | Drop a group; members revert to the parent. |
| `AddToGroupCommand` | Add components / sub-groups to a group. |
| `RemoveFromGroupCommand` | Inverse of the above. |
| `RenameGroupCommand` | Rename a group. |
| `SetExportedPinsCommand` | Update `exported_pins`. The DRC `GROUP_INVALID_EXPORT` check enforces the re-export chain. |
| `RotateGroupCommand` | Rotate every member around the group bbox centre. |

### Nets (`net_cmds.py`)

| Command | Notes |
|---|---|
| `RenameNetCommand` | Rename a net. |
| `SetNetGlobalCommand` | Toggle `is_global` (VCC / GND-style rails). |
| `SetNetColorCommand` | Override the auto-derived colour. |
| `CreateNetCommand` | Add an empty named net. |
| `DeleteNetCommand` | Remove a net (only when no pin is connected). |
| `AssignPinToNetCommand` | Add a `PinRef` to a net. |
| `UnassignPinCommand` | Inverse — exposed in the UI as "Unassign pin from net". |

### Composition

| Command | Notes |
|---|---|
| `MacroCommand` | A list of `Command[T]`. `apply()` runs them in order; `revert()` runs them in reverse. Used by multi-selection move / delete and by the autorouter. |

## State requirements

Schematic-mutating commands require a state that implements
`SchematicState(ComponentState)` with a `schematic: Schematic` field.
Net commands additionally require `NetState`.
