<!-- SPDX-License-Identifier: MIT -->
# Module dependencies & layers

High-level map of the real dependency edges between layers, derived from the
actual import graph (`grep`/read), not from the declared architecture. Renders
natively on GitHub (Mermaid).

The declared stack (CLAUDE.md §3) is:

```
GUI (PySide6)
  ↑ Qt signals
Application / Controller (EditController, Tools, Selection)
  ↑ Commands
Domain Model (pure Python — MUST NOT import Qt)
  ↑
Services (DRC, Router, KiCAD I/O, Persistence, Export)
```

The **seam** is `EditController` (`controller/editing/controller.py`), typed
against the `EditState` protocol. Both the GUI `EditorSession` and the headless
`HeadlessSession` satisfy it, so GUI tools and the headless CLI drive the *same*
verbs on one controller instance — `EditorSession(HeadlessSession, QObject)`
inherits the headless implementation and only adds Qt.

The controller-layer migration (this diagram's previous "Planned" section) has
**landed**: `controller/` is a single Qt-free top-level package —
`tests/unit/test_controller_no_gui_import.py` asserts no file under it imports
`protoboard.gui`, anywhere, including `TYPE_CHECKING`-only imports.

## Diagram

```mermaid
graph TD
    subgraph GUI["GUI — PySide6"]
        MW[main_window]
        BV["board_view / AbstractBoardView"]
        TH[tool_host]
        SC[scene_composer]
        RND["renderers/*"]
        GT["tools/gui_tools — Qt-composed tools
        (ViewToolMixin + controller/tools/*Tool)"]
        PREV["tools/*_preview — Qt*Preview
        (implements controller/tools/*_preview Protocol)"]
        TO["tools/transform_overlays — TransformOverlays
        (implements controller TransformOverlaysPort)"]
        HLR["highlight/renderer + highlight_state::HighlightStack"]
        KM["keymap::KeyMap / AbstractKeyMap — the View↔Controller seam"]
        THEME["layer_state"]
        SESS["session::EditorSession"]
    end

    subgraph CTRL["controller — Qt-free (no protoboard.gui import, enforced)"]
        EC["editing/controller::EditController"]
        ES["«protocol» EditState"]
        HS["editing/headless_session::HeadlessSession"]
        CLI["editing/cli"]
        AT[editing/armed_transform]
        PS[editing/preview_state]
        DGC[editing/drag_commit]
        PORTS["ports/* — Qt-free DTO"]
        CTOOLS["tools/* — command machines
        (select_tool, place_tool, route_tool,
        cut_tool, simplify_tool, armed_transform_tool)"]
        DCD["tools/drag_commit_decision"]
        CPREV["tools/*_preview — Protocol only"]
        CTO["tools/transform_overlays::TransformOverlaysPort"]
        CSEL["selection/* — SelectionService, hit_test,
        drag_controller, mouse_input, …"]
        CTHEME["theme / app_settings / interaction_mode /
        highlight_entry / highlight_controller /
        selection_fsm / selection_axes"]
    end

    subgraph SVC["Services"]
        AR[routing/autorouter]
        ASTAR[routing/astar]
        RS[("routing/astar_rs — Rust")]
        CM[routing/cost_model]
        HIER[routing/hierarchical]
        PROTO["routing/protocol — registry"]
        DRCE[drc/engine]
        PY[persistence/project_yaml]
        BY[persistence/board_yaml]
        EXP["export/* — Qt"]
        KIC["kicad/* — parser/importer/exporter"]
        DBG["debug_trace — imports Qt!"]
        DD[drag_detector]
    end

    subgraph DOMAIN["Domain Model — declared Qt-free"]
        BOARD[board]
        COMP[components]
        SCHEM[schematic]
        LAYOUT[layout]
        CONN[connectivity]
        CMDS["commands / CommandStack"]
    end

    %% GUI internal
    MW --> BV
    MW --> SESS
    BV --> TH
    BV --> SC
    BV --> HLR
    BV --> KM
    TH --> GT
    GT --> PREV
    GT --> TO
    SC --> RND
    SC --> NS[routing/net_status]
    SESS -. inherits .-> HS

    %% GUI composes the controller's Qt-free tools + implements its Protocols
    GT -. composes .-> CTOOLS
    PREV -. implements .-> CPREV
    TO -. implements .-> CTO
    HLR -. HighlightEntry from .-> CTHEME

    %% Controller seam
    HS --> EC
    EC -. typed by .-> ES
    HS -. satisfies .-> ES
    SESS -. satisfies .-> ES
    CLI --> HS
    CTOOLS --> EC
    CTOOLS --> CSEL
    CTOOLS --> PORTS
    CTOOLS --> DCD
    CTOOLS --> CPREV
    CTOOLS --> CTO
    CTOOLS --> CTHEME
    AT --> PORTS

    %% EditController outbound verbs (lazy/local imports)
    EC --> AR
    EC --> PROTO
    EC --> DRCE
    EC --> PY
    EC --> EXP
    EC --> CMDS

    %% routing internals
    AR --> ASTAR
    ASTAR -. optional .-> RS
    ASTAR --> CM
    AR --> CM
    HIER --> AR

    %% persistence
    PY --> BY

    %% Services -> Domain (legit downward)
    AR --> DOMAIN
    DRCE --> DOMAIN
    PY --> DOMAIN
    EXP --> DOMAIN
    KIC --> DOMAIN

    %% === Layer VIOLATIONS still open (upward services -> gui, runtime) ===
    PY -->|"runtime ⚠"| THEME
    EXP -->|"runtime ⚠"| THEME
    EXP -->|"runtime ⚠"| RND
    DD -->|"runtime ⚠"| DCD

    %% === Transitive Qt leak into domain ===
    COMP -->|"@traced ⚠"| DBG

    classDef qtfree fill:#e8f5e9,stroke:#2e7d32,color:#1b5e20;
    classDef qttaint fill:#ffebee,stroke:#c62828,color:#b71c1c;
    class DOMAIN,BOARD,COMP,SCHEM,LAYOUT,CONN,CMDS,CTRL,EC,ES,HS,CLI,AT,PS,DGC,PORTS,CTOOLS,DCD,CPREV,CTO,CSEL,CTHEME qtfree;
    class DBG,EXP qttaint;
```

## Reading the diagram

- **Green** nodes are (declared) Qt-free — includes the whole `controller/`
  subgraph now that the guard test enforces it. **Red** nodes import Qt.
- Solid arrows = imports / calls. Dashed = protocol satisfaction / inheritance
  / implementation.
- Arrows labelled **⚠** are edges that violate the declared layer direction —
  see below. The two edges the controller migration was scoped to resolve
  (`armed_transform → gui.ports`, `drag_detector →
  gui.tools.drag_commit_decision`) are gone: both targets now live in
  `controller/`, so what were upward violations are intra-`controller/` edges
  (`AT --> PORTS`, `CTOOLS --> DCD` above).

## The seam: EditController / EditState

- `EditState` (`controller/editing/controller.py`) is a `@runtime_checkable`
  `Protocol`. `HeadlessSession` (`controller/editing/headless_session.py`)
  satisfies it directly; `EditorSession` (`gui/session.py`) **is a**
  `HeadlessSession` + `QObject` (adds the `layout_changed` signal).
- One `EditController` per session (`headless_session.py`, lazy
  `edit_controller` property). GUI tools and the CLI (`editing/cli.py`) call the
  same verbs on it: `delete` / `rotate` / `flip` / `place_component` /
  `add_wire` / `begin_transform` / `apply_autoroute` / `run_drc` / `save` /
  `export`, plus the Task 7 facade: `tool_commands()` / `dispatch_tool_command()`
  and active-tool ownership (`set_active_tool` / `active_tool` /
  `active_tool_changed` notify hook).
- Every outbound service call from `EditController` uses a **lazy/local import**
  (routing, drc, persistence, export) so the verb layer itself stays Qt-free —
  the Qt-heavy export code is only imported inside `export()`.

## The seam: KeyMap stays in GUI

`controller/tools/*` never resolves a raw `Qt.Key` — each tool declares
`key_actions()` / `key_claims()` / `command_table()` by **action id**; the GUI
composition layer (`gui/tools/gui_tools.py`, one class per controller tool,
mixing in `ViewToolMixin`) resolves actual keys through `gui/keymap.py`'s
`KeyMap` and either calls the matching `on_key` translation or drives the
Controller facade's `dispatch_tool_command(action_id)`. `KeyMap`/
`AbstractKeyMap` are therefore the one View↔Controller seam that stays firmly
on the GUI side (`controller/tools/base.py::KeyResolver` is the narrow,
Qt-free Protocol the tool base needs from it — never the full `KeyMap`).

Same pattern for previews and overlays: each `controller/tools/*_preview.py`
declares a Qt-free `Protocol` (`SelectPreview`, `PlacePreview`,
`RoutePreview`, `SimplifyPreview`) and `controller/tools/transform_overlays.py`
declares `TransformOverlaysPort`; the matching `gui/tools/*_preview.py` /
`gui/tools/transform_overlays.py` hold the `Qt*Preview` / `TransformOverlays`
implementations, injected by `gui_tools.py`'s `activate()` as the Qt default
when the tool wasn't constructed with one already (tests inject fakes instead).

## Known layer violations (as of 2026-07)

These are real edges in the runtime import graph that invert the declared
"services below GUI" direction. Flagged for visibility, not necessarily bugs —
several move Qt-free DTOs, following an established precedent.

1. **services → gui (upward), runtime, top-level imports**
   - `services/drag_detector.py` → `controller.tools.drag_commit_decision`
     (`DragCommitDecision`, Qt-free DTO) — no longer a `gui` import after the
     controller migration, but still upward relative to `services/`.
   - `services/persistence/project_yaml.py` → `gui.layer_state`
     (`LayerOverrides`, `LayerVisibility`)
   - `services/export/{snapshot,paint,png,pdf,svg}.py` → `gui.{theme,
     layer_state,renderers}` (export drives the GUI renderers to rasterise) —
     note `gui.theme` is a re-export of `controller.theme.Theme`.

2. **domain → services → Qt (transitive Qt leak)**
   - `domain/components/package/{solver,loader,reach,shapes}.py` →
     `services/debug_trace` → `PySide6.QtCore` (top-level, unguarded).
   - The domain source is Qt-free in *content*, but importing these package
     modules pulls in `PySide6.QtCore` at runtime through the `@traced`
     decorator. The rule "domain must be testable without a display server"
     holds behaviourally (offscreen), but the import graph is not strictly
     clean.

3. **gui → services (downward, legitimate)**
   - `gui/scene_composer.py` → `services.routing.net_status.routed_net_ids`.
     This is the declared direction (GUI consuming a service), listed only for
     completeness.

> These reflect what was true at the time of writing. Before acting on any of
> them, re-check the file still imports what's claimed.
