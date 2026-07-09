# Per-tool KeyMap layer — lifecycle design

Status: **design** (2026-07-09, not yet implemented).
Related: `gui/keymap.py`, `gui/tool_host.py`, `gui/board_view_tool.py`,
[armed-rotate-and-subtool-delegation.md](armed-rotate-and-subtool-delegation.md).

## 1. Problem

Today the map chain is `KeyMap (single leaf, owned by ToolHost._keymap) →
GlobalKeyMap (root)`. A tool does **not** get its own map: on `activate()`
`ToolHost._register_tool_keymap` merges the tool's `key_actions()` into the
shared leaf. Consequences:

- Actions of the *previous* tool stay registered forever (stale merge).
- A tool cannot *shadow* a view binding for the duration of its activation
  (e.g. a rotate tool claiming the wheel that normally zooms) except through
  the imperative `wants_key` / `wants_wheel` veto callbacks.
- The `[?]` overlay and Preferences cannot enumerate "the active tool's map"
  as a first-class object.

## 2. Target model

**The tool is a factory of its own KeyMap layer:**

```python
class BoardViewTool:
    def make_key_map(self, parent: AbstractKeyMap) -> KeyMap:
        """Build this tool's key-map layer chained onto *parent*."""
```

Chain while a tool is active:

```
tool layer (ephemeral, from tool.make_key_map)      ← head
  → base map (permanent, ToolHost; user overrides, view/app actions)
    → GlobalKeyMap (built-in global.action.* defaults)
```

Resolution is **shadowing, not merging**: the first map in the chain that
declares a binding for a trigger wins. A rotate tool that binds the wheel
shadows the view's wheel-zoom for as long as it is active; everything the
layer does not declare falls through to the base map (BoardView) untouched.

### Lifecycle

```python
# ToolHost
def set_tool(self, tool):
    if self._active_tool is not None:
        self._active_tool.deactivate()
    self._tool_keymap = None                      # pop old layer
    self._active_tool = tool
    if tool is not None:
        tool.activate(view.session, view)
        self._tool_keymap = tool.make_key_map(self._base_keymap)   # push

@property
def keymap(self) -> AbstractKeyMap:               # the current head
    return self._tool_keymap or self._base_keymap
```

- The layer is **ephemeral**: created on activate, dropped on
  deactivate/detach. No unregistration bookkeeping; stale-merge bug gone.
- `_base_keymap` is the current `KeyMap(GlobalKeyMap.with_globals())`,
  renamed; it stays permanent and keeps load/save of user overrides.
- Every existing consumer (`_Tool._key_matches` via `view.keymap`, the `[?]`
  overlay, RMB menu rows, `trigger_tool_action`) already resolves through the
  `keymap` property, so they see the layered chain automatically.
- Default `_Tool.make_key_map` builds `KeyMap(parent)` and registers
  `key_actions()` into it — no tool needs changes on day one. Tools override
  the factory only for exotic cases (sub-tool delegation can *nest* another
  layer later).

### User overrides: one shared store

User rebinds must keep working regardless of which layer declares the action.
If the tool layer were a plain fresh `KeyMap`, its literal declaration (e.g.
`select-tool.action.reroute: "W"`) would shadow a user override ("Q") living
on the base leaf, because `resolve()` consults only *its own* `_overrides`
before its own declaration.

Therefore: **the overrides dict is owned by the base map and injected into
every layer by reference**:

```python
def make_key_map(self, parent):
    km = KeyMap(parent)
    km._overrides = parent.overrides_store   # same dict object, shared
    km.register_tool_actions(self.key_actions())
    return km
```

- A rebind performed through any head lands in the one store; persistence
  (`load_overrides` / `save_overrides`, `KEYBINDINGS_V2_FILE`) stays a
  base-map responsibility.
- `save_overrides`' "skip if equal to declared default" check walks
  `resolve_action` up the chain — call it on the current head so tool-layer
  declarations are visible; saving from the bare base merely loses that
  minimisation, never a binding.

### Multiple triggers per action (general, not final-only)

`KeyAction.shortcuts` is deliberately typed `list[str]` (the legacy `"A|B"`
string parses to the same) so that *any* action may carry alternative
triggers — `["Up", "W"]` today, `["R", "WheelUp"]` after stage 2. This is a
general capability of the model, independent of `final`. Shadowing operates
on the whole alternatives list: a non-final declaration in a layer replaces
the parent's entire list. The `final` rule below governs only *cross-layer
extension* of a protected action.

### `final` semantics (refined)

`final=True` on a global action means the *trigger* is protected, not the
action:

- **A layer may not claim a final action's trigger for a different action.**
  Enforced at layer construction (`register_tool_actions`) — reinstate the
  currently commented-out `FinalShortcutError` check there. ToolHost logs and
  skips the offending entry (best-effort, as today).
- **A layer may add additional triggers to the same final action.** Example:
  a tool declares that `global.action.cancel` is *also* fired by a
  right-click. Resolution for a final action id is therefore a **union**
  (base triggers ∪ layer additions) instead of a shadow — the base trigger
  (Esc) can never be removed, only extended.
- Rebinding a final action via `set_override` stays rejected.

Non-final actions keep pure shadowing.

### Dispatch — "unhandled returns to BoardView"

Today dispatch is per-action probing (`matches(key, mods, action_id)` in
`wants_key` chains and `on_key` ladders). With a real head map we add the
reverse lookup:

```python
def lookup(self, trigger) -> str | None:
    """First action id in the chain bound to *trigger* (leaf wins)."""
```

`ToolHost.handle_key_press` becomes: event → trigger → `head.lookup()` →
- tool-category action → `tool.dispatch(action_id, event)` — a dispatch
  *table* (`dict[action_id, handler]`) on the tool replaces the
  `_key_matches` if-ladders;
- no hit, or the tool declines (state gate, see below) → fall through to the
  existing view handlers / MainWindow forwarding — this is the "unhandled
  bubbles back to BoardView" rule.

`wants_key` (the ShortcutOverride veto) reduces to: *does the head resolve
this trigger to a tool-layer action, and does the tool's current state accept
that action* (`tool.wants(action_id) -> bool`, e.g. armed-mode gate). No raw
key matching in tools.

## 3. Mouse triggers (stage 2)

The motivating examples (wheel steps rotation; RMB fires cancel) need
triggers `QKeySequence` cannot express. Introduce a `Trigger` union:

- key chord — today's `QKeySequence` strings;
- mouse chord — `"WheelUp"`, `"WheelDown"`, `"RMB"`, `"MMB"`, `"Ctrl+LMB"`,
  `"DoubleLMB"` (modifiers reuse Qt names; no move events — a stream is not
  bindable).

The `"A|B"` alternatives syntax and the overrides file format stay; parsing
learns the mouse atoms. The base map then declares today's hard-coded view
gestures as real actions (`view.action.zoom_in: "WheelUp"`,
`view.action.pan: "MMB|Ctrl+LMB"`, `view.action.canvas_menu: "RMB"`), and:

- `BoardView.wheelEvent` / `ToolHost.handle_mouse_press` route through
  `head.lookup(trigger)` like keys do;
- `wants_wheel` / `on_wheel` and the `on_right_press` getattr-probe retire —
  a rotate tool simply declares `rotate-tool.action.rotate_cw: "WheelUp"`
  in its layer, shadowing zoom while active;
- "RMB fires cancel in tool X" is a layer union-addition to the final
  `global.action.cancel` (see above).

Stage 1 (lifecycle) ships with keys only; `wants_wheel`/`wants_key` keep
working unchanged until stage 2 lands.

## 4. Migration plan

1. **Stage 1 — lifecycle.** `make_key_map` factory + push/pop in
   `ToolHost.set_tool` / `detach_active_tool`; shared overrides store;
   `keymap` property returns the head. Parity: all existing key behaviour
   tests must pass unchanged; add tests for (a) layer popped on tool switch
   (previous tool's bindings no longer resolve), (b) user override beats a
   layer declaration, (c) final trigger collision rejected at construction.
2. **Stage 1b — dispatch table.** `lookup()` on the chain;
   `tool.dispatch`/`tool.wants(action_id)`; rewrite `wants_key`/`on_key`
   ladders in Select/Place tools. Parity tests carry over (behaviour, not
   internals).
3. **Stage 2 — mouse triggers.** `Trigger` parsing, view gesture actions on
   the base map, wheel/RMB routed through the map, retire
   `wants_wheel`/`on_right_press` probing. Union-additions for final actions.
4. **Stage 3 — UI.** Preferences sub-tabs per category (one layer = one
   sub-tab; final actions shown read-only), `[?]` overlay reads the head
   chain directly.

## 5. Open questions

1. Union-for-final is implicit (declaring a final id in a layer always
   *adds*). Alternative: explicit `"+RMB"` additive syntax. Implicit chosen
   for now — declaring a final id can mean nothing else, since replacing is
   forbidden.
2. Does `trigger_tool_action` (menu → synthesised QKeyEvent) survive stage 2,
   or do menu items call `tool.dispatch(action_id)` directly once the
   dispatch table exists? (Direct dispatch looks cleaner; synthesis exists
   only because dispatch is currently key-shaped.)
3. Sub-tool delegation (armed-rotate doc) as a *nested* third layer — out of
   scope here, but the chain design deliberately permits it.
