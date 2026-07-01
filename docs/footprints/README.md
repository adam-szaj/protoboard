# Footprint gallery

> To **author** components and packages (library YAML — fields, generics, colour
> customisation, dev-board modules) see [authoring.md](authoring.md); for the
> design rationale (lead/offset model) see [package-model.md](package-model.md). This file is the
> rendered gallery. For the full render chain (draw order, Z-stack, body
> dispatch) see [../rendering.md](../rendering.md).

Every entry below is rendered headless via `tools/render_footprints.py`. Re-generate with:

```bash
uv run python tools/render_footprints.py
```

## How footprints map to body shapes

`body_shape_for(defn, placement, instance, package_families)` in `gui/renderers/body_shapes.py` is the dispatcher — a **two-branch** decision (see [../rendering.md](../rendering.md) §4 for the full chain). A component's body comes from its **parametric package** (`docs/footprints/package-model.md`): `resolve_component_package` picks the package from the instance's `package_variant`, the definition's `package_family_ref`, or an inline `defn.package` (DIP / SIL parts get a *generated* `dip-N` / `sil-N` family variant). When a package resolves it renders through `PackageBody`; otherwise a bare footprint is only a pin cloud and draws a `GenericBody` plain bbox.

| Source | Body shape |
|---|---|
| any resolved `Package` (cylinder / prism / disc) — incl. TO-92, TO-220, DIP, SIL | `PackageBody` — geometry + colour from the package |
| no package resolves (bare footprint / pin cloud) | `GenericBody` (plain bbox) |

> There are **no** dedicated `TO92Body` / `TO220Body` / `Svg*Body` dispatch
> targets: TO-92 (flat-sided cylinder), TO-220 (prism), DIP and SIL all render
> through `PackageBody`. The `TO92Body` / `TO220Body` classes survive only in
> unit tests; the `Svg*Body` renderers were removed.

`PackageBody` dispatches on the package's `arrangement` × shape:

- **AXIAL cylinder, horizontal** (pads farther apart than the body length): a side-view capsule lying *between* the two pads, leads to each end.
- **AXIAL cylinder, vertical / upright** (pads closer than the body length — a part stood on end, one lead bent down): the body is a top-view disc sitting **directly over ONE pad**, with a single lead stub reaching the other. *Which* pad it stands on is the **Flip** (`vertical_flipped`): unflipped → over pin 1, flipped → over pin 2. The orientation is chosen automatically by the placed pad spacing — there is no separate toggle. (This is **not** the radial centroid silhouette below; an axial part mounts over a single pad.)
- **RADIAL part** (electrolytic, TO-92, disc cap): a top-down silhouette (disc / circle / rect) centred per its leads — the centroid of the pads — with a lead stub to each. The body sits *between* the pads as its leads dictate, not over one pad.

The body colour comes from the package (`color`); polarity (diode cathode stripe, polar-cap disc) is driven by `component_class` and `vertical_flipped` (the `−` marker always tracks the physical cathode, pin 2, independent of the Flip anchor), not the package. Prisms / discs honour `edge` (SHARP vs ROUNDED) and `disc_profile` (FLAT vs CANDY).

## Conventions in these images

- Green dot = pin centre.
- Yellow square on the pin-1 pad = pin-1 marker (same as in the GUI).
- Tiny red cross-hair = component origin (`placement.position`, i.e. local (0, 0) — always pin 1 in this gallery).
- For axial 2-pin parts (resistor / diode / cap / inductor / fuse) the left panel is the typical *horizontal* mount and the right panel is the *vertical* (close-mount) variant. Vertical mode is triggered by setting `pin_offsets[2]` so pin 2 sits next to pin 1.

## Index

| Slug | Title | Footprint | Class |
|---|---|---|---|
| [`resistor_axial`](#resistor_axial) | Resistor — axial 2-pin | `SingleRow` | `resistor` |
| [`capacitor_nonpolar`](#capacitor_nonpolar) | Capacitor — non-polar (axial) | `SingleRow` | `capacitor_nonpolar` |
| [`capacitor_polar`](#capacitor_polar) | Capacitor — polar (electrolytic) | `SingleRow` | `capacitor_polar` |
| [`diode_axial`](#diode_axial) | Diode — axial (1N4148 / 1N4007 …) | `SingleRow` | `diode` |
| [`diode_led`](#diode_led) | LED | `SingleRow` | `diode_led` |
| [`inductor_axial`](#inductor_axial) | Inductor — axial | `SingleRow` | `inductor` |
| [`fuse_axial`](#fuse_axial) | Fuse — axial | `SingleRow` | `fuse` |
| [`transistor_to92`](#transistor_to92) | Transistor — TO-92 (BC547 …) | `SingleRow` | `transistor_npn` |
| [`transistor_to220`](#transistor_to220) | Transistor / regulator — TO-220 | `TO220Footprint` | `transistor_nmos` |
| [`sil_pin_header_4`](#sil_pin_header_4) | Pin header — SIL × 4 | `SingleRow` | `connector` |
| [`sil_pin_header_8`](#sil_pin_header_8) | Pin header — SIL × 8 | `SingleRow` | `connector` |
| [`resistor_network_sil5`](#resistor_network_sil5) | Resistor network — SIL × 5 | `SingleRow` | `resistor` |
| [`dip8`](#dip8) | DIP-8 (LM358, NE555 …) | `DualRow` | `ic` |
| [`dip14`](#dip14) | DIP-14 (74HC04 …) | `DualRow` | `ic` |
| [`dip16`](#dip16) | DIP-16 | `DualRow` | `ic` |
| [`dip28_wide`](#dip28_wide) | DIP-28 wide (ATmega328 …) | `DualRow` | `ic` |
| [`quad_row_module`](#quad_row_module) | Quad in-line module (CoreBoard-style) | `QuadRow` | `ic` |
| [`grid_3x4`](#grid_3x4) | Grid footprint 3 × 4 | `GridFootprint` | `mechanical` |

## Entries

### resistor_axial

**Resistor — axial 2-pin**

`TwoPin` footprint, `component_class=resistor`. Renders via `TwoPinParametricBody` from the `resistor_axial` family. Pin 2 is bendable (reach 1..23 raster units).

![resistor_axial](resistor_axial.png)

### capacitor_nonpolar

**Capacitor — non-polar (axial)**

`TwoPin` footprint, `component_class=capacitor_nonpolar`. Axial cylinder body without polarity marker.

![capacitor_nonpolar](capacitor_nonpolar.png)

### capacitor_polar

**Capacitor — polar (electrolytic)**

`SingleRow(n=2)`, `component_class=capacitor_polar`. Cylinder body with `−` stripe on the cathode side; pin 1 = +, pin 2 = −.

![capacitor_polar](capacitor_polar.png)

### diode_axial

**Diode — axial (1N4148 / 1N4007 …)**

`SingleRow(n=2)`, `component_class=diode`. Cylinder body with cathode stripe; pin 1 = anode, pin 2 = cathode.

![diode_axial](diode_axial.png)

### diode_led

**LED**

`SingleRow(n=2)`, `component_class=diode_led`. Same cylinder body family; renderer treats it as polar.

![diode_led](diode_led.png)

### inductor_axial

**Inductor — axial**

`SingleRow(n=2)`, `component_class=inductor`. Cylinder body, no polarity.

![inductor_axial](inductor_axial.png)

### fuse_axial

**Fuse — axial**

`SingleRow(n=2)`, `component_class=fuse`. Resistor cylinder family.

![fuse_axial](fuse_axial.png)

### transistor_to92

**Transistor — TO-92 (BC547 …)**

`SingleRow(n=3)`, `component_class=transistor_npn`. Rendered through `PackageBody` (a flat-sided cylinder, RADIAL arrangement). Half-circle, three legs.

![transistor_to92](transistor_to92.png)

### transistor_to220

**Transistor / regulator — TO-220**

`TO220Footprint(n=3)`. Wider tab body.

![transistor_to220](transistor_to220.png)

### sil_pin_header_4

**Pin header — SIL × 4**

`SingleRow(n=4)`, `component_class=connector`. Generic SIL body (no axial / TO-92 dispatch).

![sil_pin_header_4](sil_pin_header_4.png)

### sil_pin_header_8

**Pin header — SIL × 8**

`SingleRow(n=8)`, longer SIL strip.

![sil_pin_header_8](sil_pin_header_8.png)

### resistor_network_sil5

**Resistor network — SIL × 5**

`SingleRow(n=5)`, `component_class=resistor`. Bus pin 1 (common) + four resistors. Renders as a generic SIL.

![resistor_network_sil5](resistor_network_sil5.png)

### dip8

**DIP-8 (LM358, NE555 …)**

`DualRow(n_per_row=4, rows_spacing=3.0)`, resolving the generated `dip-8` family. Rendered through `PackageBody` → prism → the `dip_ic` silhouette (notch + pin-1 marker).

![dip8](dip8.png)

### dip14

**DIP-14 (74HC04 …)**

`DualRow(n_per_row=7, rows_spacing=3.0)`.

![dip14](dip14.png)

### dip16

**DIP-16**

`DualRow(n_per_row=8, rows_spacing=3.0)`.

![dip16](dip16.png)

### dip28_wide

**DIP-28 wide (ATmega328 …)**

`DualRow(n_per_row=14, rows_spacing=6.0)`. Wide-body DIP draws a wider rectangle.

![dip28_wide](dip28_wide.png)

### quad_row_module

**Quad in-line module (CoreBoard-style)**

`QuadRow(n_per_col=10, col_spacings=(1, 8, 1))`. Four parallel pin columns; outer pads connect via flying leads, inner pads expose module signals. Inner-column pin numbers are omitted in this gallery render — they would overlap the body or the outer-column labels. Inner pins follow the spec: left-inner is `n+1..2n`, right-inner is `2n+1..3n` (numbered bottom-to-top).

![quad_row_module](quad_row_module.png)

### grid_3x4

**Grid footprint 3 × 4**

`GridFootprint(rows=3, cols=4)`. Generic body; used for tactile switches, headers, and one-off mechanical parts.

![grid_3x4](grid_3x4.png)
