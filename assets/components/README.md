# Component SVG Templates

Each file defines a component package in **raster units** (1 unit = 1 pad pitch = 2.54 mm).

## Coordinate Convention

- **Pin 1 is always at `(0, 0)`.**
- Other pins follow the footprint's `pin_positions()` layout.
- The renderer applies `QTransform.translate(pin1_world_x, pin1_world_y).rotate(angle_deg)`
  to the whole `QGraphicsSvgItem` — the SVG never encodes world position.

## Well-Known Element IDs

| id | Type | Purpose |
|----|------|---------|
| `body` | any | Main package outline — renderer sets `fill` and `stroke` |
| `lead-N` | `<line>` | Lead wire from pin N to body — renderer adjusts `x2`/`y2` for pin_offsets |
| `pin-N` | `<circle>` | Pin N pad marker — renderer sets `fill` from net colour |
| `pin1-marker` | `<rect>` | Square marker at pin 1 — renderer sets `fill` from net colour |
| `cathode-bar` | `<rect>` | Cathode end stripe (diode) |
| `polarity-stripe` | `<rect>` | Polarity marker (polar capacitor) — hide for non-polar |
| `notch` | path subpath | DIP orientation notch (via even-odd fill-rule) |
| `slot-N` | `<rect>` | Per-pin slot in SIL housing |

## `data-*` Attributes on `<svg>`

Used by the renderer to understand parametric geometry without parsing paths:

| attribute | meaning |
|-----------|---------|
| `data-pin-count` | total number of pins |
| `data-pin1` | `"x,y"` of pin 1 (always `"0,0"`) |
| `data-pin2` | `"x,y"` of pin 2 in local frame |
| `data-lead-attach-y1` | Y where lead-1 meets the body |
| `data-lead-attach-y2` | Y where lead-2 meets the body |
| `data-body-cx`, `data-body-cy` | body centre (radial types) |
| `data-n-per-row` | DIP: pins per row |
| `data-rows-spacing` | DIP: spacing between rows |

## Renderer Algorithm (pseudocode)

```python
def render_svg_component(defn, placement, positions, fill, border, alpha, theme):
    # 1. Load template SVG
    template_path = ASSETS / f"{package_type(defn)}.svg"
    tree = ET.parse(template_path)
    root = tree.getroot()

    # 2. Adjust lead lengths from actual pin_offsets
    p1, p2 = positions[0], positions[1]
    dist = hypot(p2.x - p1.x, p2.y - p1.y)
    body_len = dist * theme.axial_body_length_ratio
    attach1 = (dist - body_len) / 2
    attach2 = dist - attach1
    root.find(".//*[@id='lead-1']").set("y2", str(attach1))
    root.find(".//*[@id='lead-2']").set("y1", str(attach2))
    # Also update viewBox to match actual dist
    root.set("viewBox", f"-0.5 -0.5 1.0 {dist + 1.0}")

    # 3. Set body colours
    body = root.find(".//*[@id='body']")
    body.set("fill", fill.name())
    body.set("fill-opacity", str(alpha))
    body.set("stroke", border.name())

    # 4. Set pin colours from net
    for i, pos in enumerate(positions):
        pin_el = root.find(f".//*[@id='pin-{i+1}']")
        if pin_el is not None:
            pin_el.set("fill", net_color_for_pin(i+1))

    # 5. Render
    xml_bytes = ET.tostring(root).encode()
    renderer = QSvgRenderer(QByteArray(xml_bytes))
    item = QGraphicsSvgItem()
    item.setSharedRenderer(renderer)

    # 6. Position: translate to pin1 world pos, rotate to match pin2 direction
    angle = degrees(atan2(p2.y - p1.y, p2.x - p1.x))
    t = QTransform()
    t.translate(p1.x, p1.y)
    t.rotate(angle)
    item.setTransform(t)
    item.setZValue(z)
    scene.addItem(item)
```

## Files

| file | package | component examples |
|------|---------|-------------------|
| `axial.svg` | Axial THT | R, L, F |
| `diode.svg` | Axial diode | 1N4148, 1N4007, Zener |
| `radial.svg` | Radial THT | C, C_POL, C_Disc |
| `to92.svg` | TO-92 | BC547, BC557, 2N2222 |
| `dip.svg` | DIP-8 template | LM358, NE555, DIP-N |
| `sil.svg` | SIL-4 template | Headers, connectors, SIL-N |

## Editing in Inkscape

1. Open any `.svg` in Inkscape.
2. Use **Object → Object Properties** (Ctrl+Shift+O) to verify `id` attributes.
3. Keep `id` values intact — the renderer uses them.
4. Coordinate grid: set **Document Properties → Snap** to 0.1 units.
5. Do **not** embed fonts or rasterize — keep everything as paths/primitives.
6. The `viewBox` should cover all geometry with a small margin (≈ 0.5 units).
