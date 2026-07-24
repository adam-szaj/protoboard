# Core wire semantics — whole-wire vs. fragment operations

This document analyses the **semantics of operations on wires** in two modes:

1. **Whole-wire** — what a user can do to a wire that is merely *selected*
   (SelectTool), via context menu, menu bar, keyboard or drag, **without
   entering wire-edit mode**.
2. **Fragment (wire-edit) mode** — what a user can do to a *selected fragment*
   (an inclusive index range on one wire, or across two same-net wires) after
   arming wire-edit with `[E]`.

The **design intent** (stated in
[armed-rotate-and-subtool-delegation.md](armed-rotate-and-subtool-delegation.md)
§2.1 A) is that wire-edit becomes a *delegated sub-tool* whose op-table
converges the two: *"simplify, reroute, autoroute, flip, layer-move form one
coherent wire op-table"*, so **the same operations should be reachable on a
selected fragment as on a whole wire**. This document measures the current
distance from that goal — the inconsistencies and gaps.

> Scope: this is a **semantics / coverage** analysis (what op exists where, on
> what granularity, with what net/undo consequences). The *electrical* rules
> (shorts, pin penetration, vias, half-orphans) are the separate single source
> of truth in
> [pad-pin-wire-junction-semantics.md](pad-pin-wire-junction-semantics.md) and
> are referenced, not restated.

---

## 1. The model — two natural granularities

`domain/layout/wire.py`.

A **`Wire`** is one solder-able conductor between exactly two **Knots**
(`a` / `b` — `PinKnot`, `JointKnot` or `WirePinKnot`). It is decomposed into
**`Fragment`s**: maximal same-side sub-paths, split only where the wire changes
board side through a plated-through pad (a via).

The model therefore bakes in **two granularities** the UI must respect:

| Granularity | What it is | Natural operations |
|---|---|---|
| **Whole wire** | the artifact; stable `id`; `a`/`b` knots | delete, drag, insulate, re-side, simplify, unweave, merge, wire-pin |
| **Fragment** | a contiguous index range `[start_idx : end_idx]` on the flattened `path` | reroute, autoroute, flip-to-other-side, delete-piece |

Crucially, the attributes live at **different granularities**:

| Attribute | Granularity | Consequence |
|---|---|---|
| `insulated` | **per-wire** (`wire.py:169`) | "set insulated" can only mean the whole wire |
| `color` | **per-wire** (`wire.py:171`) | same |
| `side` | **per-fragment** (`Fragment.side`, `wire.py:33-43`) | "move to layer" / "flip" *can* meaningfully target one fragment |
| net binding | **derived**, not stored | changing geometry can silently change the net (§5) |
| `id`, `a`, `b` | **per-wire** | any fragment op that has to *cut* must mint new wires → new ids |

This asymmetry is the root cause of most gaps below: an op-table that offers
both "set insulated" and "flip fragment" on the same "fragment" selection is
mixing a whole-wire attribute with a per-fragment one.

`WireEditState` (`controller/selection/wire_edit.py:63-93`) is the fragment
value object: `start_wire_id/start_idx` … `end_wire_id/end_idx`, plus reroute
`waypoints`. `is_cross` is true when the two wire ids differ (a fragment
spanning two sibling wires of the same net).

---

## 2. Whole-wire operations (no wire-edit mode)

Available once a wire is selected (`WireSelectable`). Sources: context menu
(`SelectTool._ctx_wire_options`, `select_tool.py:3299`), menu bar
(`gui/actions.py:201-218, 334-335`), keyboard, drag.

| # | Operation | Trigger | Command | Multi? |
|---|---|---|---|---|
| W1 | **Delete wire** | `[Del]`/`Backspace`; selection | `RemoveWireCommand` (via `EditController.delete`) | yes |
| W2 | **Move / drag wire** | LMB drag (only as an *internal wire* of a moved component/group set) | `MoveDragSetCommand` | yes |
| W3 | **Set bare / insulated** | ctx `wire_insulated`; menu `Set Insulated`/`Set Bare` | `SetWireInsulatedCommand` | yes |
| W4 | **Move to layer** | ctx `wire_side`; menu `Move to Top`/`Bottom` | `SetWireSideCommand` | yes |
| W5 | **Flip to opposite layer** | menu `Flip to Opposite Layer`; `[F]` with no fragment armed | `SetWireSideCommand` (toggle) | yes |
| W6 | **Simplify path** | ctx `simplify_wire` (+ chebyshev / bare-parity); menu; SimplifyTool `Shift+LMB` | `ReplaceWireFragmentsCommand` | yes |
| W7 | **Optimize with plugin** | ctx `optimize_with` | `RemoveWireCommand` + `AddWireCommand` macro | yes |
| W8 | **Unweave (topological)** | ctx `unweave_wires`; menu — **requires ≥2 wires** | `Remove`+`Add` macro | ≥2 |
| W9 | **Merge / fuse fragments** | ctx `merge_wire_fragments`; menu — **requires a wire with ≥2 fragments** | `FuseTouchingWiresCommand` + `ReplaceWireFragmentsCommand` | yes |
| W10 | **Convert endpoint → wire-pin** | menu `Convert Endpoint to Wire-pin` | `ConvertEndpointToWirePinCommand` | one endpoint |
| W11 | **Convert wire-pin → endpoint** | menu `Convert Endpoint from Wire-pin` | `ConvertWirePinToEndpointCommand` | yes |
| W12 | **Select by net** | ctx `select_net_*` | — (selection only) | — |
| W13 | **Highlight selection** | ctx `highlight_selection` / `clear_highlight` | — (highlight stack) | — |

There is **no direct "change this wire's net"** operation (W-none): net is
derived. Net binding is (re)computed only at *wire creation* by
`resolve_wire_nets` (`controller/editing/wire_nets.py:44`), or edited via
*pin* assign/disconnect — never as a whole-wire mutation. See §5.

**W2 caveat.** A bare wire is *not* independently draggable — `_arm_drag`
(`select_tool.py:1275`) only builds a drag when an anchor *component* exists;
a lone wire selection stays a plain selection. Wires travel only as internal
wires of a moved component/group.

---

## 3. Fragment operations (wire-edit mode)

### Entry / selection / exit

- **Enter:** `[E]` after selecting a single wire → `arm_wire_edit`
  (`select_tool.py:2102`). (The legacy `Ctrl+click` entry is gone.) On entry
  `_ensure_wire_ready_for_edit` (`:2135`) materialises every board pad on the
  path as a vertex (so post-simplify wires carrying only endpoints become
  selectable).
- **Select a fragment:** two LMB clicks on wire vertices → `_range_picked`
  (`:2185`) builds the `WireEditState`. The hit-test is **net-restricted**
  (`hit_test.py:220-235`): only vertices on same-net wires match, so a
  cross-wire fragment stays within one net. Minimum fragment is two distinct
  vertices (a zero-length range is rejected).
- **Exit:** `[Esc]`, any plain board LMB, or completing any op → `_wire_edit_clear`.

### Operations (`_wire_edit_on_key` op-table, `select_tool.py:2218`)

> **2026-07-24 update:** the op-table below (F1–F5, plus simplify F6 in §4) has
> been extracted into `WireEditTool` (`controller/tools/wire_edit_tool.py`),
> which conforms to the `ArmedSubTool` protocol
> (`docs/armed-rotate-and-subtool-delegation.md` §5, §2.1 A). Both whole-wire
> and full-span-fragment ops now run through `WireEditArm`
> (`controller/editing/wire_edit_arm.py`) — the command-*building* half of each
> op, unifying control-flow on top of the data-level `WireRange` that already
> spans full-wire and fragment cases identically. The op semantics below are
> unchanged; only the entry point moved off `select_tool.py`.

| # | Operation | Key | Command | Scope | Guard |
|---|---|---|---|---|---|
| F1 | **Reroute** | `[W]` | same-wire `ReplaceWireFragmentsCommand`; cross-wire `Macro[excise, AddWire]` | fragment, single or cross | resulting path ≥ 2 pts |
| F2 | **Autoroute** | `[A]` | A* (`autoroute_fragment_path`) → same command as reroute | fragment, single or cross | path found |
| F3 | **Flip fragment to other layer** | `[F]` / ctx `flip_fragment` | `FlipWireFragmentCommand` | fragment, **same-wire only** | `start < end`; cross-wire → falls back to **W5** |
| F4 | **Delete fragment** | `[Del]`/`Backspace` | whole wire → `RemoveWireCommand`; interior → `Remove`+two `AddWire` halves; cross-wire → excise macro | fragment, single or cross | valid range |
| F5 | **Cancel** | `[Esc]` | — | — | — |

No **vertex-drag**, **insert-vertex**, **split-at-point**, **extend**, or
**change-net** operations exist in wire-edit mode. The smallest operable unit
is a two-vertex fragment; there is no single-segment or single-vertex
primitive. (Vertex-drag of a *lead* endpoint is the separate `Ctrl+R` re-drag
feature, not part of wire-edit.)

---

## 4. Coverage matrix — the core comparison

The user's stated idea: *"via editing you should be able to perform the same
operations on a selected fragment that you can perform on a whole wire without
entering edit mode."* Measured against the two inventories:

| Operation | Whole wire | Fragment | Status |
|---|:---:|:---:|---|
| Delete | W1 ✅ | F4 ✅ | **parity** (delete is the one op that bridges cleanly) |
| Flip / move to layer | W4 / W5 ✅ | F3 ✅ (same-wire) | **near-parity**; cross-wire fragment falls back to whole-wire (loss of intent, §6.1) |
| Reroute (manual path) | — | F1 ✅ | **fragment-only** — no whole-wire equivalent (arguably correct: reroute *is* a path edit) |
| Autoroute | — (only *group* autoroute / global `F6`) | F2 ✅ | **fragment-only** — no "autoroute this whole wire" |
| **Simplify** | W6 ✅ | ✅ `[S]` (F6) | **CLOSED** — `build_simplify_range_command`; whole-wire is the `full_span` case of the same builder (same code) |
| **Optimize with plugin** | W7 ✅ | ❌ | **GAP** |
| **Set bare / insulated** | W3 ✅ | ✅ (same-wire, via split — `build_set_insulated_range_command`; cross-wire deferred) | — |
| **Unweave** | W8 ✅ | ❌ | inherently multi-wire; no fragment analog (acceptable) |
| **Merge / fuse fragments** | W9 ✅ | ❌ | inherently whole-wire topology; no fragment analog (acceptable) |
| **Convert endpoint ↔ wire-pin** | W10/W11 ✅ | n/a (endpoint op) | **layering FIXED** — dispatch moved to `SelectTool.convert_*` + wire context menu; MainWindow slots now delegate (§6.3). Stays endpoint-scoped, not a range op. |
| Select by net / highlight | W12/W13 ✅ | ❌ | n/a — selection ops, not mutations |

**Direction of the asymmetry.** The fragment op-table (reroute / autoroute /
flip / delete) and the whole-wire menu (insulate / side / simplify / unweave /
merge / wire-pin) **barely overlap** — only *delete* and *flip/side* bridge.
The design intent to make them "one coherent wire op-table" is **not yet met**:

- **The clear gaps** (an op that is meaningful per-fragment but only exists
  whole-wire): **Simplify (W6)** and **Optimize (W7)**. Simplifying a *piece*
  of a long jumper while leaving the rest untouched is a coherent, useful op
  that the model already supports (`ReplaceWireFragmentsCommand` over a slice),
  and it is exactly the example the design doc calls out. It is missing.
- **The reverse gaps** (an op meaningful whole-wire but with no fragment entry
  even though it *could* have one): **autoroute a whole wire** (F2 exists for a
  fragment but there is no "autoroute this entire wire end-to-end" whole-wire
  action distinct from group/global autoroute).

---

## 5. Net-binding gap (correctness, not just coverage)

**Net membership is derived, never stored on the `Wire`** (`cached_wire_net_map`
/ `queries.net_for_wire`). It is (re)classified against pins/nets **only** at
wire *creation*, by `resolve_wire_nets` (`wire_nets.py:44`), which is invoked
from `EditController.add_wire` / RouteTool via `AddWireWithNetsCommand`.

**Fragment reroute / delete / excise mint fresh wires with plain
`AddWireCommand`** (`wire_edit.py:267, 355, 412`), which does **not** run
`resolve_wire_nets`. Consequences:

- **Same-wire reroute (F1):** endpoints unchanged → net re-derives identically
  → **safe**.
- **Cross-wire excise + reroute (F1 cross / F4 cross):** intermediate wires are
  *removed* and one replacement is *added* (`excise_cross_wire_range`,
  `wire_edit.py:231`). Because the replacement uses `AddWireCommand`, net
  classification is **never re-run**. If the replacement's endpoints no longer
  touch the same pins, the net **silently loses the removed wires' contribution**
  with no `NetMergePrompt` and no reclassification.

This is the mechanism behind the already-tracked
`memory/project_wire_id_substitution_followup.md` follow-up (T-junction split
minting a fresh UUID on one half) and is worth a DRC pass after any cross-wire
fragment op.

> Recommended invariant: **any wire-add that replaces user geometry (fragment
> excise, optimize, unweave, merge) should route through the same net-resolution
> path as a freshly drawn wire**, or explicitly assert the net set is
> unchanged. Today only RouteTool creation does.

---

## 6. Inconsistencies and design smells

### 6.1 Cross-wire fragment flip is unsupported (not a silent-degrade)

`build_flip_fragment_command` returns `None` for a cross-wire selection
(`wire_edit.py:441`). `_flip_wire_fragment` (`select_tool.py:3692`) only falls
back to `_move_selected_wires_to_side` (**W5**, whole wire) when **no fragment is
armed** (`state is None`); when a *cross-wire fragment* is armed it hits the
`cmd is None` branch and shows a status message ("Flip: pick a fragment on a
single wire") — it does **not** silently widen to a whole-wire move. So the
observed behaviour is correct; the only gap is that cross-wire *fragment* flip is
**unsupported**. (An earlier draft of this section claimed a silent degrade;
verified against the code and the `TestBuildFlipFragmentCommand` golden in
`tests/unit/test_wire_edit_helpers.py`, that is not the case.) The open choice is
merely: implement cross-wire fragment flip, or leave it refused-with-a-message.

### 6.2 "Fragment" mixes two granularities — but insulate-on-a-slice is feasible

`insulated` and `color` are per-wire; `side` is per-fragment. Because
`Fragment` has no `insulated` field, "insulate this sub-range" cannot be a flag
flip — but it **is** feasible, and is now implemented, by the same split +
net-rederivation `delete-slice` already performs: the wire is cut into an
insulated middle plus bare-end survivors (`build_set_insulated_range_command`,
`controller/selection/wire_edit.py`). This is physically faithful — a bare
wire with heat-shrink only in the middle *is* three conductors. **Same-wire
ranges are supported; cross-wire set-insulated is deferred** (murky semantics
across a plated-through via — matches the cross-wire flip refusal). The
"same ops everywhere" goal therefore covers per-slice set-insulated too, via
split, not just the genuinely per-fragment ops.

### 6.3 Wire-pin conversion layer — FIXED

`ConvertEndpointToWirePinCommand` / `ConvertWirePinToEndpointCommand` used to be
dispatched **only from `gui/main_window.py`** — a menu-bar slot — violating the
"tools belong to the controller; the GUI is only an interface" rule
(`memory/project_tools_belong_to_controller.md`). The dispatch now lives on
`SelectTool.convert_endpoint_to_wire_pin` / `convert_wire_pin_to_endpoint`, is
offered on the wire's context menu (`select-tool.ctx.endpoint_to_wire_pin` /
`wire_pin_to_endpoint`), and the MainWindow slots are thin delegators. It stays
**endpoint-scoped** — an endpoint op, not a `WireRange` op (the explicit
exception to the reduce-to-range direction).

### 6.4 One wire menu, conditionally fragment-aware (not two menus)

`gui.md:160-161` describes a distinct "wire-edit right-click menu". In the code
there is **one** `_ctx_wire_options` builder; it conditionally adds a single
"Flip fragment to {other} layer" row **only when `_is_editing_wire()`**
(`select_tool.py:3345-3353`). All other rows are whole-wire regardless of mode.
The doc's "two menus" framing is idealised; the reality is one menu with one
mode-gated row. This is fine as an implementation but the docs should say so.

### 6.5 Delete `[Del]` is overloaded by mode

`[Del]` deletes the *fragment* when one is armed (`_wire_edit_on_key`,
`gui_tools.py:259-261`) and the *whole wire(s)* otherwise
(`_delete_selection_or_fragment`, `select_tool.py:2545`). This is the intended
"fragment is the selection" model and is consistent — but it means the same key
has whole-wire vs. fragment scope depending on an invisible-to-newcomers mode.
The status line does surface it (`[Del] Delete` appears only after a fragment is
picked), so this is a documentation/discoverability note, not a bug.

### 6.6 Autoroute asymmetry

Autoroute exists as a *fragment* op (F2, `[A]`) and as a *group*/global op
(`F6`), but there is **no whole-wire "autoroute this entire wire"**. A user who
wants to re-route one whole jumper end-to-end must either enter edit mode and
select the full range, or delete + redraw. Minor, but it is a coverage hole in
the opposite direction from the simplify gap.

---

## 7. Recommendations (in priority order)

1. **Close the correctness gap first (§5).** Route every geometry-replacing
   wire-add (cross-wire excise/reroute, optimize, unweave, merge) through the
   same net-resolution path as a freshly drawn wire, or assert net-set
   invariance. This is the only item that can *silently corrupt the netlist*.
2. **Add fragment-scoped Simplify / Optimize (§4).** The model supports it
   (`ReplaceWireFragmentsCommand` over the fragment slice); this is the design
   doc's headline example and the biggest coverage gap. Wire it into
   `_wire_edit_on_key` as `[S]` (or a context row) alongside reroute/autoroute.
3. **Fix cross-wire flip (§6.1).** Refuse with a message instead of silently
   doing a whole-wire side move, or implement cross-wire fragment flip.
4. **Move wire-pin conversion into the tool (§6.3).** Add it to
   `_ctx_wire_options` / the controller so it is reachable from the wire's own
   context surface and obeys the layering rule; keep the MainWindow slot as a
   thin delegator. Update `gui.md` either way.
5. **Decide the granularity contract explicitly (§6.2).** Document that the
   "same ops on a fragment as on a whole wire" goal covers **per-slice**
   operations (reroute / autoroute / flip-side / delete-slice / simplify-slice),
   and that set-insulated is now a per-slice op via split
   (`build_set_insulated_range_command`), while color and wire-pin stay
   whole-wire by design. This turns an implicit inconsistency into a stated
   rule.
6. **Reconcile the docs (§6.3, §6.4).** `gui.md` describes a separate wire-edit
   menu and a right-click wire-pin flow that the code does not implement as
   described.

---

## Appendix — file map

| Concern | File |
|---|---|
| Wire / Fragment / KnotRef model | `domain/layout/wire.py` |
| Knot kinds (Pin/Joint/WirePin) | `domain/layout/knot.py` |
| Fragment value object + command builders | `controller/selection/wire_edit.py` |
| Whole-wire menu + entry | `controller/tools/select_tool.py` |
| Fragment op-table (`ArmedSubTool`) | `controller/tools/wire_edit_tool.py` |
| Op command-building core (`WireEditArm`) | `controller/editing/wire_edit_arm.py` |
| Net-restricted vertex hit-test + cycle | `controller/selection/hit_test.py` |
| Simplify input prep | `controller/selection/wire_ops.py` |
| Wire-net resolution (creation only) | `controller/editing/wire_nets.py` |
| GUI key routing into the op-table | `gui/tools/gui_tools.py` |
| Wire-pin conversion slots (layer smell) | `gui/main_window.py` |
| Menu-bar wire action ids | `gui/actions.py` |
| FlipWireFragmentCommand (via-boundary flip) | `domain/commands/component_cmds.py` |
| Electrical rules (shorts/pins/vias/orphans) | `docs/pad-pin-wire-junction-semantics.md` |
| Sub-tool delegation design intent | `docs/armed-rotate-and-subtool-delegation.md` §2.1 |
