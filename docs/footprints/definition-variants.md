# Defining packages, families and components — all the variants, by example

Every way to declare a **component**, a reusable **package** (generic), and a
**package family** — and every place each declaration may live. This is the
recipe companion to [authoring.md](authoring.md) (field-by-field reference) and
[package-model.md](package-model.md) (design rationale).

## 1. The three section kinds and where they may live

A library YAML file may carry up to three top-level sections. Since the
`packages:`/`families:`-in-library-files feature, **all three may sit in the
same file** — or be split across files and directories however you like.

| Section | What it declares | Read from |
|---|---|---|
| `components:` | Electrical parts (name, class, pins, body choice) | `library/**/*.yaml` + user library dirs |
| `packages:` | Named reusable package definitions — `extends` targets | `assets/packages/*.yaml` (builtin) **and** any library file |
| `families:` | Catalogue families (variants offered by the body picker) | `assets/package_families/*.yaml` (builtin) **and** any library file |

Unknown top-level keys are logged as `library.unknown_section` — a typo like
`familes:` warns instead of vanishing silently.

Loader entry points: `load_library` (`services/library/loader.py`) reads all
three sections from library files; `load_package_families`
(`services/library/package_family_loader.py`) reads the builtin family assets;
`_load_builtin_package_defs` reads the builtin generics.

## 2. Defining a component — the six geometry variants

A component must obtain its **pins + body** one way or another. From simplest
to most featureful:

### 2a. `kind: generic` — pin cloud only, generic bbox body

```yaml
components:
  - name: TestPoint
    component_class: other
    symbol: TP
    pins: {count: 1}
    package: {kind: generic}
    pin_names: {1: TP}
```

The declared spelling of "no particular body" (the legacy bare `footprint:`
field was removed 2026-07-04): a non-rigid generic bbox package over a single
1-hole-pitch pin row, tagged `_generic` — authored `bendable_pins` stay in
force, the body is excluded from the DRC body mask and the body picker.

### 2b. Inline `package:` with explicit geometry

The package supplies the pins — no `footprint:` needed. The canonical
dev-board module:

```yaml
components:
  - name: MOD_2x4
    component_class: devboard
    package:
      shape: prism
      dims: {width: 12.5, length: 12.5, height: 1.6}
      leads:
        direction: [0, 0, -1]
        body_offset: {unit: 2.5, value: [[3, [0, 1]], [5, 0], [3, [0, -1]]]}
        board_offset: default
      bend: 0
    pin_names: {1: A, 8: B}
```

### 2c. Inline `package:` with `kind:` — parametric body

The look is known, the size follows the part's pin count:

```yaml
components:
  - name: U_DIP14
    component_class: ic
    symbol: U
    pins: {count: 14}
    package: {kind: dip_ic}
    pin_names: {7: GND, 14: VCC}
```

`kind:` may also be a **list** (a left→right generator chain: compute steps
deriving values, the last step building the body), and a kind whose required
parameters are only partially supplied becomes a *pending* placeholder — the
place-time picker collects the rest.

### 2d. Inline `package:` with `extends:` — reuse a generic

The block extends a named package: a builtin generic **or one declared in a
`packages:` section of any library file** (see §3):

```yaml
components:
  - name: MOD_A
    component_class: devboard
    package: {extends: my_module_base}   # ← declared in packages:, §3
    pin_names: {1: A}
```

### 2e. `footprints:` list — multiple bodies to pick from

Each entry is a catalogue reference (`ref: "family/variant"`) or an inline
`package:` option; the first entry is the default geometry. Real example from
`library/Passive/resistors.yaml`:

```yaml
components:
  - name: R
    component_class: resistor
    symbol: R
    pins: {count: 2}
    footprints:
      - {name: DIN-0207, ref: "DIN-0207/typ"}   # default
      - {name: DIN-0204, ref: "DIN-0204/typ"}
      - {name: SMD-0805, ref: "SMD-0805/typ", style: "color: #222"}
    pin_names: {}
```

A `ref:` may point at a builtin family, a generated one (`dip-16/std`), **or a
family declared in a `families:` section of a library file** (§4). Inline
options are auto-registered as one-variant families under `@name/option`
addresses, so the picker offers them like any catalogue body.

### 2f. One default catalogue option

```yaml
components:
  - name: LED_5mm
    component_class: diode
    symbol: D
    footprints:
      - {name: LED-5mm, ref: "LED/led50"}
    pin_names: {1: A, 2: K}
```

The first referenced option is the default geometry; a **rigid** body (SMD
chip, DIP) additionally drops the bendable leads, while a bendable body (axial
R/D) keeps its bend. Resolution precedence at render/DRC time: instance
`package_variant` → definition's first `footprints` option → inline `package:`.

### Component reuse: `extends:` and `aliases:`

Components can extend other components (cross-file; `_`-prefixed names are
non-placeable templates) and publish extra names:

```yaml
components:
  - name: _DIP14_BASE
    component_class: ic
    symbol: U
    pins: {count: 14}
    package: {kind: dip_ic}
    pin_names: {7: GND, 14: VCC}
  - name: 74HC00
    extends: _DIP14_BASE
    aliases: [74LS00]
    pin_names: {1: A1, 2: B1, 3: Y1}   # deep-merged over the base's
```

## 3. Defining a reusable package (generic)

A `packages:` entry is a named package definition other definitions `extends`.
Two places to put it:

**Builtin assets** (`assets/packages/generic.yaml`) — the abstract bases
shipped with the app: `package` (root, declares the `leads.*` schema),
`cylinder`, `prism`, `disc`, and the mounted generics `cylinder_axial`,
`cylinder_radial`, `prism_dil`, `prism_smd`, `disc_radial`, …

**Any library file** — for your own reusable bases:

```yaml
# library/Modules/my_modules.yaml
packages:
  - name: my_module_base
    extends: prism                     # builtin generics are in scope
    dims: {width: 12.5, length: 12.5, height: 1.6}
    leads:
      direction: [0, 0, -1]
      body_offset: {unit: 2.5, value: [[3, [0, 1]], [5, 0], [3, [0, -1]]]}
      board_offset: default
    bend: 0

components:
  - name: MOD_A
    component_class: devboard
    package: {extends: my_module_base}
    pin_names: {1: A}
```

Library `packages:` join one global registry with the builtins, so they are
visible to inline `package:` blocks and `families:` entries **in every library
file** (order-independent, like cross-file component `extends`). Mark a
package `abstract: true` if it is only a base, never instantiated directly.

## 4. Defining a package family

A family groups variants of one body that differ only in dimensions; the body
picker offers them. Family-level keys are inherited by every variant.

**Builtin assets** (`assets/package_families/*.yaml`) — e.g. `axial_glass.yaml`:

```yaml
families:
  - family: DO-35
    extends: cylinder_axial
    label: "DO-35 (subminiature axial glass)"
    color: "#f4ece0"
    default: typ
    variants:
      - id: typ
        label: "Typical (1.8 x 3.7 mm)"
        dims: {diameter: 1.8, length: 3.7, edge_radius: 0.9}
        leads: {lead_r: 0.2, lead_length: 5.6}
```

**Any library file** — same schema, next to the components that use it:

```yaml
# library/Diodes/my_diodes.yaml
families:
  - family: MY-DO
    extends: cylinder_axial
    label: "My axial body"
    color: "#112233"
    variants:
      - id: std
        dims: {diameter: 1.8, length: 3.7}
        leads: {lead_r: 0.2, lead_length: 5.6}

components:
  - name: D_MY
    component_class: diode
    symbol: D
    footprints:
      - ref: MY-DO/std
    pin_names: {1: A, 2: K}
```

Library-declared families join the session catalogue, so the picker offers
them alongside builtin and generated (`dip-N` / `wdip-N` / `sil-N`) families.

## 5. Everything in one file

A self-contained part file — generic base, family, component:

```yaml
# library/Switches/relays.yaml
packages:
  - name: relay_brick
    extends: prism
    leads: {direction: [0, 0, -1], board_offset: default}

families:
  - family: RM40B
    extends: relay_brick
    label: "Relpol RM40B (miniature PCB relay)"
    color: "#26323f"
    variants:
      - id: std
        dims: {width: 10.0, length: 20.0, height: 15.0}
        # pin-to-pin offsets after pin 1: pins at x = 0, 5, 20, 25 mm
        leads: {body_offset: {unit: 2.5, value: [[2,0], [6,0], [2,0]]}}

components:
  - name: RELPOL_RM40B
    component_class: other
    symbol: K
    footprints:
      - ref: RM40B/std
    pin_names: {1: COIL_A, 2: COIL_B, 3: NO, 4: COM}
```

## 6. Precedence and clash rules

- **Components**: first directory/file wins (builtin `library/` is scanned
  first, then user dirs from settings; files in sorted path order).
- **Library `packages:` vs builtin generics**: the builtin wins; the library
  entry is ignored with a `library.package_name_clash` warning.
- **Library `families:` vs the builtin catalogue** (authored + generated):
  the builtin wins; warning `library.family_name_clash`. Among library files,
  first wins.
- **Malformed entries degrade, never sink the load**: a bad `families:` entry
  logs `library.family_skipped`; a component whose package fails to build logs
  `library.package_skipped` and is dropped — the rest of the library loads.
- Builtin **asset** files are single-purpose: `assets/packages/*.yaml` and
  `assets/package_families/*.yaml` are read only for their own section (they
  are not scanned for `components:`).
