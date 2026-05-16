# GUI — układ i narzędzia

## Układ okna

```
┌──────────────────────────────────────────────────────────┐
│ Menu / Toolbar                                           │
├───────────┬────────────────────────────────┬─────────────┤
│           │                                │             │
│ Component │       Board View               │ Properties  │
│ Library   │       (QGraphicsView)          │ + DRC Panel │
│ (drzewo)  │       warstwy:                 │             │
│           │       - board                  │             │
│           │       - bottom wires           │             │
│           │       - components             │             │
│           │       - top wires              │             │
│           │       - overlay (DRC, hints)   │             │
├───────────┴────────────────────────────────┴─────────────┤
│ Status bar: pozycja kursora, jednostki, tryb             │
└──────────────────────────────────────────────────────────┘
```

## Menu

- **File:** New, Open, Save, Save As, Import KiCAD .net, Export KiCAD .net, Export SVG, Export BOM CSV, Export PNG, Print, Close Project, Exit.
- **Edit:** Undo (Ctrl+Z, dynamiczny label), Redo (Ctrl+Y), Select All (Ctrl+A), Group (Ctrl+G), Ungroup.
- **Tools:** Select (S), Route Wire (W), Cut Strip, DRC, Autoroute All Nets (F6).

## Skróty klawiaturowe

`M` move, `R` rotate, `Del` delete, `Ctrl+Z/Y` undo/redo, `Esc` cancel, `S` select, `W` route wire, `Ctrl+A` zaznacz wszystko, `Ctrl+G` group.

W trybie edycji fragmentu drutu: `Enter` zatwierdź, `Esc` anuluj, `A` autoroute, `F` flip side.

## SelectTool

`MainWindow` trzyma jedną trwałą instancję `SelectTool` (singleton) — stan `_group_stack` zachowany między przełączeniami narzędzi.

### Multi-selection

- `_selected_ids: set[UUID]` + `_selected_wire_ids: set[UUID]`.
- Rubber-band **LTR (lewo→prawo):** bbox musi być w całości wewnątrz prostokąta.
- Rubber-band **RTL (prawo→lewo):** bbox musi przecinać prostokąt; druty: Cohen-Sutherland clip test.
- Shift+klik dodaje/usuwa z zaznaczenia.
- Multi-drag: `MacroCommand` z `MoveComponentCommand` per komponent.
- Multi-delete: `MacroCommand` z `RemoveComponentCommand` + `RemoveWireCommand`.

### Wire editing

- Węzły (małe kółka) na każdym punkcie ścieżki zaznaczonego drutu.
- Ctrl+klik na węźle → `_edit_start_idx`; drugi Ctrl+klik → fragment zaznaczony (węzły czerwone).
- `A` → autoroute fragmentu (A*).
- `F` → `FlipWireFragmentCommand`; wymaga plated-through padów na endpoints fragmentu częściowego.
- Ręczne rysowanie → Enter → `ReplaceWireSegmentCommand`.
- Live drag węzła: Ctrl+drag; `_drag_orig_wire_paths` przechowuje oryginały dla undo.
- Prawy klik na drucie → menu "Flip fragment", "Move to layer", "Set bare/insulated".

### Grupy — nawigacja

- `_group_stack: list[UUID]` zaczyna od `[ROOT_GROUP_ID]`.
- Double-click na komponencie → push do jego child group.
- Esc → pop (nigdy nie usuwa ROOT_GROUP_ID).
- Klik na obrysie grupy (`"group_outline"`) → zaznacza wszystkich members, inicjuje drag.
- `_is_accessible(comp_id)`: na root level zawsze True; wewnątrz podgrupy — tylko bezpośredni members i proxy child groups.

### Kontekstowe menu (prawy klik)

- Na pinie: lista netów + "Disconnect pin from net" (`DisconnectPinCommand`).
- Na komponencie wewnątrz grupy: "Export/Unexport pin N" (`SetExportedPinsCommand`).

## Ctrl+G — grupowanie

`GroupTargetDialog` — "Utwórz nową grupę" lub "Dodaj do istniejącej". Nowa grupa rejestrowana jako child aktywnej grupy przez `AddToGroupCommand`.

## Render

`render_groups()` w `component_renderer.py`:
- Obrys tylko dla child groups aktywnej grupy (full opacity) i innych grup (dimmed).
- Root group i aktywna grupa bez obrysu.
- Eksportowane pady child groups: zielone romby (Z=12.0).
- `BoardView._active_group_id` przekazywane do rendererów przy każdym refresh.

## Zoom i pan

- Zoom do kursora: `ScrollBarAlwaysOn` + 50% sceneRect margin + korekta scrollbarów.
- Zoom in: ×1.1; zoom out: ×(1/1.08) — asymetryczne, nie zmieniać.
- Pan: środkowy przycisk i Ctrl+drag; `NoAnchor` zapobiega resetowi pozycji przy `setSceneRect`.
