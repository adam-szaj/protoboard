<!-- SPDX-License-Identifier: MIT -->
# Rendering — the draw chain, layers, and body dispatch

How the board view turns session state into pixels: what is drawn, in what
order, on which Z-layers, under which conditions, and how each component's body
shape is chosen. The single source of truth in code is `gui/layer_state.py`
(the layer model) and `BoardView.refresh()` / `BoardView.drawBackground()`
(`gui/board_view.py`, the orchestrator). This document mirrors them.

Related: [gui.md](gui.md) (window layout, tools, shortcuts), [geometry.md](geometry.md)
§11 (coordinate pipeline into rendering), [footprints/package-model.md](footprints/package-model.md)
§6-7 (the `kind` / silhouette geometry the renderer draws).

---

## 1. The layer model (`LayerState`)

`LayerState` is the single context object built once per `refresh()` from the
camera side, the active tool, and the manual layer overrides. Every renderer
reads its Z and alpha from it — no renderer hard-codes a Z. Built by
`layer_state_for_tool(tool, camera_side, view_transform, overrides, theme)`
(`layer_state.py`).

Three inputs drive it:

- **`camera_side`** (`"top"` | `"bottom"`) — which physical side the camera
  points at. Camera-side elements render **in front**, the opposite side
  **behind**. Changing camera side also changes the active editing side.
- **`view_transform`** (`normal` | `mirror_h` | `mirror_v` | `mirror_hv`) — a
  purely visual flip of the scene, independent of camera side. Combined with the
  two camera sides this gives **6 distinct views** (used by the multi-panel PDF
  export template).
- **`overrides`** (`LayerOverrides`) — per-layer manual visibility set from the
  View → Layers panel (below).

### 1.1 The Z-stack (`layer_state.py:47-74`)

Back → front. A component/wire/label's Z depends on its **side** relative to the
camera (front band vs back band); the fixed anchors are:

| Z    | Layer                | Notes |
|------|----------------------|-------|
| 0.0  | `Z_BACK_LABELS`      | back-side labels — under the board when seen from the other side |
| 1.0  | `Z_BACK_COMPONENTS`  | back-side component bodies |
| 2.0  | `Z_BACK_WIRES`       | back-side wire fragments |
| 3.0  | `Z_BOARD`            | board pads / strips / frames (rasterised, not scene items — §3) |
| 3.5  | `Z_PIN_PADS`         | schematic-aware pad overlay (net-coloured discs) — toggle key `9` |
| 4.0  | `Z_FRONT_WIRES`      | camera-side wire fragments |
| 5.0  | `Z_FRONT_COMPONENTS` | camera-side component bodies |
| 6.0  | `Z_FRONT_LABELS`     | (legacy anchor; front labels actually lift to `Z_LABELS_TOP`) |
| 7.0  | `Z_OVERLAY`          | ratsnest, group outlines, axis labels |
| 9.4  | `Z_NET_PAD_HIGHLIGHT` | bright net-pad discs when a wire/net is selected |
| 9.4  | `Z_WIRE_HIGHLIGHT`   | thick overlay along a selected wire's path |
| 9.5  | `Z_PIN_MODIFIERS`    | top pin-modifier glyphs (pin1 / exported / star) — toggle key `0` |
| 9.7  | `Z_LABELS_TOP`       | front-side refdes / value / pin names / group names |
| 10.0 | `Z_SELECTION`        | selection handles / wire-edit node handles |

The side split is resolved by `LayerState.z_for_wire/z_for_component/z_for_label`
(`layer_state.py:180-190`): camera-side → the front anchor, opposite side → the
back anchor. So a "top wire" is at Z 4.0 when the camera looks at the top and at
Z 2.0 when it looks at the bottom.

### 1.2 Per-layer visibility (`LayerOverrides`)

The Layers panel (`gui/panels/layers_panel.py`) exposes ten toggleable layers,
each cycling through three states (`LayerVisibility`): **FULL** (green, follows
tool-driven ghosting), **GHOST** (amber, ~25 %), **HIDDEN** (grey, 0 %). Number
keys `1`–`0` toggle them:

| Key | Layer               | Key | Layer            |
|-----|---------------------|-----|------------------|
| `1` | Board               | `6` | Labels           |
| `2` | Components Top      | `7` | Pin Names        |
| `3` | Components Bottom   | `8` | Connections (ratsnest) |
| `4` | Wires Top          | `9` | Pin Pads         |
| `5` | Wires Bottom       | `0` | Pin Modifiers    |

### 1.3 Alpha resolution

Final alpha per layer = **override wins, else the tool-driven value**
(`LayerState._vis_alpha`, `layer_state.py:196-203`): GHOST → `ghost_comp_alpha`,
HIDDEN → 0, FULL → the tool alpha. The tool alpha encodes back-side dimming and
place/route ghosting:

- **`alpha_for_component(side)`** — opposite side → `back_alpha` (0.30);
  camera side → `ghost_comp_alpha` (0.25) while a tool sets `ghost_components`
  (Place/Route preview), else 1.0.
- **`alpha_for_wire(side)`** — camera side 1.0, opposite side `back_alpha`.
- **`alpha_for_board()`** — `board_ghost_alpha` (0.55) while ghosting, else 1.0.
- **`alpha_for_pin_pads/pin_modifiers/pin_names/label(side)`** — the *side-only*
  tool alpha (`_side_tool_alpha`: front 1.0, back `back_alpha`), so hiding the
  components layer does **not** hide labels or pin names next to them.

Default alpha constants live in `layer_state.py:81-83`; the active `Theme` may
override them.

---

## 2. Draw order — `BoardView.refresh()`

`refresh()` (`board_view.py:1663`) rebuilds the scene from session state. On the
warm path only changed component/wire groups are recreated (fingerprint reuse);
the board layer rebuilds only when its visual inputs change. The order, top of
`refresh()` down:

1. **Board layer** (`:1687`) — invalidate the board pixmap cache when
   geometry/theme/alpha change (the board is a blitted pixmap, not scene items —
   §3). A placement-only change just drops the pad-net-colour map so it rebuilds.
2. **Shared render inputs** (`:1733`) — the wire→net map, the resolved
   `net_color_map` (manual `net.color` → static palette → golden-ratio hue
   spin; the *same* map drives wire colour **and** pad colour so they match), and
   the set of `routed` net ids.
3. **Wires** (`:1748`) — `update_wire_groups` (group reuse). Drawn **before**
   components. Reads Z/alpha from `layers`, plus `highlighted_net_id` and
   `routing_target_net_id`.
4. **Wire-pin glyphs** (`:1762`) — `render_wire_pins` (virtual gold-pin knots);
   cleared and rebuilt each refresh (tiny set).
5. **Components + labels** (`:1789`) — `update_component_groups` (group reuse).
   Schematic-aware (`routed_nets`, `highlighted_net_id`); label groups lifted to
   `Z_LABELS_TOP`.
6. **Pin-pads overlay** (`:1822`) — `classify_pin_pads` → `render_pin_pads` at
   `Z_PIN_PADS`. Guarded (`alpha_for_pin_pads(side) > 0`) and wrapped in a
   defensive `try` so a classification glitch never crashes the paint. Records
   are cached for the highlight pass.
7. **Overlays** (`:1859`) — `render_ratsnest` (unless HIDDEN; GHOST → only
   unrouted nets), `render_groups` (group outlines), `render_axis_labels` — all
   at `Z_OVERLAY`, always recreated (few items).

Selection / highlight overlays are applied on top of this by the highlight pass
(§5), at Z 9.4–10.0.

---

## 3. The board "sandwich" — `drawBackground()`

The board itself (pads, strips, frames) is **not** scene items — it is
rasterised into a pixmap and blitted in `drawBackground()` (`board_view.py`,
docstring at `:215`). Back-side scene items (Z 0–2.999) are also rasterised into
`_back_cache`. The composite, in Z order:

```
1. fillRect(viewport background)
2. board_base  — board pixmap, alpha = alpha_for_board()          (bottom)
3. _back_cache — back-side items in one blit                       (Z 0..2.999)
4. back-filter — half-transparent wash, alpha = alpha_for_back_filter()  (0.10)
5. board_mid   — SAME board pixmap again, alpha = alpha_for_board_mid()  (0.10)
6. [scene paint pass] front-side items at their own Z              (Z 3.001+)
```

Why twice: the **mid** blit lets front-side pads "peek through" so components
visually rest on the board, while staying transparent enough that back-side
items underneath stay readable. The **back-filter** darkens the back side
independently of camera ghosting, so front and back stay distinguishable even at
high opacity. Both the mid blit and the filter vanish in board GHOST/HIDDEN so
the user can study routing without pad clutter (`alpha_for_board_mid` /
`alpha_for_back_filter`, `layer_state.py:240-267`).

This back-cache + Z-banding is the performance architecture that cut the refresh
budget from ~700–900 ms to ~85 ms (see [performance.md](performance.md),
[benchmarks.md](benchmarks.md)).

---

## 4. Choosing a component body — `body_shape_for` + the renderer registry

The decision "how do I draw this component?" is `body_shape_for(defn, placement,
instance, package_families)` (`gui/renderers/body_shapes.py`). It is
deliberately a **two-branch** dispatch:

```
body_shape_for:
  pkg = resolve_component_package(defn, instance, package_families)
        # precedence: instance.package_variant → defn.package_family_ref
        #             → inline defn.package → defn.footprint (topology default)
  if pkg is not None:  return PackageBody(pkg, component_class, vertical_flipped, appearance)
  else:                return GenericBody()          # bare bbox — a footprint is only a pin cloud
```

A footprint alone is only a pin cloud (a SIL-3 fits a TO-92, a header, a
trimmer…), so without a resolved package the body is a plain bbox. A part that
wants a specific silhouette declares it via `package` / `package_family_ref` /
`kind`. See [data-model.md](data-model.md) §4.1 for the resolution precedence and
the source-of-truth rule.

> **Note:** `TO92Body` / `TO220Body` still exist as classes but are **not** in
> this dispatch — they are exercised only by unit tests. In production, TO-92 and
> TO-220 render through the package renderers (a flat-sided cylinder and a prism
> respectively). See [footprints/package-model.md](footprints/package-model.md).

### 4.1 Renderer hierarchy — lineage dispatch (`gui/renderers/packages/`)

`PackageBody(pkg, …)` constructs a `PackageRenderer` subclass picked by
`renderer_for(pkg)` (`packages/base.py`): the package's **`lineage`** — its
ancestry root-first, e.g. `("cylinder", "cylinder_axial", "DO-35")` or
`("prism", "dip_ic")`, stamped by the package loaders — is walked **leaf-first**
against the `BODY_RENDERERS` registry. The most specific registered renderer
wins; an unknown name steps outward towards the shape root, so rendering never
blocks on a package the registry does not know.

```
PackageRenderer            # shared: colours/appearance, placement transform,
│                          # radial top-view machinery, bbox fallback
├── CylinderRenderer       # renders = "cylinder": axial capsule, upright disc
├── PrismRenderer          # renders = "prism": DIP/SIL silhouette, SMD chip
└── DiscRenderer           # renders = "disc": radial lens (base radial branch)
```

`PackageRenderer.draw()` handles the **RADIAL** arrangement once for every
shape (top-down silhouette on the lead centroid, a lead stub per pad; an
axial-vertical part instead stands over one pad — `_radial_disc`); everything
else goes to the subclass's `render_body()`.

**Registration is declarative:** a subclass that sets `renders = "<name>"`
enters the registry at class-creation time (`__init_subclass__`); later
registrations win, so user code overrides builtins. A subclass *without* its
own `renders` inherits behaviour but never steals its parent's slot.

### 4.2 Renderer plugins (`renderers/` in a library dir)

Any component-library directory (`component_library_dirs`) may carry a
`renderers/` subdirectory. Every `*.py` in it is imported at library load and
on a preferences reload (`load_renderer_plugins`, `packages/discovery.py`);
defining a registered subclass is the entire plugin:

```python
# <library>/renderers/to220.py
from protoboard.gui.renderers.packages import PrismRenderer

class TO220Renderer(PrismRenderer):
    renders = "TO-220"                     # family id on the package lineage

    def render_body(self, positions, fill, border, alpha, z, theme):
        items = super().render_body(positions, fill, border, alpha, z, theme)
        items.append(self._metal_tab(...))  # extra geometry on top of the prism
        return items
```

The **reference plugin is `library/renderers/to220.py`** (the builtin library
ships it, loaded through the exact same discovery path as user plugins): the
TO-220 family renders the radial prism rectangle via `super().render_body()`
and appends the light metal mounting tab. Registering a family id covers every
variant of the family; a variant leaf name (`"TO-220/std"`) would be more
specific and win.

A plugin that fails to import is logged and skipped — one broken file never
blocks the rest of the library. Plugins are ordinary Python executed in the
application process (same trust level as the app itself).

### 4.3 Prism silhouette — declared `kind` first

`_draw_prism` gets its outline from `silhouette_for_prism(prism)`
(`gui/renderers/prism_silhouette.py:210`):

```
silhouette_for_prism:
  if prism.kind in _KIND_SILHOUETTE:   return that silhouette   # DECLARED
      # dip_ic / wdip_ic → notch; dip_switch / pin_header / jst → plain; sil → housing
  else:                                return silhouette_for(prism)  # column GUESS
      # 1 column ≥3 pins → SIL housing; 2 columns ≥2 each → DIP notch; else plain bbox
```

The declared `kind` (built from a package `kind:` field) wins so a 2-row relay
is not forced into a DIP notch just because its pins cluster into two columns; an
unknown/absent kind falls back to the column guess so rendering never blocks on a
kind it does not recognise. A dev-board module suppresses the silhouette entirely
(plain PCB rectangle).

### 4.4 Appearance

Fill colour and corner radius are a visual-only override resolved per field,
instance winning over definition: `Appearance.resolve(instance.appearance,
defn.appearance)` (`body_shapes.py:1153`). Geometry / mask / DRC / routing are
unaffected.

---

## 5. Pin-pads, wires, and highlights

**Pin-pads** (`gui/renderers/pin_pad_renderer.py`) — a schematic-aware overlay
over the board pads. Each pad carrying a net is repainted in the net colour at
`Z_PIN_PADS` (3.5), with the modifier shape (pin1 square / exported diamond /
star) carved out just above it; `CONTACT_ONLY` pads draw an outline ring
instead. The routing-target net gets neon yellow. The top **pin-modifier** glyph
repeats at `Z_PIN_MODIFIERS` (9.5) so the marker stays visible over wires and
bodies. Classification is `PinPadClass` (`domain/layout/pin_pad_classification.py`);
the semantics (which pads may carry wires) are in
[pad-pin-wire-junction-semantics.md](pad-pin-wire-junction-semantics.md).

**Wires** (`gui/renderers/wire_renderer.py`) — one scene group per wire, reused
across refreshes by fingerprint. Z and alpha are **per fragment** (`z_for_wire` /
`alpha_for_wire` on the fragment's side), so a wire that crosses to the other
board side through a plated-through pad has fragments on both Z bands. Each
fragment paints a glow + outline below the core; via markers mark the
side-transition boundaries.

**Highlights** (applied after the main passes, `board_view.py`) — selecting a
wire/net dims everything else by a highlight factor (`_apply_highlight_opacity`)
and draws bright overlays: the selected-wire thick overlay at `Z_WIRE_HIGHLIGHT`
(9.4) and bright net-pad discs at `Z_NET_PAD_HIGHLIGHT` (9.4), both below the
pin-modifier glyph so pin markers on the route stay visible. The
selection/highlight FSM is in [highlight_and_selection.md](highlight_and_selection.md)
and [highlight-state-machine.txt](highlight-state-machine.txt).
