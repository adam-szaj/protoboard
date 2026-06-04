# Menu & Toolbar Audit + Redesign Plan

Status: draft, 2026-06-03. Owner: review with user before implementation.

Goal: inwentaryzacja wszystkich komend (Command classes) i akcji UI (ACTION_* / toolbar / PPM) z klasyfikacją tematyczną i zakresem działania → konkretna propozycja reorganizacji menu głównego i toolbarów.

---

## 1. Inwentarz: domain Commands (27)

Każda klasa `Command[State]` w `src/protoboard/domain/commands/`. Kolumna **Scope** odpowiada na pytanie *„na czym ta komenda operuje?”* — używamy 4 kategorii:

- **GLOBAL** — działa na całym projekcie/board, nie wymaga selekcji
- **SEL1** — wymaga dokładnie jednego itemu (komponent/druta/grupa/net)
- **SELN** — działa na zbiorze selekcji (1..n)
- **HIT** — operuje na celu wskazanym kursorem (np. tool click) niezależnie od selekcji

| # | Command | Topic | Scope | Plik |
|---|---|---|---|---|
| 1 | `PlaceComponentCommand` | placement | HIT | `component_cmds.py:99` |
| 2 | `MoveComponentCommand` | move/rotate | SEL1 | `component_cmds.py:119` |
| 3 | `MoveDragSetCommand` | move/rotate | SELN | `drag_cmds.py:52` |
| 4 | `RemoveComponentCommand` | remove | SELN | `component_cmds.py:178` |
| 5 | `LockComponentCommand` | move/rotate | SELN | `component_cmds.py:1464` |
| 6 | `AddWireCommand` | wires | HIT | `component_cmds.py:857` |
| 7 | `AddWireWithNetsCommand` | wires + connectivity | HIT | `component_cmds.py:966` |
| 8 | `RemoveWireCommand` | wires + remove | SELN | `component_cmds.py:1075` |
| 9 | `TranslateAnchoredWiresCommand` | wires (internal, drag) | SELN | `component_cmds.py:1125` |
| 10 | `DropWiresCommand` | wires (internal, drag) | SELN | `component_cmds.py:1198` |
| 11 | `SetWireInsulatedCommand` | wires | SELN | `component_cmds.py:1247` |
| 12 | `SetWireSideCommand` | wires | SELN | `component_cmds.py:1274` |
| 13 | `FlipWireFragmentCommand` | wires | SEL1 (fragment) | `component_cmds.py:1325` |
| 14 | `ReplaceWireFragmentsCommand` | wires (simplify/unweave backend) | SELN | `component_cmds.py:1498` |
| 15 | `SetPinVariantCommand` | placement (pad/bend) | SEL1 | `component_cmds.py:1301` |
| 16 | `DisconnectPinCommand` | connectivity | SEL1 | `component_cmds.py:1416` |
| 17 | `SplitStripCommand` | placement (board mod) | HIT | `board_cmds.py:116` |
| 18 | `ResizeBoardCommand` | placement (board mod) | GLOBAL | `board_cmds.py:146` |
| 19 | `CreateGroupCommand` | groups | SELN | `group_cmds.py:248` |
| 20 | `DisbandGroupCommand` | groups | SEL1 (group) | `group_cmds.py:265` |
| 21 | `AddToGroupCommand` | groups | SELN | `group_cmds.py:305` |
| 22 | `RemoveFromGroupCommand` | groups | SELN | `group_cmds.py:342` |
| 23 | `RenameGroupCommand` | groups | SEL1 | `group_cmds.py:373` |
| 24 | `SetExportedPinsCommand` | groups + connectivity | SEL1 | `group_cmds.py:396` |
| 25 | `RotateGroupCommand` | move/rotate (group) | SEL1 | `group_cmds.py:427` |
| 26 | `RenameNetCommand` | connectivity | SEL1 (net) | `net_cmds.py:26` |
| 27 | `SetNetGlobalCommand` | connectivity | SEL1 (net) | `net_cmds.py:54` |
| 28 | `SetNetColorCommand` | connectivity (cosmetic) | SEL1 (net) | `net_cmds.py:88` |
| 29 | `CreateNetCommand` | connectivity | GLOBAL | `net_cmds.py:117` |
| 30 | `DeleteNetCommand` | connectivity + remove | SEL1 (net) | `net_cmds.py:142` |
| 31 | `AssignPinToNetCommand` | connectivity | SEL1 (pin) | `net_cmds.py:177` |
| 32 | `UnassignPinCommand` | connectivity | SEL1 (pin) | `net_cmds.py:238` |
| 33 | `MacroCommand` | meta — wrap N children | — | `base.py:57` |

Uwagi:
- `AddWireWithNetsCommand` to wariant `AddWireCommand` tworzący nowe nety jako podkrok — księgujemy go pod **wires + connectivity**.
- `TranslateAnchoredWiresCommand`/`DropWiresCommand` to wewnętrzne kroki drag-pipeline; **nie mają** odpowiednika w menu/toolbar i nie powinny mieć.
- `ReplaceWireFragmentsCommand` jest backendem dla Simplify/Unweave/Merge — nie wystawiamy go bezpośrednio.

## 2. Inwentarz: UI Actions (~60)

Wszystkie zarejestrowane w `gui/keybindings.py` jako `ACTION_*` z domyślnym skrótem. Kolumna **Scope** używa tych samych kategorii (+ **MODE** dla narzędzi przełączających tryb edycji, + **VIEW** dla akcji czysto wizualnych nie mutujących modelu).

### 2.1 File / project
| Action | Domyślny skrót | Topic | Scope |
|---|---|---|---|
| `SAVE` | Ctrl+S | project | GLOBAL |
| `SAVE_AS` | Ctrl+Shift+S | project | GLOBAL |
| `QUIT` | Ctrl+Q | project | GLOBAL |
| (Import KiCAD .net) | — | project | GLOBAL |
| (Export KiCAD .net) | — | project | GLOBAL |
| (Export SVG/PDF/PNG/BOM) | — | project | GLOBAL |
| (Close Project) | — | project | GLOBAL |
| (Recent projects submenu) | — | project | GLOBAL |

### 2.2 Edit (history + global)
| Action | Skrót | Topic | Scope |
|---|---|---|---|
| `UNDO` | Ctrl+Z | history | GLOBAL |
| `REDO` | Ctrl+Y | history | GLOBAL |
| `PREFERENCES` | Ctrl+, | settings | GLOBAL |
| (Generate Shortcut Config File) | — | settings | GLOBAL |

### 2.3 Placement / board
| Action | Skrót | Topic | Scope |
|---|---|---|---|
| `TOOL_CUT` | C | placement (mode) | MODE |
| (Resize Board…) | — | placement | GLOBAL |
| (Spread Components) | — | placement | GLOBAL |

### 2.4 Remove
| Action | Skrót | Topic | Scope |
|---|---|---|---|
| `DELETE` | Del | remove | SELN |

### 2.5 Move / rotate
| Action | Skrót | Topic | Scope |
|---|---|---|---|
| `ROTATE_CW` | R | move/rotate | SELN |
| `ROTATE_CCW` | Shift+R | move/rotate | SELN |
| `FLIP` | Alt+R | move/rotate | SELN |
| `REDRAG_PINS` | Ctrl+R | move/rotate (pad bend) | SEL1 |

### 2.6 Connectivity (nets)
| Action | Skrót | Topic | Scope |
|---|---|---|---|
| `DISCONNECT_PIN` | — | connectivity | SEL1 (pin) |
| (Nets panel: Rename/Color/Global/Delete) | — | connectivity | SEL1 (net) |
| (Nets panel: Assign/Unassign pin) | — | connectivity | SEL1 (pin) |

### 2.7 Wires
| Action | Skrót | Topic | Scope |
|---|---|---|---|
| `TOOL_ROUTE` | W | wires (mode) | MODE |
| `WIRE_TOGGLE_INSULATED` | I | wires (mode-state) | MODE-state |
| `WIRE_SIDE_TOP` | T | wires (mode-state) | MODE-state |
| `WIRE_SIDE_BOTTOM` | B | wires (mode-state) | MODE-state |
| `WIRE_SET_INSULATED` | Ctrl+Shift+I | wires | SELN (wires) |
| `WIRE_SET_BARE` | Ctrl+Shift+B | wires | SELN (wires) |
| `WIRE_MOVE_TO_TOP` | Ctrl+T | wires | SELN (wires) |
| `WIRE_MOVE_TO_BOTTOM` | Ctrl+B | wires | SELN (wires) |
| `WIRE_FLIP` | Ctrl+F | wires | SELN (wires) |
| `SIMPLIFY_WIRE` | — | wires | SELN (wires) |
| `UNWEAVE_WIRES` | — | wires | SELN (wires≥2) |
| `MERGE_WIRE_FRAGMENTS` | — | wires | SELN (wires) |
| `TOOL_SIMPLIFY` | Shift+S | wires (mode) | MODE |

### 2.8 Groups
| Action | Skrót | Topic | Scope |
|---|---|---|---|
| `GROUP` | Ctrl+G | groups | SELN |
| `UNGROUP` | Ctrl+Shift+G | groups | SEL1 (group) |
| `EXIT_GROUP` | — | groups (nav) | GLOBAL |

### 2.9 Selection
| Action | Skrót | Topic | Scope |
|---|---|---|---|
| `TOOL_SELECT` | S | selection (mode) | MODE |
| `SELECT_ALL` | Ctrl+A | selection | GLOBAL |
| `DESELECT_ALL` | Ctrl+Shift+A | selection | GLOBAL |
| `SELECT_ALL_COMPONENTS` | — | selection | GLOBAL |
| `DESELECT_ALL_COMPONENTS` | — | selection | GLOBAL |
| `SELECT_ALL_WIRES` | — | selection | GLOBAL |
| `DESELECT_ALL_WIRES` | — | selection | GLOBAL |
| `FILTER_TOGGLE_COMPONENTS` | Alt+1 | selection (filter) | VIEW-state |
| `FILTER_TOGGLE_WIRES` | Alt+2 | selection (filter) | VIEW-state |
| `FILTER_TOGGLE_GROUPS` | Alt+3 | selection (filter) | VIEW-state |
| `HIGHLIGHT_CYCLE` | \` | highlight | VIEW |
| `RELATION_CYCLE_BACK` | Shift+\` | highlight | VIEW |
| `RELATION_NONE/LOGICAL_NET/PHYSICAL_NET/UNCONNECTED/NEIGHBORS` | — | highlight | VIEW-state |
| `CLEAR_HIGHLIGHT` | Ctrl+Shift+H | highlight | VIEW |
| `HIGHLIGHT_TOGGLE` | — | highlight (alias) | VIEW |

### 2.10 View
| Action | Skrót | Topic | Scope |
|---|---|---|---|
| `CAMERA_TOGGLE` | X | view | VIEW |
| `MIRROR_H` | H | view | VIEW |
| `MIRROR_V` | V | view | VIEW |
| `FULLSCREEN` | F11 | view | VIEW |
| `ZOOM_IN/OUT` | Ctrl+±  | view | VIEW |
| `FIT_BOARD/AREA/SELECTION` | Ctrl+0 / Ctrl+Shift+0 / Ctrl+Shift+F | view | VIEW |
| `LAYER_1..8` | 1..8 | view (layer toggle) | VIEW-state |
| (Camera: Top/Bottom radio) | — | view | VIEW-state |
| (Reset All Layers) | — | view | VIEW |

### 2.11 Analysis
| Action | Skrót | Topic | Scope |
|---|---|---|---|
| `RUN_DRC` | F5 | analysis | GLOBAL |
| `AUTOROUTE` | F6 | analysis (wires) | GLOBAL lub SELN (Inspector) |

---

## 3. Klasyfikacja tematyczna (kategorie z zadania)

| Topic | Commands | UI Actions |
|---|---|---|
| **placement** | PlaceComponent, SplitStrip, ResizeBoard, SetPinVariant | TOOL_CUT, Resize Board, Spread, Add Component (toolbar +) |
| **remove** | RemoveComponent, RemoveWire, DeleteNet | DELETE |
| **move/rotate** | MoveComponent, MoveDragSet, LockComponent, RotateGroup | ROTATE_CW, ROTATE_CCW, FLIP, REDRAG_PINS, drag (no action key) |
| **connectivity** | AddWireWithNets, DisconnectPin, Rename/SetGlobal/SetColor/Create/Delete Net, Assign/Unassign Pin, SetExportedPins | DISCONNECT_PIN, Nets-panel PPM |
| **wires** | AddWire, RemoveWire, TranslateAnchoredWires (internal), DropWires (internal), SetWireInsulated, SetWireSide, FlipWireFragment, ReplaceWireFragments (internal) | TOOL_ROUTE, WIRE_TOGGLE_INSULATED, WIRE_SIDE_TOP, WIRE_SIDE_BOTTOM, WIRE_SET_INSULATED, WIRE_SET_BARE, WIRE_MOVE_TO_TOP, WIRE_MOVE_TO_BOTTOM, WIRE_FLIP, SIMPLIFY_WIRE, UNWEAVE_WIRES, MERGE_WIRE_FRAGMENTS, TOOL_SIMPLIFY |
| **groups** | CreateGroup, DisbandGroup, AddToGroup, RemoveFromGroup, RenameGroup, SetExportedPins | GROUP, UNGROUP, EXIT_GROUP |
| *(meta)* selection/highlight/view/analysis | — | SELECT_*, FILTER_*, RELATION_*, LAYER_*, VIEW_*, RUN_DRC, AUTOROUTE |

## 4. Klasyfikacja po zakresie działania (project vs selection)

### Działają na całym projekcie (GLOBAL, nie wymagają selekcji)
- File: Save, Save As, Open Recent, Import/Export, Close, Quit
- Edit: Undo, Redo, Preferences
- Placement: Resize Board, Spread Components
- Selection: Select All / Deselect All / *-Components / *-Wires
- View: Camera toggle/top/bottom, Mirror H/V, Fullscreen, Zoom, Fit *, Layers 1..8, Reset Layers
- Highlight: Cycle Relation, Cycle Back, Clear, Set Relation by name
- Filter: components/wires/groups toggle
- Analysis: Run DRC, Autoroute (gdy brak Inspector-selection)
- Groups: Exit Group

### Wymagają selekcji (SEL1/SELN/HIT)
- **SELN (1..n dowolny mix lub konkretny typ)**: Delete, Rotate CW/CCW, Flip, Group; wire bulk: Set Insulated/Bare, Move to Top/Bottom, Flip; Simplify/Unweave/Merge wires; Move (drag)
- **SEL1**: Ungroup (grupa), Disconnect Pin (pin), Re-drag Pins (komponent), Flip Wire Fragment (fragment), Rename Group, Rename/Color/Global Net, Assign/Unassign Pin
- **HIT (kliknięcie narzędzia, brak preselekcji)**: Place Component, Split Strip (Cut tool), Add Wire (Route tool)

### Tryby narzędzi (MODE — przełączają sposób działania kliknięcia)
- Select (S), Route (W), Cut (C), Simplify (Shift+S)
- Stany narzędzia Route: Insulated (I), Side Top (T), Side Bottom (B)

---

## 5. Bolączki obecnego UI

1. **Toolbar duplikuje skróty klawiaturowe wpisane w tekst** („+ Add”, „⊙ Bottom [X]”, „Ins [I]”) — zaśmieca i nie pokazuje stanu (np. Insulated nie wygląda jak toggle).
2. **7 osobnych toolbarów na górze**: Tools, Selection, View, Zoom, Wire, Component, Analysis — zbyt dużo wierszy, mała hierarchia, kolejność losowa.
3. **Menu „Tools” miesza trzy poziomy**: tryby narzędzi (Select/Route/Cut/Simplify), atomowe wire-ops na selekcji (Set Insulated, Move to Top…), batch-ops (Simplify/Unweave/Merge), board mods (Resize Board), analizy (DRC/Autoroute/Spread). Trudno znaleźć cokolwiek.
4. **Brak osobnego menu „Component”** — komponenty są rozproszone: Add ląduje w toolbarze, Rotate/Flip w Edit, Place to klik w PlaceTool, Re-drag w Tools, Lock tylko w PPM.
5. **Brak osobnego menu „Wire”** — wszystko siedzi w „Tools” razem z resztą.
6. **„Generate Shortcut Config File” pod Edit** — to settings/devtools, nie Edit.
7. **`HIGHLIGHT_TOGGLE` to alias `CLEAR_HIGHLIGHT`** — dead action, mylące; albo zaimplementować, albo usunąć.
8. **Layer 8 (ratsnest) jako działanie w View > Layers** myli się z „warstwą fizyczną” — to filtr widoku połączeń, warto zmienić nazwę.
9. **Filtry selekcji (Alt+1/2/3)** są niewidoczne w UI — nie ma checkboxów ani toolbar-toggle’i; user musi pamiętać.
10. **Camera toggle (X) i Mirror (H/V)** są w View ale też w toolbarze View — duplikacja jest OK, ale brak wskaźnika *bieżącego* stanu w toolbarze (`_act_camera_tb` ma label „⊙ Bottom [X]” bez aktualizacji).

---

## 6. Propozycja: nowa struktura menu (top-level)

**Decyzja 2026-06-03 (user):** scalamy. Top-level pozostaje krótki:

```
File   Edit   Select   View   Analysis   Help
```

`Edit` jest hubem dla wszystkich operacji edycji modelu i grupuje podmenu wg tematu:

```
Edit
├── Undo / Redo
├── Design ►
│   ├── Component ►   (Rotate / Flip / Lock / Re-drag / Delete / Add…)
│   ├── Wire      ►   (Route mode + wire options + bulk wire ops + Simplify/Unweave/Merge)
│   ├── Net       ►   (New / Rename / Color / Toggle Global / Delete / Assign / Unassign pin)
│   └── Group     ►   (Group / Ungroup / Rename / Exported pins / Exit Group)
├── Board ►
│   ├── Resize Board…
│   ├── Cut Strip Tool        C       [MODE]
│   └── Spread Components
├── Preferences…              Ctrl+,
└── Keyboard Shortcuts…
```

**Decyzja 2026-06-03 (user):** Cut Strip Tool ląduje pod `Edit ► Board ►` razem z Resize Board i Spread — to operacje na płytce, nie na komponentach/drutach ani na analizie.

### 6.1 File
```
Save                          Ctrl+S
Save As…                      Ctrl+Shift+S
Open Recent                ►
─────
Import KiCAD .net…
Export                     ►  (KiCAD .net / SVG / PDF / PNG / BOM CSV)
─────
Close Project
Quit                          Ctrl+Q
```

### 6.2 Edit (history + Design hub + Board hub + settings)
```
Undo                          Ctrl+Z
Redo                          Ctrl+Y
─────
Design                     ►  (Component / Wire / Net / Group — patrz §6.5–6.8)
Board                      ►  (Resize / Cut Strip / Spread — patrz §6.9)
─────
Preferences…                  Ctrl+,
Keyboard Shortcuts…           (przenieść tu „Generate Shortcut Config File”)
```

### 6.3 Select (selekcja + filtry + highlight — wszystko meta)
```
Select All                    Ctrl+A
Deselect All                  Ctrl+Shift+A
─────
Select All Components
Select All Wires
Select All Groups            (nowe, jeśli istotne)
─────
Filter                     ► Components (Alt+1) / Wires (Alt+2) / Groups (Alt+3)
Relation                   ► None / Same Logical Net / Same Physical Net / Unconnected / Neighbors
Cycle Relation                `
Cycle Relation (Back)         Shift+`
Clear Highlight               Ctrl+Shift+H
```

### 6.4 View
```
Camera Top
Camera Bottom
Toggle Camera                 X
─────
Mirror Horizontal             H   (checkable)
Mirror Vertical               V   (checkable)
─────
Zoom In / Out                 Ctrl+± 
Fit Board / Area / Selection  Ctrl+0 / Ctrl+Shift+0 / Ctrl+Shift+F
─────
Layers                     ► (Board, Components Top/Bottom, Wires Top/Bottom, Labels, Pin Names, **Ratsnest** — rename z „Connections”)
                              Reset All Layers
─────
Full Screen                   F11
```

### 6.5 Edit ► Design ► Component  *(SEL1/SELN, **bez** wire/net/group)*
```
Add Component…                              (HIT — otwiera library/dialog, lub aktywuje PlaceTool)
─────
Rotate CW                     R              [SELN]
Rotate CCW                    Shift+R        [SELN]
Flip (2-pin)                  Alt+R          [SELN]
Re-drag Pins                  Ctrl+R         [SEL1]
─────
Lock / Unlock                                 [SELN]      (checkable label switches)
─────
Delete                        Del            [SELN]
```

### 6.6 Edit ► Design ► Wire
```
Route Tool                    W              [MODE]
  Insulated                   I              [MODE-state, checkable]
  Side: Top                   T              [MODE-state, radio]
  Side: Bottom                B              [MODE-state, radio]
─────
Selected Wires:
  Set Insulated               Ctrl+Shift+I   [SELN]
  Set Bare                    Ctrl+Shift+B   [SELN]
  Move to Top Layer           Ctrl+T         [SELN]
  Move to Bottom Layer        Ctrl+B         [SELN]
  Flip to Opposite Layer      Ctrl+F         [SELN]
─────
Simplify Tool                 Shift+S        [MODE]
Simplify Selected             —              [SELN]
Unweave Selected              —              [SELN ≥2]
Merge Same-Side Fragments     —              [SELN]
─────
Delete Wire                   Del            (współdzielone z Component → Delete na selekcji wszystkiego)
```

### 6.7 Edit ► Design ► Net
```
New Net…                                     [GLOBAL]
Rename Net…                                  [SEL1 net]
Set Net Color…                               [SEL1 net]
Toggle Global                                 [SEL1 net]
Delete Net                                   [SEL1 net]
─────
Assign Pin to Net…                            [SEL1 pin]
Unassign Pin from Net        (was: DISCONNECT_PIN)  [SEL1 pin]
```

### 6.8 Edit ► Design ► Group
```
Group Selection               Ctrl+G          [SELN ≥2]
Ungroup                       Ctrl+Shift+G    [SEL1 group]
Exit Group                                    [GLOBAL]
Rename Group…                                 [SEL1 group]
Edit Exported Pins…                           [SEL1 group]
```

### 6.9 Edit ► Board
```
Resize Board…                                [GLOBAL]
Cut Strip Tool                C              [MODE — board edit]
Spread Components                            [GLOBAL]
```

### 6.10 Analysis
```
Run DRC                       F5             [GLOBAL]
Autoroute…                    F6             [GLOBAL / SELN via Inspector]
```

### 6.11 Help
```
Keyboard Shortcuts Reference
About
```

---

## 7. Propozycja: nowe toolbary

Cel: **2 paski** zamiast 7, czytelne grupy z separatorami, ikony zamiast tekstu, aktualizacja stanu (checked).

### Toolbar A — „Workflow” (lewy/górny, primary)
| Grupa | Pozycje |
|---|---|
| File | Save • Undo • Redo |
| Tools | **Select (S)** • **Route (W)** • **Simplify (Shift+S)** • **Cut (C)** *— exclusive radio group, aktywne narzędzie podświetlone* |
| Component | Add • Rotate CW • Rotate CCW • Flip • Delete |
| Wire | Toggle Insulated *(checked when ON)* • Side Top / Side Bottom *(radio para)* • Move-to-Top • Move-to-Bottom • Flip Fragment |
| Analysis | DRC (F5) • Autoroute (F6) |

### Toolbar B — „View” (prawy/górny, secondary)
| Grupa | Pozycje |
|---|---|
| Camera | Camera Toggle *(label „Top”/„Bottom” updates)* |
| Mirror | Mirror H • Mirror V *(both checkable)* |
| Zoom | Zoom In • Zoom Out • Fit Board • Fit Area • Fit Selection |
| Layers | przycisk-dropdown „Layers” z 1..8 + Reset |
| Filter | Components • Wires • Groups *(3 checkable toggles, Alt+1/2/3)* |
| Highlight | Cycle Relation • Clear |

**Eliminujemy** osobne `tb_selection`, `tb_zoom`, `tb_wire`, `tb_comp`, `tb_analysis` jako odrębne `QToolBar`-y — wszystko ląduje w A lub B z separatorami.

### Status bar (dol)
- Current tool name, current wire side, insulated on/off, camera side, mirror flags — czytelne status indicators zamiast doczepiania ich do toolbara.

---

## 8. PPM (context menu) — bez zmian semantycznych, drobne uporządkowanie

`SelectTool._build_context_menu` (`select_tool.py:2894+`) jest dobrze pomyślany: top-level: highlight ops; sub-menu: Export pins, Disconnect pin, Net-based selection, Wire ops, Simplify variants, Unweave, Merge. **Zmiany sugerowane:**

1. Wszędzie używaj polskich/angielskich nazw spójnie (obecnie miks „Wyśrodkuj widok tutaj” obok „Anuluj narzędzie (Esc)” i angielskich net actions). Wybrać jeden język UI.
2. Lock/Unlock dodać też do menu top-level „Component” (obecnie tylko PPM).
3. „Highlight selection (Dimmed #2)” → krótsze „Pin to highlight”.

---

## 9. Cleanup po stronie kodu

1. **Usunąć `ACTION_HIGHLIGHT_TOGGLE`** jako alias `CLEAR_HIGHLIGHT` (`main_window.py:596`). Albo zaimplementować osobne zachowanie, albo zniknąć.
2. **Zunifikować budowę toolbarów**: zamiast 7× `self.addToolBar(...)` zrobić jedną metodę `_add_toolbar_group(toolbar, label, action_ids)` z separatorami.
3. **Status indicators**: `_act_camera_tb`, `_act_tb_insulated`, `_act_tb_top`, `_act_tb_bottom` mają tekst z literką skrótu wbudowanym (`"Ins [I]"`). Zastąpić: `QAction(checkable=True)` z `text=self._actions[KB.ACTION_…].text()`, skrót w `shortcut`, a stan przez `setChecked()`. Skróty wtedy pokaże tooltip Qt.
4. **Wprowadzić exclusive `QActionGroup`** dla 4 narzędzi (Select/Route/Cut/Simplify) → automatic radio behavior.
5. **Menu Tools** rozbity na **Component / Wire / Net / Group / Analysis** zgodnie z §6.
6. **`ACTION_TOOL_CUT` (C)** — to *board edit* nie wire; przenieść do Analysis (lub osobnego „Board”) wraz z Resize/Spread.

---

## 10. Decyzje

**Podjęte (2026-06-03, user):**
1. ✅ **Top-menu scalone**: `File · Edit · Select · View · Analysis · Help` (6). Component/Wire/Net/Group żyją pod `Edit ► Design ►`.
2. ✅ **Cut Strip Tool** ląduje pod `Edit ► Board ►` razem z Resize Board i Spread Components.

**Otwarte (do potwierdzenia przed implementacją):**
3. ✅ **Add Component** (2026-06-03, user): w najprostszej wersji **aktywuje panel Library** — fokusuje/pokazuje `LibraryPanel` (jeśli ukryty, raise; jeśli widoczny, set focus + pole search). Bez dialogu, bez auto-aktywacji PlaceTool — drag-drop z panelu zostaje istniejącym mechanizmem placement’u. Implementacja: nowa `ACTION_FOCUS_LIBRARY` w `keybindings.py` + handler w `main_window` wołający `self._library_panel.raise_()` + `setFocus()` (lub `show()` jeśli dockwidget zamknięty).
4. ✅ **Lock/Unlock** (2026-06-03, user): osobne akcje w `keybindings.py` z domyślnym skrótem (proponowane: `Ctrl+L` toggle / `Ctrl+Shift+L` dla "Unlock all in selection" jeśli mieszane) i pozycją w `Edit ► Design ► Component`. **Reguła ogólna potwierdzona przez user’a:** każda akcja UI o sensownym znaczeniu (menu / PPM / toolbar) ma przypisany action-id w `keybindings.py` z możliwością nadania skrótu (domyślny pusty `""` jest OK). Konsekwencja dla planu: §11 faza 1 musi rejestrować action-ids także dla obecnie-anonimowych pozycji menu (Import/Export, Resize Board, Spread, Reset Layers, Net ops, Group rename/exported pins, Recent placeholder).
5. ✅ **Język UI** (2026-06-03, user): **tylko angielski** na razie. Inwentarz PL-stringów do przetłumaczenia (grep `[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]` w `gui/`):
   - `gui/tools/route_tool.py:83` — statusbar "Pad należy do grupy…"
   - `gui/panels/properties_panel.py:446` — label "Zmień: Ctrl+R na planszy"
   - `gui/tools/select_tool.py:999, 1112, 1265, 1368, 1370, 1386, 1388-89, 1415, 2591-92, 2898, 2900, 3929-30, 4574, 4645` — drag overlay messages, PPM "Wyśrodkuj widok tutaj"/"Anuluj narzędzie (Esc)", dialog titles "Usunąć wolne druty?" / "Usunąć druty obcej grupy?", status messages "Usunięto N drut(ów)…"
   Łącznie ~20 stringów w 3 plikach. Faza tłumaczenia osobno (§11 faza 7), niezależnie od refaktoru menu.
6. ⏳/✅ **Skróty** (2026-06-03, user): *"dodawanie komponentów w KiCAD jest przez skrót 'a', mi to pasuje. innych preferencji chwilowo nie mam"*. Konkretna decyzja: **`A` na Add Component** (focus LibraryPanel) — zgodnie z konwencją KiCAD. Reszta propozycji (a–k poniżej) **nie jest forsowana**: zostaje jako sugestie do późniejszego przeglądu, default shortcuts NIE zmieniamy w fazie 1. User override w `~/.config/protoboard/keybindings.yaml` ma pierwszeństwo i wciąż działa.

   Aktualne overrides: `redrag_pins=E`, `unweave_wires=U`, `highlight_toggle=Space`, `relation_none=Ctrl+Alt+5`, `tool_route_net_aware=W`.

   **Analiza obecnych skrótów single-letter (zajęte: R/S/T/B/I/W/C/H/V/X/E/U/Space):**

   | Skrót | Akcja teraz | Uwaga |
   |---|---|---|
   | R | Rotate CW | OK, intuicyjne |
   | Shift+R | Rotate CCW | OK |
   | Alt+R | Flip | konflikt? OK na razie |
   | Ctrl+R | Re-drag Pins | **mylące** — user `keybindings.yaml` przemapował na `E` |
   | S | Select tool | OK |
   | Shift+S | Simplify tool | OK |
   | T | Wire side: Top | OK w trybie Route |
   | B | Wire side: Bottom | OK w trybie Route |
   | I | Toggle Insulated | OK w trybie Route |
   | W | Route tool | OK (user już na `tool_route_net_aware=W`) |
   | C | Cut Strip tool | **kolizja z "Copy"** gdyby dodać schowek |
   | H | Mirror H | OK |
   | V | Mirror V | OK |
   | X | Camera toggle | OK |
   | E | Re-drag Pins (user override) | OK |
   | U | Unweave (user override) | OK |
   | Space | Highlight toggle (user override) | OK |
   | \` | Cycle Relation | OK |
   | Shift+\` | Cycle Relation Back | OK |

   **Propozycje zmian:**

   a) **Cut Strip Tool**: `C` → `K` (free litera, "cut/kut", brak kolizji z potencjalnym Copy). Alternatywa: `Shift+C` (bezpieczne, ale dwa klawisze).

   b) **Re-drag Pins**: skoro user już ustawił `E`, podnieść do domyślnego — `Ctrl+R` (obecny default) jest mylące (user oczekuje Rotate). Wolne `Ctrl+R` można zostawić jako alias albo wykorzystać na `Refresh DRC` / `Reload Library`.

   c) **Unweave Wires**: user ustawił `U` — podnieść do domyślnego.

   d) **Highlight Toggle (alias dla Clear)**: user ustawił `Space`. Jeśli zostawimy alias (§9 cleanup item 1: rozważamy usunięcie), Space jest dobry. Alternatywa: jeśli `HIGHLIGHT_TOGGLE` zostanie zaimplementowane jako prawdziwy toggle (zachowaj/przywróć ostatnie ustawienie highlight), Space bardzo intuicyjne.

   e) **Wire bulk ops** (Ctrl+Shift+I/B, Ctrl+T/B, Ctrl+F) — działają, ale `Ctrl+T` (Move to Top) koliduje z konwencjonalnym "New Tab" w przeglądarkach; `Ctrl+B` (Move to Bottom) z "Bold". Nie kolidują wewnątrz aplikacji, ale są dziwne dla świeżych użytkowników. Propozycja: pakiet `Ctrl+Alt+T/B/F/I` (Top/Bottom/Flip/Insulated) — spójna rodzina.

   f) **Simplify Wire (batch action)** + **Merge Fragments** — obecnie puste; propozycje: `Shift+J` (join/simplify), `Shift+M` (merge). Lub jeden klawisz `J` na batch-simplify.

   g) **Filter toggles** (Alt+1/2/3) — dobry pomysł, ale `Alt+1` koliduje na X11 ze switcherem okien w niektórych WM. Alternatywa: `Ctrl+Alt+C/W/G` (Components/Wires/Groups).

   h) **Layer toggles** (1..8) — zachować, ale dodać `Shift+1..8` jako "solo layer" (pokaż tylko tę).

   i) **Add Component** (focus Library) — proponuję `L` (Library) lub `Ctrl+L`. Pojedyncze `L` wolne.

   j) **Lock/Unlock** — `Ctrl+L` jeśli Library dostanie `L`. Albo `M` (Mark/lock).

   k) **DRC**: F5 — OK (konwencja "run").
   **Autoroute**: F6 — OK.
   Brakuje: **Autoroute Selection** (z PPM tylko); proponuję `Shift+F6`.

   **Zatwierdzone do fazy 1:**
   ```yaml
   focus_library: "A"             # NEW (KiCAD convention: Add Component)
   ```

   **Nie zmieniamy** (na razie):
   - `tool_cut` zostaje `C` (decyzja: live with potential Copy collision).
   - User overrides (E/U/Space/Ctrl+Alt+5/W) **nie są promowane** do defaults — user może mieć je tylko u siebie.
   - Lock/Unlock dostaje action-id (§10.4) ale **bez domyślnego skrótu** (`""`).
   - Simplify / Merge / Autoroute-Selection dostają action-ids bez domyślnego skrótu.

   Propozycje a–k zachowane w dokumencie jako pomysły do późniejszego przeglądu.
7. ✅ **Wire side Top/Bottom** (2026-06-03, user): **zawsze widoczne** w `Edit ► Design ► Wire` (oraz w toolbarze A → Wire group). Pozycje zostają enabled niezależnie od aktywnego narzędzia — kliknięcie ustawia preferencję trybu Route (per-session state w `self._wire_side`), która zostanie użyta gdy user włączy Route. Sygnalizacja: checkable radio para (`QActionGroup` exclusive: Top vs Bottom) + checkable toggle Insulated, wszystkie sync z modelem. To samo dotyczy `WIRE_TOGGLE_INSULATED` (I).

---

## 11. Plan implementacji (po decyzjach)

Fazami, każda z testem smoke:

1. **Refactor `_register_actions`** — **reguła**: każda pozycja menu/PPM/toolbar dostaje action-id w `keybindings.py` (domyślny skrót może być pusty `""`); żadnych anonimowych `menu.addAction("…")` z lambdą. Konkretne brakujące do dodania:
   - `ACTION_FOCUS_LIBRARY` (Add Component → focus panel Library)
   - `ACTION_LOCK_TOGGLE` (Ctrl+L, dla SEL1/SELN; checkable label switches Lock/Unlock)
   - `ACTION_NET_NEW` / `ACTION_NET_RENAME` / `ACTION_NET_SET_COLOR` / `ACTION_NET_CLEAR_COLOR` / `ACTION_NET_TOGGLE_GLOBAL` / `ACTION_NET_DELETE`
   - `ACTION_PIN_ASSIGN_NET` (rename z `ASSIGN_PIN_TO_NET`) / `ACTION_PIN_UNASSIGN_NET` (alias dla istniejącego `DISCONNECT_PIN`)
   - `ACTION_GROUP_RENAME` / `ACTION_GROUP_EDIT_EXPORTED_PINS`
   - `ACTION_BOARD_RESIZE` / `ACTION_BOARD_SPREAD` / `ACTION_LAYERS_RESET`
   - `ACTION_IMPORT_KICAD_NET` / `ACTION_EXPORT_KICAD_NET` / `ACTION_EXPORT_SVG` / `ACTION_EXPORT_PDF` / `ACTION_EXPORT_PNG` / `ACTION_EXPORT_BOM` / `ACTION_CLOSE_PROJECT`
   - `ACTION_GEN_SHORTCUT_CONFIG` (przeniesione z Edit do Edit ► Preferences/Keyboard Shortcuts)
   Po refaktorze cały `_build_menu` powinien używać wyłącznie `self._actions[KB.ACTION_X]`.
2. **Wprowadź `_add_toolbar_group()`** helper i przepisz `_build_toolbar` na 2 toolbary (A/B).
3. **`QActionGroup` dla 4 narzędzi** + checkable state-actions dla Insulated/Side/Mirror/Camera + sync z modelem (`_wire_insulated`, `_wire_side`, `_camera_side`).
4. **Przebuduj `_build_menu`** wg §6, w `Edit` zostaje tylko historia + Preferences + Keyboard Shortcuts.
5. ~~Status bar widgets zamiast tekstu w toolbarze.~~ **Anulowane (2026-06-03)**: po fazie 3 (checkable QActions + QActionGroup sync) statusbar już pokazuje pełny stan w `_mode_label` (Route mode wypisuje insulated/side; Select/Cut/Simplify mają własne etykiety) i `_view_label` (camera+mirror). Toolbar Wire/View duplikuje to jako szybkie checkable toggle — to świadoma redundancja, daje 1-klik dostęp. Trzy źródła (menu/toolbar/statusbar) są synced przez `_set_wire_side`/`_set_wire_insulated`/`_sync_view_actions`. Faza zbędna.
6. **PPM cleanup** — język + Lock/Unlock w głównym menu.
7. **Usuń `ACTION_HIGHLIGHT_TOGGLE`** alias.

Każda faza: 1 commit, `pytest -q` zielone, smoke test ręczny.
