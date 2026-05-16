# Undo/Redo — Command pattern

## Interfejs

```python
class Command(ABC):
    @abstractmethod
    def apply(self, project: Project) -> None: ...
    @abstractmethod
    def revert(self, project: Project) -> None: ...
    description: str   # "Place R1 at (3,5)"

class CommandStack:
    def execute(self, cmd: Command) -> None: ...   # apply + push, clear redo
    def undo(self) -> None: ...
    def redo(self) -> None: ...
    def can_undo(self) -> bool: ...
    def can_redo(self) -> bool: ...
```

## Komendy

### Komponenty i druty
- `PlaceComponent`, `MoveComponent`, `RotateComponent`, `RemoveComponent`
- `AddWireWithNetsCommand` — atomowo commituje drut + mutacje schematu (create/extend/merge net); używany przez `RouteTool`
- `AddWireCommand` — prostszy wariant (wczytywanie projektu)
- `RemoveWireCommand`
- `ReplaceWireSegmentCommand(wire_id, new_path, old_path=[])` — zastępuje `path` drutu; `old_path` ważny przy mutacji przed wykonaniem (live drag)
- `DisconnectPinCommand(component_id, pin_number)` — usuwa `PinRef` z netu; jeśli net pusty, usuwa go
- `SetWireSideCommand`, `SetWireInsulatedCommand`
- `FlipWireFragmentCommand(wire_id, start_idx, end_idx, target_side)` — przenosi fragment na drugą stronę; cały drut → zmiana `wire.side`; częściowy → tworzy do 3 segmentów; wymaga plated-through padów na endpoints
- `AddStripCut`, `RemoveStripCut`
- `RenameNet`

### Grupy
- `CreateGroupCommand`, `DisbandGroupCommand`
- `AddToGroupCommand`, `RemoveFromGroupCommand`
- `RenameGroupCommand`
- `SetExportedPinsCommand`

Wszystkie operują na `Layout.groups: dict[UUID, ComponentGroup]`. Pomocniczy `_replace_group(g, **kwargs)` tworzy kopię grupy (frozen dataclass).

### Kompozycja
- `MacroCommand` — lista `Command[T]`; `apply()` po kolei, `revert()` w odwrotnej kolejności. Używana do multi-selection (move/delete) i autoroutingu.

## Wymagania stanu

Komendy mutujące schematic wymagają stanu implementującego `SchematicState(ComponentState)` z polem `schematic: Schematic`.
