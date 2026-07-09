# Qt Ports — analysis: making BoardView & friends testable headless

Status: **implementation in progress** (2026-07-09). The analysis below was
accepted and the migration sketch in §6 is being executed phase by phase —
progress is tracked in the §6 checklist. Original analysis kept as written;
per-phase deltas are noted inline where reality diverged.

Related: [gui.md](gui.md), [benchmarks.md](benchmarks.md) (perf invariants),
[keymap-per-tool-layer.md](keymap-per-tool-layer.md) (chord model the input
DTOs align with), `docs/todo.md` (deferred ports).

## 1. Findings — where the Qt coupling actually is

### 1.1 The declared seam is nearly Qt-free; the inheritance is not

`AbstractBoardView` (`gui/board_view_tool.py:60`) declares ~25 members that are
already Qt-free in signature (session, theme, keymap, layer_state, refresh,
`board_pad_positions() -> list[Point]`, group mappings that deliberately
return `Mapping[UUID, object]`, …). Only four members are Qt-typed:

| Member | Qt type | Trivially replaceable by |
|---|---|---|
| `snap(scene_pt: QPointF, …) -> Point` | `QPointF` | `Point` |
| `is_locked_pin(scene_pt: QPointF, …)` | `QPointF` | `Point` |
| `center_on_scene_point(QPointF)` | `QPointF` | `Point` |
| `set_selection_outline_override(QColor \| None)` | `QColor` | `str \| None` (hex `#AARRGGBB`, matching `Theme`) |

But the ABC **inherits `QGraphicsView`** (via the `QtABCMeta` metaclass merge,
`board_view_tool.py:44-58`), so it cannot be implemented headless, and its own
docstring sanctions "plus what `QGraphicsView` itself provides". Services and
tools use that escape hatch freely — the real coupling is the *inherited* API:

- **Coordinate mapping**: `mapToScene` (view_camera, tool_host, paint_pipeline,
  drag_corner_text), `mapToGlobal`/`mapFromScene` (menus).
- **Transform**: `transform().m11()/.m22()`, `setTransform(QTransform)`,
  `scale()` (view_camera, tool_host, board_view).
- **Viewport**: `viewport().rect()/size()/update()/setMouseTracking()`,
  `backgroundBrush()` (view_camera, tool_host, paint_pipeline, highlight).
- **Scene**: `scene().addItem/removeItem/items` — composer, highlight renderer,
  and ~35 transient-item sites in tools (select ~20, route ~8, place ~6,
  simplify 2).
- **Scrollbars**: `horizontal/verticalScrollBar().value()/setValue()`
  (pan, arrow-scroll, zoom pivot).
- **Camera**: `centerOn`, `fitInView`; **cursor**: `setCursor`.
- **`window()` cast to `QMainWindow`** (~13 sites in tools + tool_host):
  `statusBar()`, `._keybindings`, `keyPressEvent(event)` forwarding,
  `screen()`. This couples tools to the top-level window, not just the canvas.
- **Widgets built against the view**: `QMenu` (canvas + context menus,
  including `menu: object` + `assert isinstance(QMenu)` in
  `tool_host.add_tool_action_menu` — an admission the seam wants to exist) and
  the `[?]` `QLabel` overlay parented to the viewport.

### 1.2 Raw Qt events cross into tools — but the surface is closed

`BoardView`'s Qt overrides pass `QMouseEvent`/`QKeyEvent`/`QWheelEvent`
unmodified through `ToolHost` into `BoardViewTool.on_*`. Across the whole
seam only these fields are ever read: `.position()`, `.pos()`, `.button()`,
`.modifiers()`, `.angleDelta().y()`, `.key()`, `.isAutoRepeat()`, plus the
`.accept()/.ignore()/.isAccepted()` consumption protocol. Never read:
`.buttons()`, `.pixelDelta()`, `.globalPosition()`, `.text()`. An event-DTO
abstraction is therefore small and closed.

### 1.3 The controller layer already is the target pattern

- `gui/selection/mouse_input.py` defines a **Qt-free vocabulary**: `Point`,
  `Mods` (ctrl/shift/alt), `Intent` (CONFIRM/CANCEL/UNDO) and the
  `MouseInputController` protocol — "the tool extracts these from the
  QMouseEvent once".
- `drag_controller`, `point_pick_controller`, `rubber_band_controller`,
  `service.py` (SelectionService/PickSession), `wire_edit.py`, `wire_ops.py`
  are **fully Qt-free** with injected callables: `SnapFn`, `PreviewFn`,
  `ExecuteFn`, `BboxFn`, `DrawFn`, conflict resolvers.
- Hit-testing (`gui/selection/hit_test.py`) is fully Qt-free (`HitTester` +
  `HitContext`/`LayoutLike` Protocols, faked in unit tests); group hit-testing
  was deliberately migrated *off* scene items.
- Remaining leaks: `armed.py` / `float_transform.py` take raw
  `QKeyEvent`/`QWheelEvent` (hard-coded `Qt.Key_Return/Escape` compares,
  `angleDelta` unpacking, `Matcher = Callable[[QKeyEvent, str], bool]`);
  `selectable.draw_highlight(ctx) -> list[QGraphicsItem]` (TYPE_CHECKING-only
  import; `HighlightCtx` already returns opaque `object` pens/positions).

### 1.4 Scene identity and paint are already half-decoupled

Scene items carry domain identity only as `data(0)` = tag/UUID-string and
`data(1)` = kind tag — never live domain objects. The board layer is already
painted **offscreen** into the paint pipeline's pixmap caches. Perf invariants
any refactor must keep (see benchmarks.md): the Qt-free spatial index, item
group reuse keyed by fingerprint, Z-banding + back-cache, the 16 ms refresh
debounce.

### 1.5 Miscellany

- `pad_clicked` signal is **dead** — declared, never emitted, never connected.
- `cursor_moved`/`escape_tool` are emitted *by consumers of the seam*
  (tool_host, select_tool) and connected only in `main_window.py` — the
  protocol needs emit-side methods, not Qt signals.
- `protoboard.geometry` already provides the whole port type vocabulary:
  `Point`, `Vector`, `Rect`, `Size`, `Transform2D` (documented QTransform
  bridge via `coeffs`). No new geometry types are needed.
- `domain/` has zero Qt; `services/` only the export rasterisers. The
  architecture rule holds — this analysis is about `gui/` internals only.
- The practical testability bar is "**no realized QWidget, no event loop, no
  qtbot**", not "zero Qt imports": `KeyMap` imports QtCore/QtGui value types
  and tests headless today.

## 2. The port catalogue

Proposed home: `src/protoboard/gui/ports/` — pure Python + `protoboard.geometry`
imports only, so the same mechanical rule that protects `domain/` protects the
ports. Six pieces:

### 2.1 `ViewportPort`

One port — mapping, transform, scroll and viewport rect are one physical
object and mathematically interdependent; splitting camera/viewport yields two
ports that only ever ship together.

```python
class CursorKind(Enum): ARROW; CLOSED_HAND; ...        # only kinds actually used

@dataclass(frozen=True, slots=True)
class ScreenMetrics:
    size_px: Size
    dpi: float

class ViewportPort(Protocol):
    def map_view_to_scene(self, view_pt: Point) -> Point: ...
    def map_scene_to_view(self, scene_pt: Point) -> Point: ...  # verify need; may
                                                                # drop with §2.4
    def view_scale(self) -> float: ...                  # |m11|
    def view_transform(self) -> Transform2D: ...
    def set_view_transform(self, t: Transform2D) -> None: ...
    def viewport_rect(self) -> Rect: ...
    def visible_scene_rect(self) -> Rect: ...           # transform+scroll+size —
                                                        # same pure function in Qt
                                                        # adapter and in the fake
    def scroll_position(self) -> Vector: ...
    def set_scroll_position(self, v: Vector) -> None: ...
    def pan_by(self, view_delta: Vector) -> None: ...   # absorbs direct scrollbar
                                                        # juggling during drags
    def center_on(self, scene_pt: Point) -> None: ...
    def fit_in_view(self, scene_rect: Rect) -> None: ...
    def request_repaint(self) -> None: ...
    def set_cursor(self, cursor: CursorKind) -> None: ...
    def screen_metrics(self) -> ScreenMetrics | None: ...
```

**The split criterion** (settled on user feedback, and the rule that governs
every port in this document): *policy and decisions stay on the testable
side; only mechanism and data go behind the port.* Semantic camera commands
(`zoom_by`, `zoom_in/out`, `fit_board/area/selection`, mirror toggles,
`move_view_*` steps) are **not** port surface — behind them sits exactly the
math this refactor wants to test headless (zoom clamping to screen limits,
zoom-to-cursor pivot, wheel-burst accumulation, mirror/fit composition in
`view_camera.py`). Putting `zoom_in` on the port would push that logic behind
it — into the Qt adapter (untestable) or duplicated in the fake (drift). The
two layers both already exist; the port merely closes off the bottom one:

```
BoardView / tools / MainWindow
    │  semantics: zoom_by, fit_*, toggle_mirror, move_view_*  ← ViewCamera
    ▼            (policy; headless side — tested against the fake)
ViewCamera
    │  mechanism: get/set transform, get/set scroll, pan_by,
    ▼             viewport size, center_on, visible_scene_rect
ViewportPort  (dumb, easy to fake)
    │
QtViewport(QGraphicsView) — the only code knowing QScrollBar/QTransform
```

Contrast with §2.4: there the *choice* is a user interaction, so it goes
behind the port; here the *math* is application policy, so it stays in front.

Absorbs every inherited-API call in `view_camera.py`, `tool_host.py`,
`drag_corner_text.py` and `board_view.py` listed in §1.1 (mapping, transform,
scrollbars — via `pan_by`/scroll primitives, centerOn/fitInView, cursor,
`window().screen()`). Tools and BoardView never see scrollbars; keymap-bound
`move_view_up/down/left/right` become trivial ViewCamera wrappers over
`pan_by` with a policy-owned step. The adapter `QtViewport(view)` is the
**only** QPointF/QTransform conversion site; the test fake implements the
mapping with exact `Transform2D` affine math, which makes ViewCamera's
zoom-clamp / fit / mirror arithmetic unit-testable with real numbers instead
of mocks — `visible_scene_rect()` makes the fake a complete deterministic
camera model ("after zoom-at-cursor the visible rect is R and the point under
the cursor did not move"), and joins the Qt-vs-fake property test. The four
Qt-typed ABC members (§1.1) switch to `Point` / hex `str` in the same
breath — several tools already round-trip `Point(scene_pt.x(), scene_pt.y())`
today, so this *removes* conversions.

### 2.2 Input DTOs — `PointerInput`, `WheelInput`, `KeyInput`

Aligned with the two vocabularies that already exist: `Mods`/`Intent`
(`mouse_input.py`) for controller semantics, and the int-based KeyMap chord
model (`mouse_button_chord(button, modifiers)`, `wheel_chord(delta_y, mods)`,
`matches(key, mods)`).

```python
@dataclass(frozen=True, slots=True)
class PointerInput:
    view_pos: Point          # was event.pos()
    scene_pos: Point         # pre-mapped by the view
    button: int              # Qt.MouseButton value — chords already take ints
    native_mods: int         # exact mask for KeyMap chord resolution
    mods: Mods               # reuse mouse_input.Mods

@dataclass(frozen=True, slots=True)
class WheelInput:
    delta_y: int             # angleDelta().y(); notch accumulation stays put
    view_pos: Point
    native_mods: int
    mods: Mods

@dataclass(frozen=True, slots=True)
class KeyInput:
    key: int
    native_mods: int
    is_auto_repeat: bool
    mods: Mods
```

This is the complete field set read across the seam today (§1.2). The
`.accept()` protocol becomes **bool returns** (`on_key` → consumed?), and the
concrete `BoardView` forwards unconsumed keys — which deletes the
`window().keyPressEvent(event)` call in ToolHost. The same phase de-leaks the
armed sub-tools: `ArmedSubTool.on_key(KeyInput)` / `on_wheel(WheelInput)`;
`FloatTransform`'s `Matcher` takes `KeyInput`; the hard-coded
`Qt.Key_Return/Escape` compares route through `Intent.CONFIRM/CANCEL` (the
mouse_input doctrine already mandates "keys travel as intents, not keycodes").
`_WHEEL_NOTCH` accumulation is arithmetic on `delta_y: int` and stays.

### 2.3 View notification channel — status as *events*, not display calls

(Direction revised on user feedback: a `StatusPort.show_status(text)` push
was considered and rejected — displaying is the consumer's job, not the
seam's.) The view surface *publishes* notifications; the consumer
(MainWindow, or a test double that merely records) decides what to do:

```python
def notify_status(self, event: StatusEvent) -> None: ...
def notify_cursor_moved(self, x: float, y: float) -> None: ...
def notify_context(self, ctx: InteractionContext) -> None: ...   # see §2.4
def escape_to_select(self) -> None: ...
```

- **Payload is a dict/Mapping** — extensible without touching the seam's
  signature. Minimal convention: a `"kind"` discriminator
  (`"route.progress"`, `"drag.conflict"`, `"select.count"`, …) plus
  structural fields (`"net_id"`, `"count"`, `"length"`, `"timeout_ms"`…).
  During migration a tool may include `"text"` with today's formatted string
  for 1:1 parity; formatting can later move to the consumer.
- **Consumption:** concrete `BoardView` re-emits as a Qt signal
  (`status_changed = Signal(dict)`); MainWindow subscribes and calls its own
  `statusBar().showMessage(...)` — the last reason for anything behind the
  seam to touch `window()` disappears. The other `window()` uses get
  better-shaped homes anyway: `_keybindings` for the `[?]` overlay → a
  provider injected into ToolHost; `screen()` → `ViewportPort`; key
  forwarding → bool returns (§2.2).
- **Test win over the port variant:** tests assert structural fields
  (`payload["kind"] == "drag.conflict"`, `payload["count"] == 3`) instead of
  parsing formatted strings — robust against message wording changes.
- **Payload shape (D7 — settled):** a thin envelope
  `StatusEvent(kind: str, data: Mapping[str, object])` — decided by the
  user's own concept-=-class rule (§2.6): the payload conventions (`kind`,
  `text`, `timeout_ms`) are "several variables of one concept", so they get
  a class with a typed discriminator, while `data` stays a flexible Mapping.

The dead `pad_clicked` is deleted. `session.py` keeps its QObject signals —
out of scope here.

### 2.4 `InteractionContext` — RMB as context publication (no port at all)

(Twice revised on user feedback. Round one replaced a `MenuPort.show_menu`
with an `ActionChooserPort.choose_action` — "ask for a choice among the
actions executable in this context" instead of the graphical "show a menu".
Round two **superseded the port entirely**: split the blocking call into a
*query* plus a *notification*, after which no chooser crosses the seam.)

```python
@dataclass(frozen=True, slots=True)
class ActionOption:
    action_id: str                 # executes via existing trigger_tool_action
    label: str                     # presentation hint
    enabled: bool = True
    shortcut_hint: str = ""        # presentation; may be read from the KeyMap
    group: str = ""                # semantic grouping (consumer decides rendering)

@dataclass(frozen=True, slots=True)
class InteractionContext:          # ports/interaction_context.py — pure data
    scene_pos: Point
    view_pos: Point
    hit: tuple[str, UUID] | None   # (kind, id) of the clicked element (HitTester)
    selection: tuple[UUID, ...]    # selection snapshot
    actions: tuple[ActionOption, ...]  # actions executable NOW in this context
    kind: str                      # "canvas" | "selection" | "wire" | …
```

Two mechanisms instead of a port:

1. **Query:** `get_context_at(scene_pos: Point) -> InteractionContext` — a
   view-surface method; a pure function of state (selection + the existing
   Qt-free `HitTester` + action-list construction), **testable headless with
   no fake at all**. Equally usable by an interactive CLI as a plain query.
2. **RMB = context publication:** right-click →
   `notify_context(get_context_at(pt))` — the third notification beside
   `notify_status` / `notify_cursor_moved` (§2.3). The consumer (MainWindow)
   subscribes, renders the QMenu from `ctx.actions`, and on choice calls the
   **existing** `trigger_tool_action(action_id)`. A test simulates the choice
   with the same call; a CLI prints a numbered list.

Gains over the `choose_action` port: no blocking call through the seam
(`QMenu.exec` lives only in MainWindow, where it is at home); execution stays
decoupled through the existing action-id path (KeyMap shortcuts,
`displayable_action_ids`, `trigger_tool_action`); one abstraction less — the
port disappears, leaving data + two methods. Option-set construction is a
pure function either way ("locked component ⇒ Rotate disabled" testable
headless).

Parity nuances for implementation: (a) today's `exec` is modal — after the
split, state could in principle change between publication and choice; with a
modal menu in MainWindow behaviour stays effectively identical — note it in
the parity tests; (b) verify every current menu item (ToolHost canvas menu,
SelectTool context menu) is expressible as an action-id —
`add_tool_action_menu` already returns a QAction→action_id map, so this
should close naturally.

Follow-up to verify at implementation: if SelectTool's menu was the only
`map_scene_to_view` user, drop that member from `ViewportPort` — the port
should stay minimal.

### 2.5 `HelpOverlayPort`

```python
class HelpOverlayPort(Protocol):
    def show_help(self, html: str) -> None: ...
    def hide_help(self) -> None: ...
    @property
    def help_visible(self) -> bool: ...
```

Moves the `[?]` QLabel lifecycle out of ToolHost (`_render_shortcuts_html` is
already pure and stays). Low value alone, but it is the *last* widget API in
ToolHost after §2.1–2.4 — with it, ToolHost becomes fully headless-drivable.

### 2.6 Implementation guideline — one concept = one class (`_details.py`)

Settled on user feedback as a general rule for this refactor (and recorded
in CLAUDE.md §6): *a group of variables that move together is one concept
and gets one small class with readable methods — never scattered fields
mutated chorally across call sites.* Companion file convention:
`file.py → file_details.py` for such extracted concept classes. Concrete
applications here:

- **`CameraMath`** (`ports/viewport_details.py`): the
  transform + scroll + viewport-size triple is one concept, today smeared
  across `mapToScene`/scrollbar/`viewport().rect()` call sites. A pure class
  with `map_view_to_scene()`, `visible_scene_rect()`, `pan_by()` becomes
  **the entire implementation of the viewport fake** — the fake stops being
  a second ad-hoc copy of the math; it *is* the math, and the Qt-vs-fake
  property test pins the adapter to it.
- **`WheelNotchAccumulator`**: the `_wheel_accum` + `±_WHEEL_NOTCH` loop
  exists in `float_transform.py` and in ViewCamera's burst handling — one
  small class `add(delta_y) -> int` (whole notches), reused by both
  (natural moment: the wheel-DTO phase).
- **`TransformBurst`** (`view_camera_details.py`): the zoom/pan burst
  flags-and-timers (`in_transform_burst`, pan-settled bookkeeping) are one
  "a transform series is live" concept — `note_wheel()`, `note_pan()`,
  `active`, `settled()`.
- **`StatusEvent`** (§2.3) — the same rule settles D7.
- **`HeadlessBoardView` state**: recorded notifications, group registries and
  highlight state grouped into small recorder classes instead of loose dicts
  on the double.

### 2.7 Colour

`set_selection_outline_override(str | None)` — hex `#AARRGGBB`, matching how
`Theme` already stores colours; the QColor conversion happens in
`highlight/renderer.py` (Qt-side anyway).

## 3. Deliberately rejected / deferred

- **Generic `ScenePort` — rejected.** The heavy scene consumers (composer,
  highlight renderer, paint pipeline) stay Qt-side by design; a port for the
  tools' ~35 transient-item sites would grow a drawing vocabulary (pens,
  ellipses, Z values) — a bad re-implementation of QPainter at the wrong
  altitude, endangering the perf invariants. The repo already has the right
  pattern twice (`rubber_band_controller.DrawFn`, `drag_conflict_overlay`,
  `drag_corner_text`): **semantic per-tool preview objects**
  (`RoutePreview.show_live_segment(a, b)`, `.clear()`), Qt concretions
  injected at tool construction, fakes recording semantic calls. Extraction
  is optional polish, per tool, on demand.
- **`ExecutorPort` — deferred.** Workers (`gui/workers/`) already isolate
  compute behind immutable snapshots (`RouteRequest`, `SimplifyInputs`) that
  are testable today; only `operation_worker`'s QProgressDialog+QEventLoop
  harness is Qt-bound. Build the port only when a tool flow that *launches* a
  worker must be tested end-to-end. Tracked in todo.md.
- **`draw_highlight -> list[QGraphicsItem]`** — acceptable as-is
  (TYPE_CHECKING-only; adapters are Qt-side concretions). Tighten to an opaque
  handle only if a headless consumer ever appears. Tracked in todo.md.

## 4. Recommendation: ports-only, not full MVC

Full MVC (a view-model owning all canvas state, the view as a dumb
projection) would re-run the SelectTool v1→v2 failure mode at 10× scale: the
polish lives in the interplay of composer/pipeline/highlight and would have to
be re-implemented — exactly the "rewrite loses every undocumented refinement"
trap CLAUDE.md warns about. 3755 tests are green today against the real
widget; MVC invalidates their fixtures wholesale.

**Ports-only end state:** `AbstractBoardView` stops inheriting
`QGraphicsView` and becomes a `Protocol` composing the existing ~25 Qt-free
members + `viewport: ViewportPort` + `help_overlay: HelpOverlayPort` +
`get_context_at` + the notification channel (§2.3–2.4); the concrete
`BoardView(QGraphicsView)` satisfies it structurally. Wins:

- **Kills the QtABCMeta trap**: today Shiboken skips the
  `__abstractmethods__` check at runtime *and* pyright needs a TYPE_CHECKING
  fake metaclass; a Protocol has neither problem and pyright enforces
  conformance structurally on both the real view and the double.
- **The sanctioned surface finally equals the real surface** — mechanically,
  because the Protocol simply does not have `horizontalScrollBar()`.
- **`HeadlessBoardView` test double** (`tests/fakes/board_view.py`):
  dict-backed state, the affine-math fake viewport, recorded
  status/menu/help calls, optionally a real `QGraphicsScene` (no realized
  widget, no event loop — works offscreen exactly as the suite already
  does). Unlocks tool-logic tests without qtbot, ViewCamera math tests,
  ToolHost dispatch tests, menu-content tests — and a major suite-speed
  lever.

Honest risks: the accept→bool change is the one real regression surface
(ShortcutOverride veto, key fall-through to MainWindow) — parity tests must
precede it; two mapping implementations can drift — pinned by one small
property test comparing `QtViewport` vs the fake on random transforms.

## 5. Decision gate

None of this is decided. The choices that shape any implementation:

| # | Decision | Options | Impact | Proposed default |
|---|---|---|---|---|
| D1 | Headless bar for the double | real `QGraphicsScene`, no widget / zero QtWidgets | A keeps preview extraction optional; B makes it mandatory and earlier (~20 scene sites in select_tool alone) | real scene, no widget |
| D2 | Input-DTO landing order | per event type (3 commits) / per tool (5) | atomic ABC signature changes vs narrower diffs | per event type |
| D3 | `Mapping[UUID, object]` group handles | keep `object` / marker `ItemGroupHandle` | doc-only | keep `object` |
| D4 | SelectTool dialogs (drag-disconnect/conflict, `parent=window()`) | in scope as injected `Callable[..., Decision]` prompts (drag_commit_decision.py pattern) / out of scope | +1 sub-phase vs tracked debt | in scope |
| D5 | Doc location | this file + todo.md debt entries | — | as done |
| D6 | `KeyMap.matches` modifiers typing | widen to `int` / convert at call sites | runtime-identical | widen to `int` |
| D7 | `notify_status` payload shape | ~~bare dict / envelope~~ | **SETTLED**: `StatusEvent(kind, data)` envelope, by the concept-=-class rule (§2.6) | — |

## 6. Appendix — non-binding migration sketch

Each phase green + committable; existing qtbot tests run unchanged throughout
(nothing is swapped until the type-level Protocol change at the end).
Progress: ☑ 1, ☑ 2, ☑ 3 (a/b/c), ☑ 4, ☑ 5, ☑ 6 (2026-07-09; phase 2 landed first —
the notification channel does not depend on the groundwork — and took
`pad_clicked` deletion with it; `notify_cursor_moved` takes a `Point` — user
feedback).  Phase deltas vs the plan: the port attribute is named
``viewport_port`` (bare ``viewport`` collides with ``QGraphicsView.viewport()``
until the phase-6 Protocol swap); ``WheelNotchAccumulator`` landed in
``ports/inputs_details.py`` and is used by ``FloatTransform`` only —
ViewCamera's wheel *burst buffer* has different semantics (timer-flushed
total, partial ticks round to ±1) and deliberately stays as-is;
``drag_corner_text`` keeps its Qt-side ``mapToScene`` — it is a semantic
per-tool overlay of exactly the §3 kind, not seam surface.  Phase 5 landed
for the *generic* canvas menu; SelectTool's item-aware RMB menu stays
in-tool (parameterised data-tuple entries are not expressible as plain
action-ids — parity nuance (b) is real; see todo.md).  Phase 6: the
Protocol keeps two sanctioned Qt touchpoints — ``scene()`` (per D1) and
``window()`` (dialog parenting, D4 debt); phase 7 (semantic previews,
ExecutorPort, dialog prompts) remains optional/per-tool per §3 and todo.md.

1. **Groundwork**: delete `pad_clicked`; add `gui/qt_geometry.py`
   (`Point/Rect/Transform2D ↔ QPointF/QRectF/QTransform`); the 4 Qt-typed ABC
   members switch to domain types.
2. **View notification channel** (~10 `statusBar()` sites →
   `view.notify_status({...})`, today's texts kept in `"text"` for parity;
   plus `notify_cursor_moved`/`escape_to_select` conversion and the
   MainWindow subscription) — status becomes assertable by structural
   fields.
3. **Input DTOs + bool consumption** (the big mechanical phase; sub-commits
   pointer → wheel → key; key-fall-through parity tests FIRST; includes the
   armed/float_transform de-leak).
4. **ViewportPort** + `QtViewport` + affine fake + the Qt-vs-fake property
   test — ViewCamera math becomes unit-testable.
5. **`get_context_at` + `notify_context` + QMenu construction moves to
   MainWindow; HelpOverlayPort + keybindings provider** — ToolHost is
   widget-free; context/action-set content tests need no fake at all.
6. **Protocol swap**: `AbstractBoardView` drops `QGraphicsView`, `QtABCMeta`
   deleted, `HeadlessBoardView` lands; the swap is type-level only.
7. **Optional, per tool, on demand**: semantic previews (route → place →
   simplify → select, hardest last); `ExecutorPort`; dialog prompts (per D4).
