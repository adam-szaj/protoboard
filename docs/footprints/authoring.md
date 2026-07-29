# Defining components and packages

A practical guide to the library YAML: how to declare a **component**, how to give
it a **package** (its physical body + lead geometry), which **generic packages**
you can extend, what is **generated in code**, and what you can **customise** and
how.

For the underlying design rationale (the lead/offset model, the two offset
domains, the axial solver) see [package-model.md](package-model.md). For a rendered gallery see
[README.md](README.md). This document is the *authoring* reference and is grounded
in the loader as implemented (`src/protoboard/services/library/loader.py`,
`src/protoboard/domain/components/package/loader.py`).

---

## 1. The big picture

```
component  ──references──▶  package        ──gives──▶  body shape + pin geometry
(what it is)               (physical body)            (how it draws / where pins land)
```

- A **component** (`components:` entry) is an electrical part: a name, a class, pin
  names, and a way to obtain its **pins + body**.
- A **package** (`packages:` entry, or a `package_families:` family, or an inline
  `package:` block on the component) is the physical body — a `cylinder`, `prism`
  or `disc` with lead geometry. The package decides the pin positions and the body
  outline/colour.
- A component gets its package one of three ways (precedence, highest first):
  1. the placed **instance's** `package_variant` (`"family/variant"`),
  2. the definition's **`package_family_ref`** (`"family/variant"`),
  3. an **inline `package:`** block on the component.

  Resolved in `resolve_component_package`
  (`domain/components/package/component_package.py`). The last rung always
  exists — `defn.package` is required (a `kind: generic` bbox body is the
  spelling for "no particular body").

Files: components load from `library/**/*.yaml`; builtin packages from
`assets/packages/*.yaml`; builtin families from `assets/package_families/*.yaml`.
Any library file may **also** carry its own `packages:` and `families:` sections
next to `components:` — same schema as the assets; builtins win on a name clash
(earlier directories win on component name collision). See
[definition-variants.md](definition-variants.md) for examples of every
combination.

---

## 2. Defining a component

A `components:` entry. Fields (from `_component_from_dict`, `loader.py:226`, and the
`ComponentDefinition` dataclass, `definition.py:124`):

| Field | Req. | Meaning |
|---|---|---|
| `name` | yes | Part identifier (`"BC547"`, `"R"`, `"NE555"`). |
| `footprint` | — | **REMOVED (2026-07-04).** Declare geometry via `package:` (inline or `kind:`), `footprints:` (catalogue refs), or a resolvable `package_family_ref` (the referenced variant is then the default geometry). A part still using `footprint:` is skipped at load with a migration hint. |
| `pins` | — | `pins: {count: N}` (or bare `pins: N`) — the logical pin count a `kind:` package is parameterized by. |
| `pin_names` | — | `{1: "C", 2: "B", 3: "E"}` — pin number → signal label. Belong to the component, not the package. |
| `component_class` | — | Open classification tag. Known values (§7) drive the refdes prefix and a few rendering specifics (polarity marker, devboard body); **any other string also loads** (warned as `library.unknown_component_class`, behaves like `other`, the label survives in BOM/panels). Default `other`. |
| `description` | — | Human text. |
| `package` | — | Inline parametric package (§3) — body + pin geometry. |
| `package_family_ref` | — | `"family/variant"` into the family catalogue (§5). |
| `variants` | — | Alternative `pin_names` mappings (same footprint, different labelling) — each `{id, label, pin_names}`. |
| `aliases` | — | Extra names the part is also found under. |
| `default_value` | — | Value shown when placed (`"?"` for a generic template, `""` for a concrete part). |
| `metadata` | — | Free key→value bag (not interpreted by the renderer). |
| `bendable_pins` | — | Per-pin reach/bend for flexible THT leads (resistors etc.). |
| `body_blocks_routing` | — | `true` for SMD/tight parts whose body should block routing under it. |

Minimal examples:

```yaml
components:
  # A) component that points at a catalogue family (preferred for real parts).
  #    The referenced variant IS the default geometry — nothing else needed.
  - name: R_SMD_0805
    component_class: resistor
    package_family_ref: SMD-0805/typ
    pin_names: {}

  # B) component with a parametric kind (a 4-pin single-row header)
  - name: J1
    component_class: connector
    pins: {count: 4}
    package: {kind: sil}
    pin_names: {1: VCC, 2: GND, 3: TX, 4: RX}

  # C) component with an inline package (body + pins defined right here)
  - name: BC547
    component_class: transistor_npn
    package: {extends: TO-92}
    pin_names: {1: C, 2: B, 3: E}
```

---

## 3. Defining a package (the body)

A package is a `cylinder`, `prism` or `disc` (the `shape:` registry; dispatch in
`package/loader.py`). Geometry is computed in code from the data — the YAML is pure
description.

| `shape` | `dims` keys (mm) | Typical use |
|---|---|---|
| `cylinder` | `diameter`, `length`, `edge_radius` | axial resistor/diode bodies, radial electrolytics |
| `prism` | `lx`, `ly`, `lz`, `corner_radius` | DIP, TO-92, SMD chips, dev-board PCBs |
| `disc` | `diameter`, `thickness` | disc capacitors, LEDs |

Common fields on a package:

| Field | Meaning |
|---|---|
| `shape` | `cylinder` \| `prism` \| `disc`. |
| `extends` | Name of a package to inherit from and close (deep-merge, §4). |
| `abstract: true` | A base meant only for `extends`, never used directly. |
| `dims` | Body dimensions in **mm**. Prism keys are LOCAL-FRAME axis names: `lx` along the lead axis, `ly` across it, `lz` off-board elevation — translate the datasheet's length/width/height once, at authoring time. `diameter`/`thickness`/cylinder-`length` keep their datasheet words (rotational anatomy has no single-axis spelling). |
| `color` | Body fill, `#rrggbb` or `#rrggbbaa` (CSS order, alpha last; see §8). |
| `edge` | `sharp` \| `rounded`. |
| `margin` | (prism) PCB margin in mm from outer pin centres — when present, `dims` may be derived from the pin bbox. |
| `bend` | Lead "give": one radius in **holes** the pin may move from its default (rigid = `0`). |
| `leads` | Lead geometry — `direction`, `body_offset`, `board_offset`, … (see §3.1 and package-model.md §2). |

### 3.1 Pin geometry — aggregated offsets

Pin positions are a chain of **consecutive offset vectors**, cumulative from pin 1
at the origin `[0,0]`. For N pins you give **N−1** step vectors. Two domains
(see package-model.md §2.3, "Two offsets"):

- **`body_offset`** — where leads exit the body. Declares its own `unit` (mm per
  step); `{unit: 2.5, value: [...]}` (or a bare list, unit defaults to 1.0).
- **`board_offset`** — where pins land in the grid (holes, fixed raster 2.5 mm).
  Usually `default` = the same vectors read on the grid; or an explicit list when
  the board shape differs; or `solve` for the axial solver (`body_offset == 0`).

**Run-length form** for repeated steps: `[count, [x, y]]` (second element a *list*
= repeat `count` times) vs `[x, y]` (second element a *number* = a single step).
The repeat counts must sum to N−1.

```yaml
# DIP-8: down the left column (3 steps), jump 3 across, down the right (3 steps)
body_offset: {unit: 2.5, value: [[3, [0, 1]], [3, 0], [3, [0, -1]]]}
board_offset: default
```

This is also how **dev-board modules** declare their (many) pins — see §9.

---

## 4. Generic packages and `extends:`

`extends:` does a recursive **deep merge** of the parent dict under the child
(`resolve_extends` + `_deep_merge`, `package/loader.py:52`): nested dicts merge,
scalars/lists replace, child wins. A field set to `null` means **required** — a
child (or the GUI at instancing time) must supply it, else load fails
(`_require_*`). This is how a generic base leaves `diameter: null` for the concrete
part to fill.

Generic bases you can `extends:` (from `assets/packages/generic.yaml`):

| Base | Shape | What it sets up |
|---|---|---|
| `cylinder` | cylinder | abstract root; `diameter/length` left `null` |
| `prism` | prism | abstract root |
| `disc` | disc | abstract root |
| `cylinder_axial` | cylinder | two opposite leads, `body_offset: 0`, `board_offset: solve` (axial solver) |
| `cylinder_radial` | cylinder | two same-side leads, `board_offset: default`, `bend: 0` |
| `prism_dil` | prism | rigid DIP-style (`direction: [0,0,-1]`, `bend: 0`) |
| `prism_smd` | prism | SMD chip (`mount: surface`, axial arrangement) |
| `disc_radial`, `radial_brick`, `prism_radial` | disc/prism | radial two-lead bodies (LED, ceramic, HC-49) |

Concrete named packages in the same file (`DO-35`, `R_Axial_DIN0207`,
`CP_Elec_6.3x11`, `TO-92`, `DIP-8`) show the pattern: `extends:` a base, fill the
`dims`/`leads`, done.

```yaml
packages:
  - name: DO-35
    extends: cylinder_axial
    description: "Glass axial diode body"
    dims: {diameter: 2.0, length: 4.0}
    leads: {lead_r: 0.27}        # board_offset stays `solve`
```

---

## 5. Package families and variants

A **family** (`package_families:` entry) groups variants of one body that differ
only by dimensions — e.g. resistor body sizes. A component picks
`"family/variant"`; a placed instance may override with its own `package_variant`.

```yaml
families:
  - family: DIN-0207
    extends: cylinder_axial
    label: "DIN 0207 / R-6 (1/4 W axial)"
    color: "#d6c08c"
    default: typ
    variants:
      - id: typ
        label: "Typical (2.5 x 6.5 mm)"
        dims: {diameter: 2.5, length: 6.5}
        leads: {lead_r: 0.27, lead_length: 8.0}
      - id: S
        label: "Small (2.0 x 5.5 mm)"
        dims: {diameter: 2.0, length: 5.5}
```

Resolution: `resolve_variant(families, "DIN-0207/typ")` → that variant; a bare
`"DIN-0207"` → the `default` (or first) variant
(`family.py`, `resolve_variant`).

---

## 5b. Offering several footprints — `footprints:`, `symbol:`, `kind:`

A component's **logical identity** (name / pins / class) is separate from the
**footprints** it can be placed in. Instead of a single `package_family_ref`, a
component may list the bodies it offers; the place-time picker forces the user to
choose one (label `symbol_name`, e.g. `R_DIN-0207`).

```yaml
- name: R
  component_class: resistor
  symbol: R                      # refdes prefix + picker label root
  pins: {count: 2}              # logical pin count (drives kind packages)
  default_value: "?"
  footprints:                    # the available bodies (the first is the default)
    - {name: DIN-0207, ref: "DIN-0207/typ"}   # a catalogue family/variant
    - {name: SMD-0805, ref: "SMD-0805/typ"}
    - name: custom               # …or an inline package per entry
      package: {shape: prism, dims: {ly: 5, lx: 2, lz: 1}, leads: {...}}
      style: "color: #333"       # optional per-entry style override
  footprint_style: "color: #228" # default style for every entry (overridable)
```

A per-entry `style:` (or the list-wide `footprint_style:`) colours the body of
the **placed option**: when the instance uses that footprint (its
`package_variant` matches the entry, or the entry is the default first one),
the style layers between the instance override and the component-wide `style:`
(instance > footprint entry > component > package colour > theme).

- **`symbol:`** is the refdes prefix and the picker-label root; omit it to fall
  back to the class default (`REFDES_PREFIX[component_class]`, e.g. resistor → `R`).
- **`footprints:`** entries are a **ref** (`ref: "family/variant"`) or an **inline
  `package:`** (built like a top-level package). `name` defaults to the ref token.
  The default/first entry derives the component's singular geometry; inheriting
  definitions that re-list `footprints:` **replace** the list.
- **`kind:`** in a `package:` block names a parameterized body whose geometry is
  built from the pin count: `dip_ic` / `wdip_ic` (DIP narrow / wide), `sil`
  (header), `dip_switch` (plain dual-row, no notch). The renderer draws the
  declared look (the notch for a DIP-IC, plain for a DIP-switch) instead of
  guessing from the pin columns; an unknown kind falls back to that guess.
  A `kind` is a **procedural generator** (`package/kind.py`) that synthesises the
  body from the pin count — a *separate* build path from the YAML `dims` /
  `schema` families (`footprints:` refs above). See
  [package-model.md](package-model.md) §6 (`kind`) vs §5 (schema/families).

```yaml
- name: 74HC00
  component_class: ic
  pins: {count: 14}
  package: {kind: dip_ic}        # DIP-14 body + notch, sized from pins
  pin_names: {1: A1, 2: B1, 7: GND, 14: VCC, ...}
```

`extends:` (§4) works across files now (a base in one file, a child in another).

---

## 6. The removed `footprint:` block — migration map

The legacy `footprint:` field was **removed 2026-07-04**: it only fed the
synthesized generic body, and every use has a declared equivalent. A part
still using it is skipped at load with the hint in the log. Migration map:

| Legacy | Declared equivalent |
|---|---|
| `footprint: {type: DIP, pins: N}` | `pins: {count: N}` + `package: {kind: dip_ic}` (IC look) or `{kind: dip_switch}` (plain) |
| `footprint: {type: SIL, pins: N}` | `pins: {count: N}` + `package: {kind: sil}` (or `pin_header`) |
| `footprint: {type: TwoPin}` + `package_family_ref` | drop the footprint — the family variant is the default geometry |
| `footprint: {type: TwoPin}` (no ref) | `pins: {count: 2}` + `package: {kind: generic}` — or an explicit prism |
| `footprint: {type: TO92}` | `footprints: [{name: TO92, ref: "TO-92"}]` |
| non-raster pitch (`pitch: 2.0`) | an explicit inline `package:` prism whose `body_offset` spells the pitch out |

`kind: generic` is the declared spelling of the old bare-footprint semantics:
a plain bbox body over a single 1-hole pin row that keeps every `_generic`
behaviour (authored `bendable_pins` stay in force, excluded from the DRC body
mask and the body picker).

---

## 7. Component classes

`ComponentClass` (`definition.py:16`): `resistor`, `capacitor_polar`,
`capacitor_nonpolar`, `inductor`, `transformer`, `diode`, `diode_zener`,
`diode_led`, `transistor_npn/pnp/nmos/pmos/njfet/pjfet`, `ic`, `devboard`,
`connector`, `switch`, `fuse`, `crystal`, `mechanical`, `other`.

The class drives:

- the **refdes prefix** (`REFDES_PREFIX`, `definition.py:47` — R, C, L, D, Q, U,
  J, …; `devboard` → `U`). Every class must have an entry (indexed with no
  fallback);
- a few **rendering specifics**: polarity marker for diodes / polar caps; the
  half-circle `TO92Body` for a 3-pin SIL transistor; the plain-PCB body + pad
  rings for `devboard` (§9).

The class does **not** by itself pick the body shape or its colour (those come from
the package / footprint).

---

## 8. Customising appearance — the `style:` override

> **Headline question: can a resistor in SMD-0805 render black while a capacitor in
> the *same* SMD-0805 renders beige?**
>
> **Yes.** A component definition *and* a placed instance can carry a visual-only
> **`style:`** string that overrides the body's fill colour and corner radius
> without touching geometry. Both parts keep the shared
> `package_family_ref: SMD-0805/typ`; each just adds a `style:` colour.

`style:` is a single CSS-like string of `;`-separated declarations, each
`property: value` (the first `:` splits the property from its value). Two
properties are supported:

| Property | Meaning |
|---|---|
| `color` | `#rgb` / `#rgba` / `#rrggbb` / `#rrggbbaa` body fill (CSS channel order — **alpha last**); the border is derived by darkening. A declared alpha (e.g. `#ff000080` ≈ 50 % red, a translucent LED lens) **multiplies** with the layer alpha at render time (ghosting/back-side dimming), it does not replace it. |
| `corner-radius` | 1–4 bare millimetre numbers in CSS `border-radius` order. **`0` = sharp (square)**, **`> 0` = rounded** with that radius; overrides the package edge either way. |

An unknown property is an error; a duplicate property keeps the **last**
declaration (CSS behaviour). The 1–4 radius values are rendered **per corner**
using the CSS `border-radius` expansion — `(top-left, top-right, bottom-right,
bottom-left)`; e.g. `corner-radius: 2 0` rounds only the top-left and
bottom-right corners.

`style:` can sit on a **component** (override for the whole type) and on a placed
**instance** (override for one part). Precedence per field is **instance →
definition → package → theme default** (`Appearance.resolve`,
`domain/components/appearance.py`).

```yaml
components:
  # Two parts share the SMD-0805 body; only the colour differs.
  - name: R_0805
    component_class: resistor
    package_family_ref: SMD-0805/typ
    style: "color: #1a1a1a"                       # black-ish
    pin_names: {}
  - name: C_0805
    component_class: capacitor_nonpolar
    package_family_ref: SMD-0805/typ
    style: "color: #d9c7a3; corner-radius: 0.3"   # beige, slightly rounded
    pin_names: {}
```

A placed instance overrides per part (in the project's `instances.yaml`, or
programmatically), winning over the definition:

```yaml
instances:
  - id: …
    definition_ref: R_0805
    refdes: R7
    value: 10k
    style: "color: #cc3030"                       # this one part is red
```

Resolution at render time (`PackageRenderer._resolve_colors` /
`_effective_corner_radii_mm`, `gui/renderers/packages/base.py`):

1. `color` (instance then definition) — **override wins**;
2. else the resolved **package** `color:` (border = darkened);
3. else the theme default `comp_fill_color` (`theme.py`, pale green).

Corner radius follows the same fall-through, with `0` forcing square corners even
on a `rounded` package and `> 0` forcing rounded even on a `sharp` one.

Notes:
- **Visual only.** `style:` changes nothing in geometry, collision mask, DRC,
  routing or pin positions.
- Colour is **not** derived from `component_class`.
- The legacy `appearance: {color, corner_radius}` mapping is **no longer read** —
  a leftover block is silently ignored (no override). Use `style:`.
- An inline `package:` may still set `color`/`corner_radius` on the package itself
  (it `extends:` a builtin base like `prism_smd` — not a family); `style:` is
  the lighter way to vary just the visuals on a shared family.
- `GenericBody` (the bare-bbox fallback for a part with no package) honours the
  override the same way the package-backed renderers do.
- **GUI editing:** the Properties panel edits a selected instance's style — a
  colour field (`#RRGGBB[AA]`) and a corner-radius field (1–4 mm values),
  committed with the **Apply** button. An **empty field = no override** (falls
  back to the option/definition style, then the package/theme defaults).
  Changes go through `SetComponentAppearanceCommand` (undo/redo-able, recorded
  for replay as `set_appearance`).

---

## 9. Dev-board modules (`devboard`)

A development board (Arduino Nano, STM32 BluePill, …) is a `devboard` component
with an **inline `prism` package**: explicit real PCB `dims` (mm) and its pins as
aggregated run-length offsets. It renders as a plain PCB rectangle (no DIP notch),
pins on the top surface with a faint pad-sized ring marking the click-active area.

```yaml
components:
  - name: STM32_BluePill
    component_class: devboard
    package:
      shape: prism
      dims: {ly: 22.86, lx: 53.0, lz: 1.6}   # real PCB, mm (ly across / lx along the pin rows)
      leads:
        direction: [0, 0, -1]
        # 2×20 header: 19 down the left column, jump 6 across, 19 up the right
        body_offset: {unit: 2.5, value: [[19, [0, 1]], [6, 0], [19, [0, -1]]]}
        board_offset: default
      bend: 0
    pin_names: {1: VBAT, 2: PC13, …}
```

See `library/Modules/boards.yaml` for the four shipped modules (Nano, BluePill,
BlackPill, STM32G4 WeAct — the WeAct uses four columns via two extra column-jump
vectors).

---

## 10. Generated families (no YAML needed)

Some families are **synthesized in code** (`generated_families.py`) and merged into
the catalogue, so you do not write YAML for every pin count:

| Family | Generated for | Body |
|---|---|---|
| `dip-N` | even N ∈ [4, 64], 0.3" rows | narrow DIP |
| `wdip-N` | even N ∈ [4, 64], 0.6" rows | wide DIP |
| `sil-N` | N ∈ [3, 40] | single-row header housing |

A component opts in by referencing the address (`package_family_ref: dip-16/std`)
or, when picking a body in the UI, the picker offers the families matching the
footprint's pin count (`offered_body_families`). Each generated family has a single
variant `std`.

Separately, `prism_for_footprint` (`from_footprint.py`) synthesizes a prism on the
fly for a bare `DualRow` / `SingleRow` / `QuadRow` footprint that has no catalogue
package — so even a plain `footprint: {type: DIP, pins: 16}` gets a proper DIP body.

---

## 10b. Custom kind generators (plugins)

A package **kind** (`package: {kind: pin_header, rows: 2}`) is a parameterized
body recipe. To add your own, drop a `.py` file into a `generators/`
subdirectory of any component-library directory — it is imported at startup
and on a preferences reload, and defining the class **is** the registration:

```python
# <library>/generators/my_relay.py
from protoboard.domain.components.footprints import DIL
from protoboard.domain.components.package.generators import (
    PackageGenerator, prism_from_footprint, register_generator,
)
from protoboard.domain.components.package.schema import parse_schema

@register_generator(kind="relay_dpdt")           # the kind name
class RelayGenerator(PackageGenerator):
    params = parse_schema({                       # the parameter set {K}
        "rows_spacing": {"type": "float", "default": 3.0},
    })
    color = "#3355aa"                             # optional body colour

    def generate(self, ctx):
        pkg = prism_from_footprint(
            DIL(n=ctx.pin_count, rows_spacing=ctx.values["rows_spacing"]))
        return ctx.with_package(pkg)
```

Components then declare `package: {kind: relay_dpdt}`. Rules of the game:

- **Later registration wins** — your plugin may override a builtin kind.
- **Parameters** are validated against `params` before `generate` runs; a
  field left `null` by the component is a *hole* the place-tool picker asks
  the user to fill (the build itself uses the default).
- Missing **required** parameters make the kind *partially applied*: the
  component loads with a placeholder body and the picker collects the rest.
  `generate` may also return `ctx.with_residual(ResidualKind(other_kind,
  values))` to redirect to a different kind.
- A generator is **pure domain** — no Qt, no I/O; it must be importable
  headless. A broken plugin file is logged and skipped.
- Pair it with a **renderer plugin** for the same kind name
  (`@register_renderer(kind="relay_dpdt")` in `<library>/renderers/…` — see
  [../rendering.md](../rendering.md) §4.2) for a custom look; without one the
  body renders through the shape-root renderer.

---

## 11. Quick recipes

- **Real part on a standard body** → `footprint:` + `package_family_ref` into a
  catalogue family, set `pin_names`. (Resistor → `TwoPin` + `SMD-0805/typ`.)
- **Same body, two colours per part** → keep the shared `package_family_ref` and
  add a `style: "color: #…"` string to each component (or instance) (§8).
- **A new body size** → add a `variant` to an existing family, or a new named
  package with `extends:` a generic base + `dims`.
- **A module / dev board** → `component_class: devboard` + inline prism with real
  `dims` and run-length `body_offset` (§9).
- **Just need pins, generic body** → bare `footprint:` block (§6).
