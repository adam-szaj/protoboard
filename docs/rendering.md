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

## 2. Draw order — `SceneComposer.refresh()`

`refresh()` (`gui/scene_composer.py`, owned by `BoardView` as `view.composer`;
`BoardView.refresh()` delegates) rebuilds the scene from session state. On the
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
rasterised into a pixmap and blitted by `PaintPipeline.draw_background()`
(`gui/paint_pipeline.py`, owned by `BoardView` as `view.paint`; the Qt
`drawBackground()` override just delegates). Back-side scene items (Z 0–2.999)
are also rasterised into `_back_cache`. The composite, in Z order:

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
        # precedence: instance.package_variant → defn.package_options[0]
        #             → defn.package (default body — always set)
  if pkg is not None:  return PackageBody(pkg, component_class, vertical_flipped, appearance)
  else:                return GenericBody()          # unreachable for loaded parts (package required)
```

A footprint alone is only a pin cloud (a SIL-3 fits a TO-92, a header, a
trimmer…), so without a resolved package the body is a plain bbox. A part that
wants a specific silhouette declares it via `package` / `footprints` / `kind`.
See [data-model.md](data-model.md) §4.1 for the resolution precedence and
the source-of-truth rule.

> **Note:** `TO92Body` / `TO220Body` still exist as classes but are **not** in
> this dispatch — they are exercised only by unit tests. In production, TO-92 and
> TO-220 render through the package renderers (a flat-sided cylinder and a prism
> respectively). See [footprints/package-model.md](footprints/package-model.md).

### 4.1 Renderer hierarchy — lineage dispatch (`gui/renderers/packages/`)

`PackageBody(pkg, …)` constructs a `PackageRenderer` subclass picked by
`renderer_for(pkg, component_class)` (`packages/base.py`). A
**`component_class`** registration (`CLASS_RENDERERS`) wins outright — the
class is the part author's intent for the whole part, whatever body it
carries (`DevboardRenderer` is the builtin case). Otherwise the package's
**`lineage`** — its ancestry root-first, e.g. `("cylinder", "cylinder_axial",
"DO-35")` or `("prism", "dip_ic")`, stamped by the package loaders — is
walked **leaf-first** against the `BODY_RENDERERS` registry. The most
specific registered renderer wins; an unknown name steps outward towards the
shape root, so rendering never blocks on a package or class the registry does
not know.

```
PackageRenderer            # shared: colours/appearance, placement transform,
│                          # radial template (leads + centroid), bbox fallback
├── CylinderRenderer       # renders = "cylinder": axial capsule, upright disc,
│                          # radial disc / D-shape / "-" segment
├── PrismRenderer          # renders = "prism": plain declared rect; column-guess
│                          #   ONLY for _generic (bare-footprint) bodies; SMD chip
│   ├── DipIcRenderer      # @register_renderer(kind=("dip_ic", "wdip_ic")): notched DIP
│   ├── SilHeaderRenderer  # @register_renderer(kind="sil"): rounded SIL housing
│   ├── PlainBodyRenderer  # @register_renderer(kind=("dip_switch", "pin_header",
│   │                      #   "jst")): plain rect
│   ├── SmdChipRenderer    # @register_renderer(package_name="prism_smd"): chip + caps
│   ├── DevboardRenderer   # @register_renderer(component_class="devboard"):
│   │                      #   plain PCB + pad rings (CLASS axis — wins dispatch)
│   └── TO220Renderer      # @register_renderer(family="TO-220") (plugin): + metal tab
└── DiscRenderer           # renders = "disc": edge-mounted radial lens
```

The **RADIAL** arrangement is handled once for every shape in the base
(`_maybe_radial` → `_draw_radial`: a lead stub per pad, body on the lead
centroid); the silhouette itself is the per-shape `_radial_silhouette()` hook
— bbox rounded-rect by default (prism/generic), disc / D-shape in
`CylinderRenderer`, lens in `DiscRenderer`. An axial-vertical part instead
stands over one pad (`_radial_disc` on the anchor). Everything else goes to
the subclass's `render_body()`.

**Registration is declarative**, two spellings. The preferred one is the
`@register_renderer` decorator with **typed axes** — each key says what it
is, instead of the one string-bag `renders` used to be:

```python
@register_renderer(kind="dip_ic")              # a package kind
@register_renderer(family="TO-220")            # a catalogue family id
@register_renderer(package_name="prism_smd")   # a generic package name
@register_renderer(shape_name="prism")         # a shape root
@register_renderer(component_class="led_bar")  # a component class (CLASS axis)
```

The first four are lineage axes (kinds, family ids, generic names and shape
roots all appear on a package's `lineage` — they share `BODY_RENDERERS`);
`component_class=` writes `CLASS_RENDERERS` and wins over every lineage
entry. Classes are open strings, so a library may ship
`component_class: led_bar` on a part plus a renderer plugin for it without
touching the builtin enum. Each axis takes one name or a tuple; a collision
on the same key logs a warning and the later registration wins (user code
overrides builtins). The legacy spelling — a subclass that sets
`renders = "<name>"` — still registers at class-creation time
(`__init_subclass__`) into the same lineage registry. A subclass *without*
a registration inherits behaviour but never steals its parent's slot. The
same interface exists for package-kind **generators**:
`@register_generator(kind=...)` in `domain/components/package/generators`
(legacy `generates = "<kind>"`), so a plugin registers both halves of a kind
through one consistent decorator idiom.

### 4.2 Renderer plugins (`renderers/` in a library dir)

Any component-library directory (`component_library_dirs`) may carry a
`renderers/` subdirectory. Every `*.py` in it is imported at library load and
on a preferences reload (`load_renderer_plugins`, `packages/discovery.py`);
defining a registered subclass is the entire plugin:

```python
# <library>/renderers/to220.py
from protoboard.services.rendering.packages import PrismRenderer, register_renderer

@register_renderer(family="TO-220")        # family id on the package lineage
class TO220Renderer(PrismRenderer):

    def render_body(self, positions):
        items = super().render_body(positions)
        canvas = self.axis_canvas(positions, positions[0], positions[1])
        canvas.track(items)                # z bookkeeping over the base body
        canvas.setFillColor("#dddddd")
        canvas.addRect((-1.0, 0.5), (1.0, 0.9), round=canvas.mm(0.1),
                       z=canvas.zAbove())  # a strip just above the body
        return items + canvas.items
```

`render_body` receives only the pad `positions`; everything else a draw needs
lives on **`self.paint`** — a frozen `PaintState` with the resolved `fill` /
`border`, the layer `alpha`, the base `z` and the `theme`, set by `draw()`
for the duration of the call. To re-colour the base drawing without losing
the resolved colours for your own details, wrap the `super()` call in
`self.paint_override(fill=…, border=…)`:

```python
with self.paint_override(fill="#000000"):   # base body in black
    items = super().render_body(positions)
canvas = self.body_canvas(positions)        # details in the resolved fill
```

The **reference plugin is `library/renderers/to220.py`** (the builtin library
ships it, loaded through the exact same discovery path as user plugins): the
TO-220 family renders the radial prism rectangle via `super().render_body()`
and appends the light metal mounting tab. Registering a family id covers every
variant of the family; a variant leaf name (`"TO-220/std"`) would be more
specific and win.

What a renderer sees: `self._pkg` (geometry; `pin_positions()` is the local
pin cloud — world pad positions are the `positions` argument),
`self._component_class`, `self._vertical_flipped` / `self._side` /
`self._placement` (orientation), `self._appearance` (resolved visual
override) and `self._instance` — the placed (bound) `ComponentInstance`, or
`None` in bare previews/tests. The instance is the escape hatch to part
identity: `instance.value` (e.g. a resistance for colour-band rendering),
`instance.refdes`, `instance.definition` (part name, pin names) and
`instance.resolved_package()`.

A plugin that fails to import is logged and skipped — one broken file never
blocks the rest of the library. Plugins are ordinary Python executed in the
application process (same trust level as the app itself).

### 4.2a The drawing API — `BodyCanvas`

Plugins and renderer subclasses draw through a **canvas**
(`packages/canvas.py`), not raw Qt: a `BodyCanvas` carries theme / alpha / z /
fill / border and the frame transform once, and every `add<Shape>` call
returns a finished, collected `QGraphicsPathItem`.

- **Frames** (renderer factory methods): `self.body_canvas(positions)` —
  the package-local frame (pin 1 at the origin) through the same
  local→world transform as the pins (rotation + bottom-side reflection;
  transform baked into the paths); `self.axis_canvas(positions, p1, p2)` —
  the pad-axis frame (origin at the pad centroid, +x along the p1→p2 lead
  axis; transform set on the items). Paint state comes from `self.paint`.
- **Units**: geometry inputs are raster holes by default; `canvas.mm(x)`
  converts a single millimetre value. A body authored purely from datasheet
  dimensions can instead ask for a millimetre canvas —
  `self.axis_canvas(positions, p1, p2, units="mm")` — which converts every
  coordinate, radius and path on input. Style is unit-less either way (pen
  widths stay in holes, Z is a layer index). Don't mix domains on one mm
  canvas: pad positions and `bbox()` / `lead_axis_extents()` extents are in
  HOLES and must not be fed to a `units="mm"` canvas.
- **Shapes**: `addRect(p1, p2, round=r | (tl,tr,br,bl))`, `addCircle(c, r)`,
  `addEllipse(c, rx, ry)`, `addArc(c, r, start_deg, span_deg)` (stroke-only),
  `addPath(qpainter_path)`.
- **State vs inline**: `setFillColor()` / `setBorderColor()` / `setPen()`
  set the defaults for subsequent calls; every `add<Shape>` accepts the same
  as per-call kwargs (`fill=`, `border=`, `pen=`, `pen_width=`).
- **Z stacking**: `zAbove()` / `zBelow()` return a Z one `Z_STEP` above/below
  everything drawn so far; `canvas.track(items)` includes the base class's
  items in that bookkeeping, so a detail lands just above the body
  (`z=canvas.zAbove()`), matching the historical `z + 0.005` idiom.

The built-in prism/radial/bbox/SMD paths draw through the same canvas, so a
plugin's output composes with them pixel-identically. `SmdChipRenderer`
(`prism_kinds.py`, `@register_renderer(package_name="prism_smd")`) is the
in-tree example of the "base outline + per-type detail" pattern: the base
chip rectangle plus the metallised terminal caps.

### 4.3 Prism silhouette — declared `kind` renderers first

`_draw_prism` gets its outline from the renderer's overridable
`_silhouette(pkg)` hook. The **declared look** lives in the per-kind renderer
subclasses (`gui/renderers/packages/prism_kinds.py`), registered for their
kind names and picked by the ordinary lineage dispatch:

```
lineage dispatch (renderer_for):
  dip_ic / wdip_ic     → DipIcRenderer      → notched DIP body + pin-1 marker
  sil                  → SilHeaderRenderer  → rounded housing
  dip_switch / pin_header / jst → PlainBodyRenderer → plain rect (colour per kind)
  <no renderer for the kind>    → PrismRenderer     → PLAIN bbox rectangle
      # declared beats guessed: an explicit package renders as written
  <_generic (bare footprint:)>  → PrismRenderer     → silhouette_for (column GUESS)
      # 1 column ≥3 pins → SIL housing; 2 columns ≥2 each → DIP notch; else plain bbox
```

**Declared beats guessed** (decided 2026-07-04, after an explicit LED-bar
prism was column-guessed into a DIP-20 notch): a package the author declared
— an inline `package:` block, an authored family variant, a declared `kind`
with no registered renderer — renders as exactly what was written (the plain
bbox rectangle) unless a lineage-registered renderer supplies its declared
look. The pin-column guess (`silhouette_for`) applies ONLY to the synthesized
`_generic` body of a bare `footprint:` part, where nothing was declared. The
generated `dip-N` / `wdip-N` / `sil-N` catalogue bodies carry their kind on
the lineage, so their notch/housing is a declared look, not a guess.
Rendering never blocks on a kind it does not recognise. A dev-board module
suppresses the silhouette entirely (plain PCB rectangle + pad-active rings)
via `DevboardRenderer`, registered on the **component-class axis**
(`@register_renderer(component_class="devboard")`), not by lineage: it is a
component-*class* trait that must hold for any package a devboard carries —
the class axis exists precisely for such traits (this replaced the historical
hardcoded `ComponentClass.DEVBOARD` gates in `_draw_prism` / `_draw_bbox`).

### 4.4 Appearance

Fill colour and corner radius are a visual-only override resolved per field,
instance winning over definition: `Appearance.resolve(instance.appearance,
defn.appearance)` (`body_shapes.py:1153`). Geometry / mask / DRC / routing are
unaffected.

### 4.5 Body-printed value

Every `PackageRenderer.draw()` prints the instance's value on the body after
`render_body()`, through `_maybe_value_label(positions)`
(`packages/base.py`) — one shared code path so a new renderer gets the label
for free:

```
draw():
  items = render_body(positions)
  label = _maybe_value_label(positions)   # None → nothing appended
  if label is not None: items.append(label)
```

- **What text** — `body_print_text(component_class, value)`
  (`domain/components/value_text.py`, pure, Qt-free). Resistors, inductors and
  capacitors print a **coded** marking (`parse_value` + `smd_code`, e.g.
  `22k` → `"223"`); capacitance goes through a new pF parser
  (`parse_capacitance`) into the same EIA 3-digit code. Every other class
  prints the **raw** `value` string — the part number for a concrete part
  like `BC547`. A coded class whose value doesn't parse still falls back to
  the raw string rather than printing nothing. An unset value (`""` or `"?"`)
  prints nothing.
- **What ink** — `best_ink(body_color)` (`gui/renderers/text_contrast.py`).
  WCAG relative luminance of the resolved body fill picks a **palette**
  first (luminance > 0.5 → `DARK_INKS` {black, graphite, navy}, else
  `LIGHT_INKS` {white, light-grey, yellow}), then the highest WCAG contrast
  ratio picks the **ink** within that palette. The two-stage rule is
  correctness-by-construction: a dark body can never receive a dark ink
  regardless of the contrast arithmetic.
- **How it's laid out** — `make_body_label(text, ink, body_transform,
  local_center, local_angle_deg, avail_len, avail_w, max_height=, alpha=, z=)`
  (`gui/renderers/packages/body_label.py`) builds a `QGraphicsSimpleTextItem`
  in the body's **local** frame — centred on the local bbox, rotated to the
  pin-row direction — then maps it through `body_transform`, the *same*
  `_body_placement` transform every other body element uses. Because the
  label is pinned to that transform rather than rebuilt around a world point,
  it inherits the body's rotation **and** flip and stays fixed relative to
  the body geometry, not the live pin positions — safe under bendable-pin
  re-drag. Two readability compensations ride on top of the transform: a
  mirrored (flipped) body gets a counter-scale on the glyphs so text never
  reads backwards, and a world orientation that would land upside-down gets a
  180° turn so it always reads upright. `_maybe_value_label`
  (`packages/base.py`) feeds it the body bbox centre at 90% of body length ×
  75% of body width, and the direction from `_pin_row_angle_deg()` — the
  pin-1→pin-2 heading in the local frame, since packages don't agree on which
  local axis the pin row runs along (a TO-92's row is local +x, a DIP's is
  +y). The label sits one `BodyCanvas.Z_STEP` triple above the base `z`.
  Returns `None` if the text is empty or the box has no area.
- **Size cap** — scale-to-fit is geometric (the text item keeps its default
  point size and is scaled to the available box, so it never hits the tiny
  point-size range that hangs the offscreen Qt backend). For generic bodies
  `_maybe_value_label` bounds that scale by `theme.body_print_max_height`
  (default 1.0 raster unit): scale-to-fit alone would balloon a short code
  like a resistor's `"223"` to fill a big body; the cap stops that while
  leaving long text free to shrink to the box as before. The SMD chip's
  between-caps label (below) passes `max_height=None` — uncapped — since its
  available box is already tightly bounded by the cap markers.
- **Opt-outs** — a renderer that already shows the value some other way
  overrides `_maybe_value_label` to return `None`: `SmdChipRenderer`
  (`prism_kinds.py`, the between-caps code label, drawn via its own
  `_code_label` → `make_body_label` call with `max_height=None`) and
  `DinColorBandRenderer` (`library/renderers/resistor_bands.py`, colour
  bands). This is a by-convention override, not a structural guard — see
  `docs/todo.md` for the follow-up if a future value-drawing renderer forgets
  it.
- **Fallback frame (no placement)** — a bare `draw()` call with no
  `ComponentPlacement` (unit tests, bbox previews) has no `placement_transform`
  to project through, so `_body_placement` falls back to a translate-only
  transform onto pin 1. This matches the *linear* part of the production
  transform at rotation 0 (identity) — it must not also rotate by the
  pin1→pin2 heading, because every local-frame geometry builder (`Prism.bbox()`,
  `Cylinder.bbox()`, the radial silhouette anchor) already self-orients from
  the local pin cloud; adding a heading-based rotation on top would
  double-rotate packages whose local pin row runs along +y (a DIP).

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

**Highlights** (applied after the main passes, `gui/highlight/renderer.py` —
`HighlightRenderer`, owned by `BoardView` as `view.highlight`) — selecting a
wire/net dims everything else by a highlight factor (`HighlightRenderer.apply`)
and draws bright overlays: the selected-wire thick overlay at `Z_WIRE_HIGHLIGHT`
(9.4) and bright net-pad discs at `Z_NET_PAD_HIGHLIGHT` (9.4), both below the
pin-modifier glyph so pin markers on the route stay visible. The
selection/highlight FSM is in [highlight_and_selection.md](highlight_and_selection.md)
and [highlight-state-machine.txt](highlight-state-machine.txt).
