# GUI — layout, tools, shortcuts

## Window layout

```
┌──────────────────────────────────────────────────────────┐
│ Menu / Toolbars                                          │
├───────────┬────────────────────────────────┬─────────────┤
│           │                                │             │
│ Component │       Board View               │ Properties  │
│ Library   │       (QGraphicsView)          │ + DRC panel │
│ (tree)    │       Z-banded layers:         │             │
│           │       - board                  │             │
│           │       - bottom wires           │             │
│           │       - components             │             │
│           │       - top wires              │             │
│           │       - overlay (DRC, hints)   │             │
├───────────┴────────────────────────────────┴─────────────┤
│ Status bar: tool, cursor position, units, side, mirror   │
└──────────────────────────────────────────────────────────┘
```

## Menu

The post-2026-06-03 redesign collapses the top bar to six items —
`File · Edit · Select · View · Analysis · Help`. Component, Wire, Net and
Group actions live under `Edit ▸ Design ▸ …`; board-level edits live
under `Edit ▸ Board ▸ …`.

```
File
  Save / Save As… / Open Recent ►
  Import KiCAD .net… / Export ► (KiCAD .net / SVG / PDF / PNG / BOM CSV)
  Close Project
  Quit

Edit
  Undo / Redo
  Design ►
    Component ► (Add Component, Rotate CW, Rotate CCW, Flip, Move to Other Side,
                 Re-drag Pins, Lock / Unlock, Delete)
    Wire      ► (Route Tool, Insulated toggle, Side: Top, Side: Bottom,
                 Set Insulated / Set Bare, Move to Top / Bottom,
                 Flip to Opposite Layer, Simplify Tool, Simplify Selected,
                 Unweave Selected, Merge Same-Side Fragments,
                 Convert endpoint to / from wire-pin)
    Net       ► (New, Rename, Set Color, Clear Color, Toggle Global,
                 Delete, Assign Pin, Unassign Pin)
    Group     ► (Group, Ungroup, Exit Group, Rename, Edit Exported Pins,
                 Export Auto / Export All / Clear Exports,
                 Autoroute Group)
  Board ►
    Resize Board…
    Cut Strip Tool
    Spread Components
  Preferences…
  Keyboard Shortcuts…
  Customize Toolbars…

Select
  Select All / Deselect All
  Select All Components / Select All Wires
  Filter ► (Components / Wires / Groups)
  Relation ► (None / Logical Net / Physical Net / Unconnected / Neighbors)
  Cycle Relation / Cycle Back / Clear Highlight

View
  Camera Top / Bottom / Toggle
  Mirror H / Mirror V (checkable)
  Zoom In / Out / Fit Board / Fit Area / Fit Selection
  Layers ► (1..0 toggle list + Reset)
  Full Screen

Analysis
  Run DRC (F5)
  Autoroute… (F6)

Help
  Keyboard Shortcuts Reference
  About
```

## Toolbars

Two toolbars (Workflow + View) replace the seven that existed before the
2026-06-03 redesign. Both are customisable via **Edit ▸ Customize
Toolbars…**, which writes `~/.config/protoboard/toolbars.yaml`.

## Keyboard shortcuts (defaults)

Every menu / context / toolbar action has an entry in
`gui/keybindings.py` (`ACTION_*`). Empty defaults are common — the user
can bind them through Preferences. The non-empty defaults:

| Action | Default |
|---|---|
| Save / Save As / Undo / Redo / Quit | `Ctrl+S` / `Ctrl+Shift+S` / `Ctrl+Z` / `Ctrl+Y` / `Ctrl+Q` |
| Select All / Deselect All | `Ctrl+A` / `Ctrl+Shift+A` |
| Group / Ungroup | `Ctrl+G` / `Ctrl+Shift+G` |
| Delete | `Del` |
| Rotate CW / CCW / Flip / Re-drag Pins | `R` / `Shift+R` / `Alt+R` / `Ctrl+R` |
| Move component to other board side | `Alt+B` |
| Tools: Select / Route / Cut / Simplify | `S` / `W` / `C` / `Shift+S` |
| Add Component (focus Library) | `A` |
| Route options: Insulated / Side Top / Side Bottom | `I` / `T` / `B` |
| Wire bulk: Set Insulated / Set Bare | `Ctrl+Shift+I` / `Ctrl+Shift+B` |
| Wire bulk: Move to Top / Bottom / Flip | `Ctrl+T` / `Ctrl+B` / `Ctrl+F` |
| Camera / Mirror H / Mirror V | `X` / `H` / `V` |
| Zoom In / Out / Fit Board / Fit Area / Fit Selection | `Ctrl++` / `Ctrl+-` / `Ctrl+0` / `Ctrl+Shift+0` / `Ctrl+Shift+F` |
| Layer toggles 1..10 | `1`..`9`, `0` |
| Filter Components / Wires / Groups | `Alt+1` / `Alt+2` / `Alt+3` |
| Cycle Relation / Cycle Back / Clear Highlight | `` ` `` / ``Shift+` `` / `Ctrl+Shift+H` |
| Run DRC / Autoroute | `F5` / `F6` |
| Preferences / Full Screen | `Ctrl+,` / `F11` |

User overrides land in `~/.config/protoboard/keybindings.yaml`. Common
overrides observed in the wild: `redrag_pins=E`, `unweave_wires=U`,
`highlight_toggle=Space`.

The Route-tool wire default (insulated vs. bare, top vs. bottom side) is
**bare / bottom** out of the box and is **persisted**: changing it via the
`I` / `T` / `B` keys or the toolbar toggles writes the new default to the
`wire:` section of `~/.config/protoboard/app_settings.yaml`, so it survives a
restart. `MainWindow._wire_insulated` / `_wire_side` seed from those settings
on launch.

## SelectTool

`MainWindow` keeps a single persistent `SelectTool` instance so that
`_group_stack` and selection survive tool switches. Selection semantics are
fully described in
[highlight-state-machine.txt](highlight-state-machine.txt) (rev 3, which
treats components, wires and **groups** as first-class hit kinds and uses
an orthogonal `Alt(basis) × Ctrl/Ctrl+Shift(set-op)` modifier matrix).

### Multi-selection

- `_selected_ids: set[UUID]`, `_selected_wire_ids: set[UUID]`,
  `_selected_group_ids: set[UUID]`.
- Rubber-band **LTR** (left-to-right): the bbox must be fully inside the
  rectangle. For groups, all members must be inside.
- Rubber-band **RTL** (right-to-left): the bbox must intersect; wires use a
  Cohen–Sutherland clip test. For groups, any member intersecting counts.
- `Ctrl+click` adds, `Ctrl+Shift+click` removes, plain click sets.
- Multi-drag goes through `MoveDragSetCommand` (atomic; all moved
  components share one delta).
- Multi-delete is a `MacroCommand` of `RemoveComponentCommand` +
  `RemoveWireCommand`.

### Wire editing

- The wire-edit mode is entered with `[E]` after selecting a single wire
  (the legacy `Ctrl+click` entry point is gone).
- The first LMB defines the start of an editable fragment; the second LMB
  defines the end. The fragment is highlighted in cyan
  (`_WIRE_EDIT_PEN #00CCFF`).
- Hit testing is scoped to the selected wire's net so that cross-wire
  edits on the same net keep working without false hits on foreign wires.
- A miss is a no-op (no fall-through to the selection dispatcher).
- Right-click on a wire opens the wire menu: Flip fragment, Move to layer,
  Set bare / insulated, Convert endpoint to / from wire-pin.

### Group navigation

- `_group_stack: list[UUID]` starts at `[ROOT_GROUP_ID]`.
- Double-click a component → push that component's child group.
- Esc → pop (never pops `ROOT_GROUP_ID`).
- Plain LMB on a group outline → `Hit("group", gid)` SET (immediate; the
  legacy 500 ms long-press select phase is gone).
- A 1 s hold on a group outline still triggers enter-group.
- `_is_accessible(comp_id)`: at the root level always `True`; inside a
  child group only its direct members and direct proxy child-groups are
  accessible.

### Context menus

- On a pin: list of nets plus *Disconnect pin from net*
  (`DisconnectPinCommand`).
- On a component: *Move to {other} side* — flips the component to the opposite
  board side (`flip_component_side`, see geometry.md §5), plus lock/unlock and
  (inside a group) *Export / Unexport pin N* (`SetExportedPinsCommand`).
- On a wire: Flip fragment, Move to layer, Set bare / insulated, Convert
  endpoint to / from wire-pin (when the endpoint qualifies), Simplify,
  Unweave, Merge.

## Selection sync (session hub)

Selection is synchronised between the board view and the side panels through
the `EditorSession`, which is the single hub for **both** directions.
`MainWindow` does not fan selection out to panels nor relay panel clicks to the
board — each panel owns its own wiring.

- **Board → panel.** Every selection-changing click funnels through
  `SelectTool._apply_selection` (and the release / rubber-band / clear paths),
  each of which calls `_notify_selection()` →
  `session.selection_changed(comp_ids, wire_ids, net_id)`. Panels subscribe to
  that signal themselves: `TreePanelMixin.refresh()` connects
  `_on_session_selection`; `NetsEditorPanel` and `PropertiesPanel` connect in
  their own `refresh` / `set_session`. A panel's `sync_selection` highlights the
  matching rows (component rows, the net row, the Component-Pin rows, the
  Inspector row + its cost column).
- **Panel → board.** A row click calls `session.request_select_*`
  (`request_select_component` / `request_select_net` / `request_select_pin`),
  which emits `session.selection_requested`. `BoardView._on_selection_requested`
  is the single subscriber; it drives `SelectTool` via the existing
  `_set_selection_from_ids` (expanding a net through `SessionNetSource`), then
  `_notify_selection()` loops the result back out so every panel re-syncs.
- **No loops.** `selection_requested` and `selection_changed` are distinct
  signals, and each panel's row-click slot early-returns while `_syncing` is set
  during a programmatic `sync_selection`, so the click → sync → click cycle is
  broken.
- **Session swap.** On open-project, panels re-bind their subscription off the
  old (dead) session: tree panels via `refresh(new_session)`, `PropertiesPanel`
  via `set_session`, and `BoardView` re-connects `selection_requested` in
  `set_session`.

## Drag pipeline

See [drag-pipeline-architecture.md](drag-pipeline-architecture.md). The
short version: every drag (single component, multi-selection, single group,
or mix) is captured as a `DragSet` at press time; live conflict detection
runs through `DragDetector` and an injected `PadStrategy`; commit goes
through `MoveDragSetCommand`. Conflict resolution is preference-driven —
see Preferences ▸ Drag for the `auto-remove / ask-remove / auto-reroute /
ask-reroute / block` knobs.

## Render

The full draw chain — draw order, the Z-stack (0.0–10.0), the `LayerState`
model (camera-side front/back, alpha, per-layer visibility), the board pixmap
"sandwich", and how a component's body shape is chosen — is documented in
[rendering.md](rendering.md). In brief:

`render_groups()` in `component_renderer.py`:

- Draws outlines only for child groups of the active group (full opacity)
  and other groups (dimmed).
- The root group and the currently active group have no outline.
- Exported pads of child groups are drawn as green diamonds at the overlay Z.
- `BoardView._active_group_id` is passed to every renderer on each
  refresh.

The board view paints in Z-banded layers (back labels/components/wires →
board → pin-pads → front wires/components → overlay → highlights → selection).
Ten layers are toggled by the View → Layers panel with keys `1`–`0` (`9` =
pin-pads, `0` = pin-modifiers). Full Z anchors and toggle map: see
[rendering.md](rendering.md) §1.

## Hover-reveal

When a label-carrying layer is hidden, hovering a component briefly reveals
it without permanently toggling the layer back on. Driven by `HoverReveal`
(`gui/hover_reveal.py`), which subscribes to `BoardView.hover_published` and
arms a ~1 s dwell timer (`TimerWrap(1000, restart_on_arm=True)`) on the
hovered component:

- **`labels` hidden → dwell tooltip.** After the dwell, a small
  cursor-anchored tooltip (`HoverTooltip`, `gui/hover_tooltip.py`) shows
  `refdes value` (just `refdes` if the value is unset) at the pointer.
- **`meta_pin_labels` hidden + Alt held → per-component MetaPin reveal.** After
  the dwell, that one component's primary MetaPin labels render as if the layer
  were on for it alone — `LayerState.meta_pin_labels_reveal_cid` is set to the hovered
  component id (persisted on `BoardView._meta_pin_labels_reveal_cid`, threaded onto
  the fresh `LayerState` each `layer_state()` call) and
  `alpha_for_meta_pin_labels(side, cid=)` special-cases that one id back to full
  alpha.

Either reveal clears on a hovered-component change, an Alt press/release, a
move to bare canvas, or the mouse leaving the view — so nothing lingers past
the gesture that triggered it. See [rendering.md](rendering.md) §1.2 for the
Layers panel and the `meta_pin_labels` / `labels` toggle keys.

## Zoom and pan

- Zoom-to-cursor: `ScrollBarAlwaysOn` + 50 % `sceneRect` margin + scrollbar
  correction (no `translate()`).
- Zoom factors: in = ×1.1, out = ×(1/1.08) — asymmetric on purpose;
  changing them affects the feel of the tool.
- Pan: middle mouse button and `Ctrl+drag`. `NoAnchor` prevents the
  position reset that Qt otherwise applies when `setSceneRect` runs.
