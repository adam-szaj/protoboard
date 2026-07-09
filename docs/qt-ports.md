# Qt Ports — analysis: making BoardView & friends testable headless

Status: **analysis / decision gate** (2026-07-09). No implementation decision has
been taken; this document inventories the Qt coupling of the board canvas and
proposes the abstract, Qt-independent interfaces ("ports") that would make the
BoardView ecosystem — the view, its tools and its controllers — testable
without a realized `QGraphicsView`.

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
    def map_scene_to_view(self, scene_pt: Point) -> Point: ...
    def view_scale(self) -> float: ...                  # |m11|
    def view_transform(self) -> Transform2D: ...
    def set_view_transform(self, t: Transform2D) -> None: ...
    def viewport_rect(self) -> Rect: ...
    def scroll_position(self) -> Vector: ...
    def set_scroll_position(self, v: Vector) -> None: ...
    def center_on(self, scene_pt: Point) -> None: ...
    def fit_in_view(self, scene_rect: Rect) -> None: ...
    def request_repaint(self) -> None: ...
    def set_cursor(self, cursor: CursorKind) -> None: ...
    def screen_metrics(self) -> ScreenMetrics | None: ...
```

Absorbs every inherited-API call in `view_camera.py`, `tool_host.py`,
`drag_corner_text.py` and `board_view.py` listed in §1.1 (mapping, transform,
scrollbars, centerOn/fitInView, cursor, `window().screen()`). The adapter
`QtViewport(view)` is the **only** QPointF/QTransform conversion site; the
test fake implements the mapping with exact `Transform2D` affine math, which
makes ViewCamera's zoom-clamp / fit / mirror arithmetic unit-testable with
real numbers instead of mocks. The four Qt-typed ABC members (§1.1) switch to
`Point` / hex `str` in the same breath — several tools already round-trip
`Point(scene_pt.x(), scene_pt.y())` today, so this *removes* conversions.

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
def notify_status(self, payload: Mapping[str, object]) -> None: ...
def notify_cursor_moved(self, x: float, y: float) -> None: ...
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
- **Trade-off (decision D7):** `Mapping[str, object]` is weakly typed under
  `mypy --strict` (the repo prefers frozen dataclasses). Middle ground: a
  thin `StatusEvent(kind: str, data: Mapping[str, object])` envelope — still
  flexible, but with a typed discriminator.

The dead `pad_clicked` is deleted. `session.py` keeps its QObject signals —
out of scope here.

### 2.4 `MenuPort`

```python
@dataclass(frozen=True, slots=True)
class MenuItem:
    action_id: str
    label: str
    enabled: bool = True
    shortcut_hint: str = ""
    separator_before: bool = False
    children: tuple["MenuItem", ...] = ()

class MenuPort(Protocol):
    def show_menu(self, items: Sequence[MenuItem],
                  at_view_pos: Point) -> str | None: ...
```

Menu *construction* becomes a pure function returning `list[MenuItem]` —
finally testable ("locked component ⇒ Rotate disabled"); the Qt adapter
builds the QMenu, maps to global and execs; the fake returns a scripted
choice and records the spec. Absorbs the ToolHost canvas menu and the
SelectTool context menu, and removes the last `mapToGlobal`/`mapFromScene`
users. Context-menu content is user-facing behaviour with zero coverage
today — this is the highest-leverage small port.

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

### 2.6 Colour

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
members + `viewport: ViewportPort` + `menus: MenuPort` + `help_overlay:
HelpOverlayPort` + the notification channel (§2.3); the concrete
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
| D7 | `notify_status` payload shape | bare `Mapping[str, object]` / thin `StatusEvent(kind, data)` envelope | typing strictness vs flexibility (see §2.3) | bare dict (user's proposal) |

## 6. Appendix — non-binding migration sketch

Each phase green + committable; existing qtbot tests run unchanged throughout
(nothing is swapped until the type-level Protocol change at the end):

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
5. **MenuPort + HelpOverlayPort + keybindings provider** — ToolHost is
   widget-free; menu-content tests.
6. **Protocol swap**: `AbstractBoardView` drops `QGraphicsView`, `QtABCMeta`
   deleted, `HeadlessBoardView` lands; the swap is type-level only.
7. **Optional, per tool, on demand**: semantic previews (route → place →
   simplify → select, hardest last); `ExecutorPort`; dialog prompts (per D4).
