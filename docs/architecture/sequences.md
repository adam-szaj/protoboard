<!-- SPDX-License-Identifier: MIT -->
# Sequence flows

Call-level sequences for the three most important interactions, faithful to the
real method names. The **Qt-free boundary** is at the entry to `EditController`
(`services/editing/controller.py`) and downward — View + Tools +
SceneComposer/Renderers are the Qt layer; `KeyMap` (orchestrated by `ToolHost`)
is the View↔Controller seam.

- **[1] Armed rotate (E5)** — arm → step → accept, on the preview-model.
- **[2] Drag-commit** — mouse drag of a component (contrast: trial PadIndex +
  glide, vs armed's preview-copy + full refresh).
- **[3] Autoroute** — background `QThread`, Qt-free router, apply on the stack.

---

## [1] Armed rotate (E5 preview-model)

Key contrast with drag: a rotate/flip changes body orientation, which a cheap
`setPos` glide cannot show — so each step does a full `view.refresh()` that draws
the **preview** layout (`_render_layout()` returns `armed.preview_layout()` while
armed). The live layout is mutated exactly once, at accept.

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant ST as SelectTool<br/>(gui/tools/select_tool.py)
    participant TH as ToolHost / KeyMap<br/>(gui/tool_host.py)
    participant ATT as ArmedTransformTool<br/>(gui/tools/armed_transform_tool.py)
    participant EC as EditController<br/>(services/editing/controller.py)
    participant AT as ArmedTransform<br/>(services/editing/armed_transform.py)
    participant LO as Layout<br/>(domain/layout/layout.py)
    participant CS as TransformDragSetCommand + Stack<br/>(domain/commands/drag_cmds.py)
    participant SC as SceneComposer + Renderers<br/>(gui/scene_composer.py)

    Note over ST,SC: View (Qt) above ── Controller (Qt-free) below
    User->>ST: [R] key
    ST->>ST: on_key → arm_rotate → _arm_transform("rotate")
    ST->>ST: _rotatable_affected() → (comp_ids, group_id)
    ST->>TH: view.tools.set_tool(ArmedTransformTool(..., return_to))
    TH->>ATT: deactivate old / activate(session, view)
    ATT->>EC: begin_transform(comp_ids, ..., preview=_on_preview)
    EC->>AT: ArmedTransform(state, snap, execute, ...)
    AT->>LO: preview_copy()
    Note over AT,LO: fork PREVIEW — live stays pristine
    LO-->>AT: preview_layout
    AT-->>EC: armed
    EC-->>ATT: armed
    TH->>SC: view.refresh()

    User->>ATT: [R] step
    ATT->>EC: rotate_armed(90)
    EC->>AT: _rotate(90)
    AT->>LO: _write_world_op / _transform_wires (mutate PREVIEW)
    AT->>AT: _emit_preview → _classify() via PreviewState
    AT-->>ATT: preview callback _on_preview(decision)
    ATT->>SC: view.refresh()
    SC->>EC: armed_transform (via _render_layout)
    EC-->>SC: preview_layout()
    SC-->>User: renderers draw the PREVIEW pose

    User->>ATT: Enter (confirm)
    ATT->>EC: accept_transform()
    EC->>AT: accept()
    AT->>AT: _classify() → diff original→preview
    AT->>CS: _build_commit_command → TransformDragSetCommand
    AT->>EC: execute(cmd)
    EC->>CS: stack.execute(cmd) → apply (live mutated ONCE)
    EC-->>ATT: armed_transform is None
    ATT->>TH: return_to() → set_tool(SelectTool)
    TH->>SC: view.refresh() (draw live)
```

---

## [2] Drag-commit (mouse drag)

Contrast with armed:

| | Drag | Armed transform |
|---|---|---|
| pose store | mutates **live** layout in-place | mutates a **preview** copy |
| conflict model | **trial PadIndex** (clone + resync) | `PreviewState` over the preview |
| live feedback | **glide** `grp.setPos(delta)` (no rebuild) | full `view.refresh()` |
| ops | translation only | rotate / move / flip |

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant ST as SelectTool<br/>(gui/tools/select_tool.py)
    participant DC as DragController<br/>(gui/selection/drag_controller.py)
    participant SC as SceneComposer<br/>(gui/scene_composer.py)
    participant DCL as classify_drag_commit<br/>(services/editing/drag_commit.py)
    participant CS as MoveDragSetCommand + Stack<br/>(domain/commands/drag_cmds.py)

    Note over ST,SC: View (Qt) above ── drag_commit / commands (Qt-free) below
    User->>ST: press on pin
    ST->>ST: on_press → _arm_drag(sel)
    ST->>DC: DragController(session, drag_set, snap, preview, execute)
    Note over DC: snapshot _orig_positions (LIVE) — trial PadIndex lazy = None

    User->>ST: mouse move
    ST->>DC: on_move(scene_pos)
    DC->>DC: _settle_at → _apply_delta (mutate LIVE, translation-only)
    DC->>DC: _resync_trial_pad_index (clone once + remove/add)
    DC->>DCL: _classify → classify_drag_commit (trial PadIndex)
    DCL-->>DC: DragCommitDecision
    DC-->>ST: preview(decision) → _drag_preview
    ST->>SC: session.drag_moved.emit() → on_drag_moved
    SC-->>User: glide grp.setPos(delta) (NO rebuild)

    User->>ST: release
    ST->>DC: on_release(scene_pos)
    DC->>DC: _settle_at (re-classify at release)
    alt decision.can_commit + auto_accept
        DC->>DC: confirm_floating → _resolve_drops
        DC->>CS: _build_move_command → MoveDragSetCommand
        DC->>CS: execute(cmd) → stack.execute → apply
        CS-->>DC: committed
    else HARD conflict
        DC-->>ST: stays FLOATING (await confirm/cancel)
    end
    ST->>SC: view.refresh() (full rebuild, resets glide setPos)
```

---

## [3] Autoroute (background QThread)

The worker snapshots an immutable `RouteRequest` **on the main thread** (no race
with GUI mutations), routes on the background thread (Qt-free), and applies the
result on the main thread as one `MacroCommand` on the stack. GUI never calls
the synchronous `EditController.autoroute` (that variant is for the CLI); both
paths share `apply_autoroute`.

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant MW as MainWindow<br/>(gui/main_window.py)
    participant DLG as AutorouteDialog + QProgressDialog<br/>(gui/dialogs/autoroute_dialog.py)
    participant W as AutorouteWorker — QThread<br/>(gui/workers/autoroute_worker.py)
    participant R as AStarRouter / registry<br/>(services/routing/builtin.py)
    participant EC as EditController<br/>(services/editing/controller.py)
    participant CS as MacroCommand / AddWire + Stack<br/>(domain/commands/component_cmds.py)

    Note over MW,W: main thread (Qt) above ── Qt-free routing (worker thread) below
    User->>MW: autoroute action
    MW->>MW: _autoroute → _autoroute_with_net_ids(net_ids)
    MW->>DLG: AutorouteDialog.exec()
    DLG-->>MW: routing_settings()
    MW->>W: AutorouteWorker(board, layout, ...)
    Note over W: RouteRequest.from_session (immutable snapshot on main thread)
    MW->>W: finished.connect(_release_gate → _on_autoroute_done) + progress.connect
    MW->>MW: session.set_autoroute_active(True) — gate UI
    MW->>W: worker.start()

    activate W
    W->>W: run() [background thread], disable GC
    W->>R: registry.default().route(request, sink)
    R-->>W: AutorouteResult
    loop optimizer chain
        W->>R: opt.optimize(request, result, sink)
    end
    W-->>MW: progress.emit(...) (queued to main thread)
    W->>MW: finished.emit(result)
    deactivate W

    MW->>MW: _release_gate → set_autoroute_active(False)
    MW->>MW: _on_autoroute_done(result)
    MW->>EC: apply_autoroute(result)
    EC->>EC: filter dup signatures → AddWireCommand list
    EC->>CS: MacroCommand("Autoroute N wires") → _execute_silent → stack.execute
    CS-->>EC: applied count
    EC-->>MW: applied
    MW->>MW: mark_dirty / board_view.refresh / layout_changed.emit
    MW-->>User: summary QMessageBox
```
