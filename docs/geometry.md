# Geometry of Components — Placement, Rotation, Flip

## 0. The vocabulary law (`geometry.py`)

Every geometric name derives from the coordinate axes — one law across
dimensions:

| concept | 2D | 3D |
|---|---|---|
| position | `Point(x, y)` | `Point3D(x, y, z)` |
| displacement | `Vector(dx, dy)` | `Vector3D(dx, dy, dz)` |
| extents | `Size(lx, ly)` | `Size3D(lx, ly, lz)` |
| rectangle | `Rect(origin: Point, size: Size)` | — |

The words width/height/length are banned from the core model (they meant
x/y-extent in `Rect` but across-rows/z-elevation in package dims); a vector
magnitude is `norm()`. Screen vocabulary `[x, y, w, h]` exists only at the Qt
boundary, `gui/qt_geometry.py`. Package bodies carry a `Size3D` in a fixed
**package-local frame** (local +x along the lead axis, +y across it, +z
off-board — see `docs/footprints/package-model.md`) and project into the
world through `Package.body_frame()` (a `Transform2D`) — the along/across
orientation of a body falls out of the rotation matrix, never a conditional
swap.

## 1. Coordinate systems

### RasterUnit
All internal positions use `RasterUnit` (float). One unit equals one hole pitch (default 2.54 mm). Fractional and negative values are legal — they appear in sub-pitch pin offsets and negative gap margins.

### World vs. local frame
- **Local frame** — origin at the component's (0, 0), which is the footprint's pin-1 position *before any pin_offset*. Axes are aligned with a rotation-0 component on the top side.
- **World frame** — origin at board top-left corner, x right, y down.

The transformation pipeline from local to world is applied in this fixed order:

```
footprint pin position  (pin 1 = (0, 0) by definition)
  → + pin_offset        (bend adjustment in local frame; does not affect the anchor)
  → X-mirror            (only when side == "bottom")
  → clockwise rotation  (always around local (0, 0), i.e. the component origin)
  → + placement.position (world translation of the component origin)
```

`pin_world_position()` in `domain/layout/placement.py` implements this pipeline.

**Key invariant:** `placement.position` is always the world coordinate of the component's local (0, 0) — the bare footprint origin, independent of any `pin_offsets`. Rotation is always around that point. `pin_offsets` shift individual pins in local space but never affect `placement.position` or the rotation centre.

---

## 2. Footprint — relative pin positions

`FootprintTemplate` (abstract, `domain/components/footprints.py`) defines pin geometry **in local frame**. Pin 1 is always at the origin `Point(0, 0)`. All other pins are expressed as offsets from it.

| Class | Pin layout |
|---|---|
| `SingleRow(n, pitch)` | n pins in a column: pin k at `Point(0, (k-1)*pitch)` |
| `DualRow(n_per_row, pitch, rows_spacing)` | DIP numbering — pins 1..n down the left column (x=0), pins n+1..2n up the right column (x=rows_spacing) |
| `GridFootprint(rows, cols, pitch)` | Row-major grid; pin 1 at top-left |
| `TO220Footprint` | Same as `SingleRow(3)`, distinct type for wider body rendering |

Convenience aliases: `DIP(n)` → `DualRow(n//2)`, `SIL(n)` → `SingleRow(n)`, `TO92 = SIL(3)`.

---

## 3. Placement — the data structure

`ComponentPlacement` (`domain/layout/placement.py`) stores everything needed to place one component instance on the board.

| Field | Type | Meaning |
|---|---|---|
| `component_id` | UUID | Links to `ComponentInstance` |
| `position` | `Point` | World position of the **component origin** — local (0, 0), i.e. the footprint's pin-1 position *without any pin_offset*. Always the rotation centre. |
| `rotation` | `Literal[0,90,180,270]` | Clockwise degrees |
| `side` | `"top"` or `"bottom"` | PCB side |
| `pin_offsets` | `dict[int, Point]` | Per-pin bend offsets (local frame, before mirror/rotation) |
| `shared_pins` | `frozenset[int]` | Pins intentionally sharing a pad with another component |
| `label_offset` | `Point` | Refdes label offset from component centre |
| `vertical_flipped` | `bool` | Set by the flip operation (used by renderer for polarity marker) |

### Pad centres and the 0.5 convention

Board pads are rendered at the **centre** of each hole cell, which sits at `(col + 0.5, row + 0.5)` in world coordinates. Because pin 1 has no offset by default, `placement.position` normally coincides with pin 1's pad centre and must be a value of the form `n + 0.5`. Wire endpoints must also land exactly on pad centres. This convention is enforced by the snap logic in `PlaceTool` and `SelectTool`.

---

## 4. Rotation

Rotation is **clockwise** around the component origin (`placement.position`). The rotation matrix for a clockwise angle θ is:

```
x' =  x·cos(θ) + y·sin(θ)
y' = -x·sin(θ) + y·cos(θ)
```

`_rotate(p, rotation)` in `placement.py` implements this using an integer dispatch (0/90/180/270) to avoid floating-point error:

| rotation | x' | y' |
|---|---|---|
| 0 | x | y |
| 90 | y | −x |
| 180 | −x | −y |
| 270 | −y | x |

`unrotate(p, rotation)` is the inverse — equivalent to rotating by `(360 − rotation) % 360`.

Rotating a placed component in the GUI (R / Shift+R in SelectTool or PlaceTool) increments/decrements the `rotation` field; `placement.position` is never touched. All pins — including any that have been bent via `pin_offsets` — orbit around the fixed component origin. `pin_offsets` have no influence on the rotation centre.

---

## 5. Bottom-side mirror

When `side == "bottom"`, the X-axis is negated **before** rotation is applied:

```python
if placement.side == "bottom":
    effective = Point(-local.x, local.y)
```

This mirrors the footprint through the Y-axis, matching real PCB convention: when you flip a board over, left and right swap. The anchor (pin 1) stays at the same world position.

After the X-mirror the same clockwise rotation formula is applied, so the rotation semantics remain consistent regardless of side.

### Moving a component to the other side (flip)

`flip_component_side(footprint, placement)` in `placement.py` performs a
**physical flip** about an axis in the board plane — the SelectTool "Move to
other side" action (`Alt+B` / context menu). The X-mirror above reflects about
the Y-axis through **pin 1** (local `x = 0`), but a real flip rotates about the
**body centre**, so the helper toggles `side` and shifts `position` by the
mirror's offset: `min_x + max_x` of the local pin cloud (twice the centre),
signed by direction and rotated into world space by `placement.rotation`.

Pins keep their identity (no renumbering); which pad each pin occupies is a
geometric consequence:

- **Single-row (SIL, 2-pin)** — pins sit on the flip axis (`x = 0`), so the shift
  is zero and the pins do **not** move. The change is purely in body orientation
  (§11: the renderer mirrors an asymmetric silhouette's flat normal per side).
- **Multi-row (DIP-N)** — the shift is `rows_spacing`, so pin `i` lands where pin
  `N+1-i` was; the whole body settles back onto the same pads.

`rotation`, `pin_offsets` and `vertical_flipped` are untouched, and applying it
twice is the identity. A hard conflict (body overlap / different-net pad) is
rejected by the move's conflict check, leaving the component in place.

---

## 6. Pin bending (flexible leads)

For through-hole components with flexible leads (resistors, diodes, axial parts) each pin may be moved independently from the footprint's default position. This is stored in `pin_offsets: dict[int, Point]` — a vector added to the footprint pin position **in local frame** (before mirror/rotation).

`ComponentDefinition.bendable_pins: dict[int, PinBendSpec]` declares which pins are bendable and under what constraints:

- **Discrete mode** (`PinBendSpec` with `allowed_offsets`): a set of fixed allowed offset vectors (e.g., only horizontal stretches in multiples of 1.0 raster unit).
- **Free 2D mode** (`PinBendSpec` with `reach_min/reach_max`): any board pad whose world distance from pin 1's world position falls within `[reach_min, reach_max]`.

`PlaceTool` exposes a **PIN_DRAG phase** after the component is anchored: the user clicks each bendable pin's target pad in sequence. The tool calls `snap_pin_offset()` to compute the local-frame offset that would move that pin's world position to the chosen pad.

`SelectTool` offers **Ctrl+R re-drag** on an already-placed bendable component to reposition its free pins without moving pin 1.

---

## 7. Flip (Alt+R)

> This is the in-plane 2-pin flip (electrical reversal, same board side). To move
> a component to the **other board side**, see the flip-to-side helper in §5
> (`Alt+B` / "Move to other side").

Flip is defined only for 2-pin components. It swaps the two pins' local positions purely by adjusting `pin_offsets` — `placement.position` and `rotation` are never touched.

`flip_component(footprint, placement)` in `placement.py`:

1. Read the current local position of each pin: `loc_n = fp_pos_n + pin_offsets.get(n, (0,0))`.
2. Set `pin_offsets[1] = loc2 - fp_pos1` so pin 1 lands where pin 2 was.
3. Set `pin_offsets[2] = loc1 - fp_pos2` so pin 2 lands where pin 1 was.
4. Toggle `vertical_flipped`.

The component origin stays fixed; the rotation centre is unchanged. The net effect: the component appears electrically reversed (anode ↔ cathode for a diode, + ↔ − for a capacitor). The renderer uses `vertical_flipped` to draw the polarity marker on the correct end.

```
Before flip:              After flip:
  (0,0)                     (0,0)       ← placement.position unchanged
    [1]──[2]                  [2]──[1]
    off1=(0,0)                off1=(0,1)
    off2=(0,0)                off2=(0,-1)  (for SIL(2), rotation=0)
```

---

## 8. Bounding box

`component_bbox(footprint, placement, component_class)` computes the axis-aligned bounding box in world coordinates by collecting all pin world positions and inflating:

| Component class | Inflation | Rationale |
|---|---|---|
| RESISTOR, DIODE, TRANSISTOR | `_PAD_MARGIN = 0.1` per side | Flexible leads — body floats between pads |
| IC, CONNECTOR, and others | `_BODY_MARGIN = 0.5` per side | Rigid DIP/SIL body extends past outermost pins |

The bbox is used for:
- **Overlap detection** in `MoveComponentCommand._move_blocked()` and `PlaceTool`.
- **Rubber-band selection** in `SelectTool` (LTR mode requires full containment; RTL mode requires intersection).
- **Group outline rendering** in `component_renderer.py`.

---

## 9. Coordinate pipeline — worked example

**DIP-8 (U1), placed at (3.5, 4.5), rotation=90, side=top.**

Footprint (DualRow, n_per_row=4, rows_spacing=3):
- pin 1 local: (0, 0)
- pin 2 local: (0, 1)
- pin 3 local: (0, 2)
- pin 4 local: (0, 3)
- pin 5 local: (3, 3)
- pin 6 local: (3, 2)
- pin 7 local: (3, 1)
- pin 8 local: (3, 0)

Pipeline for pin 5 (local = (3, 3), no offset, side=top, rotation=90):

```
step 1: no pin_offset  → (3, 3)
step 2: side=top       → no X-mirror → (3, 3)
step 3: rotation=90    → (y, −x) = (3, −3)
step 4: + position     → (3+3.5, −3+4.5) = (6.5, 1.5)
```

Pin 5 lands at world position (6.5, 1.5), which is the centre of the hole at column 6, row 1.

---

## 10. GUI tools — geometry interaction

### PlaceTool
- **ANCHOR phase**: ghost component follows cursor; snap point = nearest board pad centre → sets `placement.position`.
- **R / Shift+R**: rotate ±90° around current anchor.
- **Alt+R**: call `flip_component()` — swaps pin_offsets only, anchor stays fixed.
- **PIN_DRAG phase**: for each bendable pin, compute `snap_pin_offset()` and store in `pin_offsets`.
- **Ctrl+R**: reset current pin's offset to (0,0).

### SelectTool
- **R / Shift+R**: issue `RotateComponentCommand(±90)` for each selected component.
- **Alt+R**: issue `RotateComponentCommand` via `flip_component()` (2-pin only).
- **Drag**: compute delta from mouse-down to mouse-up; issue `MoveComponentCommand(new_position)` for each selected component. Wire endpoints anchored to moved pins follow automatically via `_update_wires_for_move()`.
- **Ctrl+R re-drag**: freezes pin 1, re-enters PIN_DRAG for free bendable pins.

---

## 11. Rendering

`component_renderer.py` calls `pin_world_position()` for every pin of every placed component, then:

- Draws a **square** pad marker for pin 1, **circle** for all others.
- Colours pads by net (each net gets a deterministic colour from its UUID hash).
- Draws the component body via
  `body_shape_for(defn, placement, instance, package_families)`
  (`gui/renderers/body_shapes.py`) — see the dispatch below.
- Draws the refdes label at `component_bbox().center + placement.label_offset`.
- For ghost items (during placement): dashed border, 50 % opacity pins.
- `render_groups()` draws a dotted rectangle around the union bbox of all components in a group visible at the current navigation level.

**Body-shape dispatch.** A component's body comes first from its **parametric
package** (see [footprints/package-model.md](footprints/package-model.md)):
`resolve_component_package` picks the package from the instance's
`package_variant`, the definition's default `PackageOption` (both against the
`package_families` catalogue), or an inline `defn.package`. When a package
resolves it renders through `PackageBody`; otherwise the dispatch falls back to
the **footprint type** (generic bbox, or the dedicated `TO92Body` / `TO220Body`
for those packages).

**Prism silhouette — declared `kind` first.** A multi-row prism body picks its
silhouette from `silhouette_for_prism` (`gui/renderers/prism_silhouette.py`):
when the prism carries a declared `kind` (built from a package `kind:` field) the
look is **declared** — `dip_ic` gets the notch, `dip_switch` is a plain dual-row
body, `sil` a housing. Only when the kind is unknown/absent does it fall back to
`silhouette_for`, the column-count guess (1 column of ≥3 → SIL; 2 columns of ≥2 →
DIP; else plain bbox). A dev-board module suppresses the silhouette regardless
(plain PCB rectangle).

**Asymmetric body per side.** For a part with an asymmetric silhouette (a TO-92
flat face, a TO-220 tab), `PackageBody` takes the placement `side`: on the bottom
the flat-side normal is mirrored across the lead axis (`_flat_side_world` negates
the local-X component of `flat_normal`), so the body faces the correct way after a
flip to the other side (§5). Pin positions are unchanged for a single-row part —
only the drawn face flips.
