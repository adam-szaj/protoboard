# MVC verb layer — the View/Controller interaction seam

Status: **in progress** (2026-07-10). Step-by-step refactor on top of the
shipped ports-only work ([qt-ports.md](qt-ports.md)). `BoardView` stays a
`QGraphicsView` (full MVC was rejected, qt-ports §4); this document is about the
*interaction* (input) seam, not the widget.

Related: [gui.md](gui.md), [keymap-per-tool-layer.md](keymap-per-tool-layer.md),
`docs/todo.md` (deferred items).

## 1. The boundary rule

The View/Controller boundary is defined **operationally by a test script**: a
headless test drives the app as a sequence of semantic *verbs* whose arguments
are in model coordinates.

```
default_action(Point, Mods)      # primary click (LMB)
secondary_action(Point, Mods)    # context click (RMB)
empty_action(Point, Mods)        # hover / move (throttled)  → HoverEvent
intent(CONFIRM | CANCEL | UNDO)
```

**Rule:** anything the script must *fake or bypass* belongs to the **View**.

- **View owns:** Qt events, `mapToScene` / scrollbars / `centerOn` / `QTimer` /
  **throttling**, and the **`KeyMap`** (physical key/chord → `action-id`).
  Proof the `KeyMap` is View: it *vanishes* from the script — the script
  supplies verbs directly, so no key-to-action translation is exercised. (Today
  the `KeyMap` physically lives on `ToolHost`; that is an implementation
  accident, not an architectural claim.)
- **Controller owns:** interaction state (SelectTool armed / float / route),
  hit-testing, and speaks **only** the verb/intent vocabulary. It replies to a
  hover with an enriched `HoverEvent{point, objects:[uuid…]}`.

Scene units are board/raster units (`geometry.py`, `view_camera.py` — `GRID_PX`
is only a view transform), so a verb's `Point` is already in model units: no
scene→mm conversion crosses the seam.

## 2. Controller state is first-class feedback

`ToolController.state` (e.g. `select.idle`, `move.float.rotate`,
`move.float.move`) is an **assertable Controller fact**, not merely an input
concern. A test asserts it directly, **including the negative case**: arming
rotate with no selection leaves the state `select.idle`. The View happens to
consume state changes to pick a `KeyMap` layer, but the *source* is the domain
state of the interaction, not the View's need. (Consumers: (a) tests / CLI /
panels read it as feedback; (b) the View swaps the `KeyMap` layer.)

## 3. Where the seam sits

`ToolHost` stays the View-side translator. Its Qt handlers run in two stages:

- **Stage A (View):** Qt event → mouse chord + `PointerInput`. The
  `KeyMap`-driven chord dispatch (pan, RMB menu, wheel-zoom) runs first and
  keeps winning — this is `KeyMap` territory.
- **Stage B:** the un-chorded residue becomes a **verb** and is handed to the
  active tool through the [`ToolController`](../src/protoboard/gui/ports/tool_controller.py)
  protocol.

The verbs sit **above** — and for now *delegate to* — the legacy
`on_press` / `on_move` / `on_right_press` hooks, via default adapters on
`_Tool` (`board_view_tool.py`). Introducing the vocabulary therefore changes no
behaviour on its own (parity before swap, CLAUDE.md §8).

`empty_action` replies with a [`HoverEvent`](../src/protoboard/gui/ports/hover.py);
it does **not** reuse the heavy `InteractionContext` (which builds the full
menu-action set for a right-click). The hover reply is published onward via a
dedicated `notify_hover` channel, never through `notify_context`.

## 4. Migration order (each sub-commit green + reversible)

1. **Vocabulary + adapters + parity twins** (no live re-route) — *this commit*.
   `ToolController`, `HoverEvent`, default verb methods on `_Tool`,
   `SelectTool.secondary_action` / `SelectTool.state`, parity twins in
   `tests/gui/test_headless_tool_flows.py`. Runtime unchanged.
2. `empty_action` runs the hit-test eagerly and returns
   `HoverEvent{point, objects}`.
3. View-side `HoverThrottle` + live move re-route + `notify_hover`.
4. LMB residue → `default_action`.
5. RMB chord → `secondary_action` (View reconstructs `view_pos` from the point).
6. `controller_state_changed` emit + `KeyMap`-layer swap on state (replacing the
   `wants_key` armed-arrow claim); parity tests for armed keys precede the swap.

Deferred (tracked in `docs/todo.md` when reached): migrating each tool's
internals into verbs; inlining the LMB logic out of `on_press`; the
`select.pick` / `select.wire_edit` states; the actual hover-highlight visual.
