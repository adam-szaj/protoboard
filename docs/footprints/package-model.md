<!-- SPDX-License-Identifier: MIT -->
# Package model — bodies, leads, schema, kind

The canonical description of the *package* model (a component's body): body and
lead geometry, the two offset domains, the axial solver, the `extends`
hierarchy, the parameter-declaration system (`schema` / `auto`), and the
procedural `kind` generators. The practical YAML-authoring guide lives
separately in [authoring.md](authoring.md); the rendered gallery in
[README.md](README.md); the component model that *points at* a package in
[../data-model.md](../data-model.md) §4.

> **Implementation status.** The model is **implemented**: the `Package` /
> `Cylinder` / `Prism` / `Disc` classes
> (`domain/components/package/shapes.py`), the loader
> (`package/loader.py`), the schema (`package/schema.py`), and the `kind`
> generators (`package/kind.py`). Open items (the place-tool picker, strict
> mode, the generator chain driver) are flagged below and tracked in
> [../todo.md](../todo.md).

---

## 1. Problem and solution

Electronic components — regardless of their electrical characteristics — can be
placed in different bodies. Some body types are strictly defined, so the name
alone describes the package's look and dimensions (DIP-8, TO-92, TO-220, DO-35,
DO-41). Several of these are parameterized versions of more generic shapes
(prism, cylinder). The body shape is only part of the problem — the leads matter
too: some bodies have rigid dimensions (DIP), others allow flexibility through
bendable wire leads (THT resistors).

The library defines a number of **"named" bodies** plus a set of **parametric
bodies**. Parametrization lets the user tailor the library to whatever is
actually in their drawer — and knowing "this configuration is buildable" before
soldering matters, because rerouting after part of the board is already soldered
can be costly.

Most common bodies are described by geometric primitives: the **cylinder** and
the **rectangular prism**, which form the basis of most footprints. (The "DIP"
and "TO" families additionally have dedicated renderers — see
[../geometry.md](../geometry.md) §11.)

Two-pin bodies split, by lead arrangement, into **axial** and **radial**:

- **axial** — leads on the same axis, on opposite faces of the body;
- **radial** — leads on the same face of the body.

The model extends to any number of leads.

---

## 2. Lead geometry

### 2.1 Body parameters

- **Prism** — three dimensions `W × L × H` + an optional edge-rounding radius
  (`0` = no rounding).
- **Cylinder** — `D × L` (or `H`) + an optional rounding radius seen from the
  cylinder's side face.

### 2.2 `direction` — lead direction

A nominal direction relative to the body. If the cylinder is described in the
`(X, Y, Z)` frame with its axis along `Z`, the lead directions for an axial
capacitor are `[0,0,1]` and `[0,0,-1]`; for a radial one both share the same
direction, e.g. `[0,0,-1]`.

`direction` has **three notations**:

- **single vector** → all pins (DIP: `[0,0,-1]` → N× downward);
- **list of vectors** (as many as pins) → per pin (axial: `[[0,0,-1],[0,0,1]]`;
  radial: `[[0,0,-1],[0,0,-1]]`);
- **grouped** `[[indices], vector]` → for irregular N-pin layouts, e.g.
  `[[[1,2,3,4],[0,0,-1]], [[5,6,7,8],[0,0,1]]]` (pins 1-4 down, 5-8 up).

### 2.3 Two offsets: `body_offset` vs `board_offset`

Axial and radial are not two independent types but **named special cases** of
one, more general lead geometry — described **per pin**, not by a single label.
Lead spacing comes in **two variants**:

- **`body_offset`** — where the leads exit *the body*. A physical manufacturer
  trait (TO-92: 1.25 mm; axial resistor: 0). Rigid.
- **`board_offset`** — where the pins *land in the board's holes*. A result of
  assembly (possible lead bending).

Both share an **identical structure**: a list of N−1 consecutive vectors. For
convenience, when there is a single pair (a 2-pin component), YAML may give a
**scalar** `s` (expanded to `[[s, 0]]`) or a single vector `[x, y]`; the loader
always normalizes to a list.

**One vector describes both offsets.** Since `body_offset` and `board_offset`
differ **only in domain/unit** (body vs grid), not in shape, the **same vector**
usually serves as both — give only **one** (`body_offset`), and
`board_offset: default` reads that same `value` in the grid domain. TO-92:
`value [[1,0],[1,0]]` is 3-in-line both on the body (×1.25 mm) and on the board
(every 1 hole). The renderer shows the spread that follows from the unit
difference (1.25 → 2.5).

**Give the second vector only when the shape must differ** — not when only the
unit differs. Example: 3-in-line on the body, but pin 2 shifted sideways on the
board (a triangular mount):

```yaml
body_offset:  [[1,0],[1,0]]      # 3-in-line on the body
board_offset: [[1,1],[1,-1]]     # pin 2 shifted — a different SHAPE, not a unit
```

`board_offset` is the source of truth for the **default pin positions on the
board**. Pin 1 sits at local `[0,0]` *on the board* (not at the body-exit
point), and the remaining pins follow a **list of consecutive, adjacent
vectors** (from pin *i* to *i+1*); for N pins the list has N−1 elements,
positions computed cumulatively. The vectors are 2D, so the same structure
describes any topology — e.g. DIP-8:

```
board_offset = [[0,1],[0,1],[0,1],[3,0],[0,-1],[0,-1],[0,-1]]
```

(pin1→4 down the left column, jump to the right, pin5→8 up).

**Run-length form.** Repeated offsets are grouped by the **count of consecutive
repeats** (a different mechanic than grouping `direction`, which enumerates pin
indices — here we count steps in the pin→pin chain). An element is:

- **`[count, [x,y]]`** — the vector `[x,y]` repeated `count` times (2nd element
  is a list),
- **`[x,y]`** — a single vector (2nd element is a number; `count` = 1).

Distinguished by the type of the 2nd element: list → run-length, number → a bare
vector. DIP-8 compactly:

```
body_offset = [[3,[0,1]], [3,0], [3,[0,-1]]]
            =  3×[0,1]      [3,0]   3×[0,-1]
```

A single jump is not wrapped in `[1,[3,0]]` — `[3,0]` suffices. The sum of
counts = N−1 (DIP-8: 3+1+3 = 7).

**By default `board_offset` ≡ `body_offset`** where it makes sense:

- **Rigid leads (DIP):** no bending, so board = body permanently.
- **Bendable leads (TO-92):** `body_offset` rigid, but `board_offset` may differ
  (e.g. 1.25 → 2.5), usually within a given **range**.
- **Axial `body_offset == 0`:** board ≠ body by definition (the pins cannot hit
  the same hole) — `board_offset` is then **computed from geometry** (§2.5).

### 2.4 Units: two domains

Offset numbers are written **without a unit in the name** (`offset`, not
`offset_mm`). The two offsets live in **two different domains**:

- **`board_offset` — the grid domain.** Always in **whole holes** (unit = board
  raster = 2.5 mm, fixed). **No `unit`** — `[1,0]` is one hole by definition.
- **`body_offset` — the body domain.** Declares its own scalar **`unit`** (e.g.
  `1.25`, `2.5`); `[1,0]` is multiplied by `unit` and kept in mm. These are the
  natural steps of a given body (TO-92: `unit: 1.25`).

**Body dimensions** (diameter, length, height) and `lead_length` are always
given in **mm** — datasheet data, not raster.

**Canonical raster = 2.5 mm.** Internally the 0.1" / 2.54 mm raster is
represented as **2.5 mm** (2.54 truncated). 2.5 mm and 0.1" are treated as the
**same raster** — the 0.04 mm difference is absorbed by the clearance between the
lead diameter and the hole. Metric components (lead spacing 2.5) and imperial
boards (holes 2.54) sit on one grid.

### 2.5 Solver: `board_offset` for axial (`body_offset == 0`)

For axial components with `body_offset == 0` (resistor, axial diode) the
on-board spacing has no single "true" value — we compute the **minimum spacing**
from geometry. Parameters:

- `L` — body length (the lead axis),
- `r_body` — the cylinder radius *or* half the shorter edge of the exit face
  (prism),
- `lead_r` — lead-wire radius; minimum bend radius = `3·lead_r`,
- `body_eccentricity` — the lead exit's eccentricity relative to the body axis
  (default 0).

Linear approximation (`3·lead_r` added as bend allowance):

```
horizontal (DEFAULT):  board_offset_min = L + 6·lead_r
vertical:              board_offset_min = r_body − body_eccentricity + 3·lead_r
```

- **Default = horizontal orientation** (axial parts are mounted flat more often)
  — a reasonable convention, not a hard rule.
- Vertically, eccentricity **reduces** the minimum spacing.

**Maximum spacing** from the lead length (both leads straightened):

```
board_offset_max = board_offset_min + 2·lead_length
```

The solver returns values in **mm (continuous)** — the radii of the allowed
spacing ring. `Package` does not know the raster of a specific board.
**Orientation** (horizontal/vertical) is **instance/placement state**, not a
package trait: the solver computes both ranges and which one is active depends on
how the instance was laid out.

**"Snap to raster" — a ring, not an axis.** Pin positions are discrete (each THT
pin must hit a physical hole — a shift `(dX, dY)` is always whole rasters), but
the spacing range is a **continuous constraint**: any whole `(dX, dY)` is allowed
that satisfies (Euclidean metric)

```
r_min ≤ √(dX² + dY²) ≤ r_max
```

Pin 2 may thus fall into **any hole in the ring** `[r_min, r_max]` around pin 1 —
at any angle (e.g. `[5,2]`), not only ortho or diagonal.

### 2.6 Bendability (`bend`)

When geometry cannot be solved analytically (anything beyond axial
`body_offset == 0`), the allowed pin slack is given **explicitly**. Bendability
is a value in **holes** (the board domain) = the **length of the displacement
vector** of a pin relative to its default `board_offset` position. The pin can
fall into any hole in a **circle of radius = bend** around the default
(isotropic 2D slack). One value **per package** (pin 1 remains the origin anchor
`[0,0]`).

Bendability is **analogous** to, but **not identical** with, the axial solver's
range: the solver gives an **asymmetric range along the axis**, `bend` gives a
**symmetric radius** around the default. Unifying the two is under consideration
(tracked in [../todo.md](../todo.md)); for now they stay separate.

**Lower clamp.** `bend` is symmetric (±r), but physics adds a lower bound:
`board_offset` cannot drop **below `body_offset`** — the leads will not cross
tighter than they exit the body. The effective range:

```
[ body_offset , body_offset + bend ]
```

### 2.7 Eccentricity (`body_eccentricity` / `body_axis_pos`)

The shift of the body's symmetry axis relative to the geometric midpoint of the
lead axis. **Two notations, one field** — give **only one** in a definition
(giving both is a load error):

- **`body_eccentricity`** (relative) — a 2D vector (mm) from the geometric
  midpoint of the lead axis to the body's symmetry axis; a scalar `s` is sugar
  for `[s,0]`; default `[0,0]`.
- **`body_axis_pos`** (absolute) — the **absolute** position of the body's
  symmetry axis relative to the origin (pin-1), in holes; mutually exclusive with
  `body_eccentricity`; not allowed with `board_offset: solve`.

The loader converts `body_axis_pos` to `body_eccentricity` **once at load time**;
the model then keeps only `body_eccentricity`:

```
body_eccentricity = (body_axis_pos − geo_mid) · raster
```

where `geo_mid` is the geometric midpoint of the default pin layout on the board
(in the board unit = the 2.5 hole), origin = pin-1 = `[0,0]`. For
`board_offset: solve` `geo_mid` does not exist up front, so `body_axis_pos` is
disallowed there — the eccentricity goes to the solver as relative.

### 2.8 Required values (`null`) and closure

`null` means **"no sensible default — the user MUST supply one"** (distinct from
the value 0). The `extends` hierarchy **closes** values monotonically: each level
(generic → series → component) may fill a `null` with a concrete value, but a
**once-closed value is never overwritten**. A value that stayed `null` until
instantiation is enforced in the GUI.

`lead_length` defaults to `null`. For axial `body_offset == 0` it is a **solver
input**; for DIP/TO-92 it is mostly information for the renderer.

---

## 3. Worked examples

### 3.1 Axial resistor (DIN 0207)

Data: `L = 6.3`, `D = 2.5` (→ `r_body = 1.25`), `lead_r = 0.27`,
`body_eccentricity = 0`, raster 2.5 mm. `lead_length ≈ (70 − 6.3)/2 ≈ 32 mm`.

```
min horizontal (default) = L + 6·lead_r      = 6.3 + 1.62 = 7.9 mm  → 4 rasters
min vertical             = r_body + 3·lead_r  = 1.25 + 0.81 = 2.06  → 1 raster
max                      = 7.9 + 2·32         = 71.9 mm             → ~29 rasters
```

The solver **reproduces** the hand-tuned `min` (1) and `default` (4) from pure
geometry; `max` (~28) is near 23 — the difference comes from the assumed
`lead_length`.

```yaml
packages:
  - name: R_Axial_DIN0207
    extends: cylinder_axial
    dims: {diameter: 2.5, length: 6.3}
    leads: {lead_r: 0.27, lead_length: 32.0}   # board_offset = solve
```

> `lead_length` has NO hard default — in the data it is `null` (required). The
> "1.5 × body dimension" rule is only a **GUI field start value**, which the
> user overrides with the real length from the datasheet.

### 3.2 Radial electrolytic (6.3×11)

```yaml
packages:
  - name: CP_Elec_6.3x11
    extends: cylinder_radial          # board_offset:default
    dims: {diameter: 6.3, length: 11.0}
    leads:
      body_offset: {unit: 2.5, value: [[1,0]]}   # 2.5 mm = 1 hole
    bend: 4                            # spread up to +4 holes
```

default = `body_offset` = 1 hole; range: lower clamp = `body_offset` (1), upper =
`body_offset + bend` = 5. It does not invoke the solver (`body_offset ≠ 0`), but
uses `unit`/`step` and `bend`.

### 3.3 TO-92 (3 pins, one vector, two domains)

A boundary test: **the same offset vector yields different spacing on the body
and on the board — because the unit differs, not the shape**. 3-in-line at
1.25 mm on the body, at 2.5 mm on the board.

```yaml
packages:
  - name: TO-92
    extends: prism
    dims: {width: 4.5, length: 4.5, height: 5.2}
    leads:
      direction:   [[0,0,-1],[0,0,-1],[0,0,-1]]   # radial, 3 pins
      body_offset: {unit: 1.25, value: [[1,0],[1,0]]}   # 3-in-line
      board_offset: default                          # same vector, grid domain
    bend: 3.54        # √2 · 2.5 — a pin reaches a hole diagonally
```

`bend = √2 · 2.5 ≈ 3.54` is the raster-cell diagonal — each pin reaches any of
the 8 neighbouring holes (including diagonals), which lets TO-92 be folded into a
**triangle** without a rigid per-pin state enumeration. If the triangle were to
be the default, we would give the second vector explicitly:
`board_offset: [[1,1],[1,-1]]`.

### 3.4 DIP-8 (multi-row, rigid)

**Rigid legs (zero bending)** and a **multi-row topology**. No solver, no
required value.

```yaml
packages:
  - name: prism_dil                  # DIP generic — rigid
    extends: prism
    leads:
      direction:    [0, 0, -1]       # one vector → all pins down
      board_offset: default          # ≡ body, latched
    bend: 0.0                          # rigid = zero slack

  - name: DIP-8
    extends: prism_dil
    dims: {width: 9.8, length: 10.2, height: 4.0}
    leads:
      body_offset:
        unit: 2.5
        value: [[3,[0,1]], [3,0], [3,[0,-1]]]   # run-length: 3×↓, jump, 3×↑
```

The vector `[3,0]` is the jump to the second column (row spacing 3 holes ≈ 0.3").
`bend: 0.0` = rigid → `board_offset` latched to `default`. Reproduces the
existing `DualRow` (`rows_spacing=3`).

### 3.5 Devboard (module, dims from pins + margin)

A dev-kit module (ESP32, WeAct STM32) is trivial in the new model:
`shape: prism`, `direction: [0,0,-1]`, `body_offset` as vectors "where the next
pin goes". The only novelty is the **PCB dimension computed from the pins**,
not entered by hand:

```yaml
packages:
  - name: WeAct_STM32G474
    extends: prism_dil                # rigid, direction down
    margin: [0, 2.5]                  # top/bottom 0 (cut along the pin axis),
                                       # left/right 2.5 mm — CSS 2 values (V H)
    leads:
      board_offset:                    # 2 SIL rows, row spacing = module width
        value: [[0,1],[0,1], ... ,[W,0], ... ,[0,-1],[0,-1], ...]
    # dims computed: bbox(pins)→mm + margin
```

The PCB `width/length` = bbox of pin centres (holes→mm, ×2.5) **+ margin** (mm);
`height` from the datasheet or an explicit override. `margin: [0, 2.5]` — CSS 2
values (vertical | horizontal); `0` on the pin side = PCB cut along the line of
the outermost pin centres. Multiple rows = a longer `board_offset` list with more
`[W,0]` jumps — no structural difference from DIP.

---

## 4. YAML syntax

### 4.1 Location and loading

Package definitions live in `*.yaml` files loaded by the same mechanism as
components: the builtin directory (`assets/packages/`) plus user directories from
`component_library_dirs` (earlier ones win). User-parameterized variants are
saved to their library.

### 4.2 Package fields

| field | meaning |
|---|---|
| `name` | package identifier |
| `abstract: true` | a base, not for direct use (e.g. `cylinder`) |
| `extends:` | the package to inherit from and close parameters against |
| `shape:` | `cylinder` \| `prism` \| `disc` |
| `dims:` | body dimensions in **mm** (`diameter`/`length`/`edge_radius` or `width`/`length`/`height`/`corner_radius`). For `prism` **optional** — when `margin` is given, computed from the pin bbox |
| `margin:` | (prism, optional) PCB margin in **mm** from the outermost pin centres; present → `dims` from bbox(pins)+margin. CSS convention: 1 \| 2 (V·H) \| 3 (T·H·B) \| 4 (T·R·B·L) |
| `leads:` | lead geometry (below) |
| `bend:` | bendability — one slack radius in **holes**, per package |
| `kind:` | a procedural body generator sized from the pin count (§6) |
| `schema:` | parameter-field declarations (§5) |

The `leads` block:

| field | domain / unit | meaning |
|---|---|---|
| `direction` | — | list of 3D vectors per pin (axial `[[0,0,-1],[0,0,1]]`, radial `[[0,0,-1],[0,0,-1]]`) |
| `body_offset` | body, own `unit` | list of N−1 vectors; **run-length** `[count,[x,y]]` or bare `[x,y]` (sugar: scalar/vector for 1 pair). Form: `{unit: U, value: [...]}` or a bare list |
| `board_offset` | grid, **holes** (fixed raster 2.5) | list of vectors (a different **shape** than body) **or** `default` (same vector as body) **or** `solve` (axial solver) |
| `lead_length` | mm | `null` = filled in the GUI; the axial solver input |
| `lead_r` | mm | lead-wire radius (solver) |
| `body_eccentricity` | mm | eccentricity — a **2D vector**; scalar `s` = sugar `[s,0]`; default `[0,0]` |
| `body_axis_pos` | grid, **holes** | the **alternative** absolute eccentricity notation; excludes `body_eccentricity`; not allowed with `board_offset: solve` |

`bend` lives in the board domain → counted in holes.

### 4.3 Generics (abstract, builtin)

```yaml
packages:
  - name: cylinder
    abstract: true
    shape: cylinder
    dims: {diameter: null, length: null, edge_radius: 0.0}

  - name: cylinder_axial            # axial resistor, diode
    extends: cylinder
    leads:
      direction:   [[0,0,-1], [0,0,1]]
      body_offset: 0                # → [[0,0]]
      board_offset: solve           # body=0 → solver
      lead_length: null             # mm, GUI/solver
      lead_r:      null             # mm
      body_eccentricity: 0.0

  - name: cylinder_radial           # electrolytic
    extends: cylinder
    leads:
      direction:    [[0,0,-1], [0,0,-1]]
      body_offset:  null            # required (with its own unit)
      board_offset: default         # := body_offset projected onto the grid
      lead_length:  null
    bend: 0.0                        # in holes
```

### 4.4 Named (closing a generic)

```yaml
  - name: DO-35
    extends: cylinder_axial
    dims: {diameter: 2.0, length: 4.0}
    leads: {lead_r: 0.27}           # board_offset still `solve`

  - name: TO-92
    extends: prism
    dims: {width: 4.5, length: 4.5, height: 5.2}
    leads:
      direction:   [[0,0,-1],[0,0,-1],[0,0,-1]]
      body_offset:  {unit: 1.25, value: [[1,0],[1,0]]}  # 1.25 mm between neighbours
      board_offset: default                              # → projection onto the 2.5 grid
    bend: 3.54                        # √2 hole
```

### 4.5 Composition in a component (HAS-A)

A component **points at** a package and closes the missing parameters. The
package fixes the **count and geometry** of pins (the length of the lists in
`leads`); `pin_names` belong to the **component** (names depend on the concrete
part, not the body). `step` (the discrete-step spinner) is declared here, closer
to the concrete part. The ways a component can point at a package (and their
precedence) are described in [../data-model.md](../data-model.md) §4.

```yaml
components:
  - name: C_POL
    component_class: capacitor_polar
    package:
      extends: cylinder_radial
      dims: {diameter: 6.3, length: 11.0}
      leads:
        body_offset: {unit: 2.5, value: [[1,0]]}   # 1 × 2.5 = 2.5 mm
      bend: 4
      step: 1
    pin_names: {1: "+", 2: "-"}
```

---

## 5. Parameter schema (`schema` / `auto`)

> **Implemented** (`package/schema.py`, `_apply_schema` in `package/loader.py`).
> The model agreed at the 2026-06-27 design session.

### 5.1 Problem

An abstract base package used to declare its dimension fields directly inside
`dims:` as bare values (`dims: {diameter: null}`, `null` = required). There was
no place for **field metadata** (type, default, required) *other than* the value
slot itself — and putting the description in that same slot broke `extends`
deep-merge: a child's `diameter: 5.0` replaced the `{type, default}` dict
wholesale, losing the schema exactly where it was needed. There was also no way
to express a **derived parameter** (a DIP body's length is a function of the pin
count, not a number to type).

### 5.2 The model — three separate concerns

**`schema` — field declaration, namespaced and path-mirrored.** A package
declares its fields under a top-level `schema:` whose **structure mirrors the
value tree**: a field at `dims.width` is described at `schema.dims.width`,
`leads.direction` at `schema.leads.direction`. A leaf is a dict:

```yaml
schema:
  dims:
    width:  {type: float, required: true}
    length: {type: float, required: true}
    radius: {type: [float, [float, float], [float, [float, float]]], default: 0.0}
```

| key | meaning |
|---|---|
| `type` | a single shape (`float`, `int`, `[float, float]`, …) or a **list of allowed shapes** (a union). A flat `[float, float, float]` is one 3-vector; a list of shape-tuples is a union |
| `default` | the value used when the instance omits the field |
| `required` | `true` ⇒ the instance (or an `extends` descendant) must supply a value. `required: true` with no `default` is the new spelling of today's `null` |

**Leaf vs section rule:** a node is a *field declaration* iff it contains a
`type:` key; otherwise it is a *section* (a branch grouping fields). One rule, no
name special-casing.

**Validation is loose** (decided 2026-06-27). The schema validates **only the
fields it declares**; a value under `dims`/`leads` with no matching leaf passes
untouched to `build_package` (`flat_radius`, `corner_radius`, `margin`, …). This
keeps the migration small at the cost of a silent gap (an unknown field is *not*
an error). Strict mode (unknown field ⇒ `PackageDefError`) is a possible later
tightening, tracked in [../todo.md](../todo.md), not done.

**Values — bare, under the mirrored path, in a separate subtree.** The instance
supplies values under `dims:` / `leads:` as today. Because `schema.dims.diameter`
(a dict) and `dims.diameter` (a float) are **different paths**, `extends`
deep-merge never collides them — the schema subtree merges with schema, the value
subtree with values, independently.

**`auto` — a parameter computed by a generator.** When a value should be
*computed*, not typed, the instance writes the sentinel `auto` in the value slot;
the **generator for that `kind`** (Python) resolves each `auto` to a concrete
number. There is deliberately **no expression language in YAML** — the value
stays pure data, the computation stays in testable Python. `required` is
satisfied by the *presence* of `auto`; the type check runs **after** the
generator (§5.4 step 5).

**Schema overrides, not just adds.** A descendant may **narrow** an inherited
field (change `default`, add `required: true`, tighten `type`) — this falls out
of deep-merge.

### 5.3 Editability — from the value, no flag

**The read-only rule** (2026-06-27): a field is **editable iff its resolved value
is `null`** (an authored hole the user must fill). Every other state is
read-only.

| resolved value | editability | why |
|---|---|---|
| `null` (required, no value) | **RW** | a hole the author left open |
| a concrete value from YAML | RO | the package declared it |
| a filled `default` | RO | a value like any other |
| `auto` | RO | a generator computes it |

Read-only needs **no stored flag** — it is a pure function of the value tree after
`extends` + `apply_to`. Interface: `ParamSchema.fields_view(values) ->
list[FieldView]`; each `FieldView` carries `{path, spec, value, status}` where
`status ∈ {REQUIRED_EMPTY, SET, AUTO}` and `FieldView.editable` = `status is
REQUIRED_EMPTY`.

**Value lifecycle — holes live in a kind, not in a placed part.** A package with
*holes* (unfilled required params) is a **`kind`**, not a concrete package:

```
kind (parameterized, has holes) → the PICKER fills the gaps → concrete package → geometry
```

A placed component always has a **concrete** package (the kind was resolved in
the picker before placement); the Properties panel of a *placed* part never sees
a `null` — every field is `SET` (RO). The editable (`REQUIRED_EMPTY`) state is
what the **picker** shows for a kind's holes. `build_package` **rejects** a `null`
required field (geometry needs a number) — correctly, because holes never reach
`build_package`; the picker/generator fills them first.

### 5.4 Loader — order of operations (the contract)

1. **Resolve `extends`** — deep-merge the `schema` and value subtrees
   independently.
2. **Apply `default`** — for every `schema` leaf without a value, fill from
   `default`.
3. **Check `required`** — every `required: true` leaf must have a value present
   (`auto` counts as present).
4. **Run the `kind` generator** — resolve every `auto` slot (a no-op without
   `auto`/`kind`).
5. **Validate `type`** — now that no `auto` remains, check each value against
   `schema.….type` (union-aware). The layer that did not exist before.
6. **Build the shape** — dispatch by `shape` into `Cylinder`/`Prism`/`Disc`.

Step 5-after-4 is the crux: `_require_float` never sees `auto`.

A declaration lives at the `extends` level that **introduces** the field:
`package` (root) → `schema.leads.direction`; `prism` →
`schema.dims.width/length/height`; `cylinder` → `schema.dims.diameter/length`;
a `kind` → its own new inputs (`schema.dims.pins_per_row`).

### 5.5 Lead schema (`leads.*`)

> **Implemented** (`schema.py`; declarations on the root base
> `assets/packages/generic.yaml`; `_normalise_body_offset` in the loader). The
> field formats are canonically in §2; this section adds the *schema
> declaration* layer.

The three `leads` fields (`direction`, `body_offset`, `board_offset`) have YAML
richer than the base `TypeSpec` matcher (which knows only a primitive and a
fixed-length vector + union). Three capabilities were missing — added as **four
orthogonal, additive** dict constructors (a plain shape never carries these
keys):

| constructor | meaning | example |
|---|---|---|
| **`{list: <shape>}`** (opt. `len`) | a list of any length, every element `<shape>` | `{list: [float, float, float]}` |
| **`{oneof: [...]}`** | matches any branch (a shape or a literal) | `{oneof: [[float,float], {seq: [int,[float,float]]}]}` |
| **`{const: <value>}`** | matches iff `value == <value>` (sentinels) | `{const: default}` |
| **`{seq: [...]}`** | a **positional** tuple: element *i* matches shape *i* | `{seq: [int, [float, float]]}` |

`{seq}` was essential: a run-length `[3, [1, 0]]` would not match
list-as-union — an ordered match of a pair with differing members was needed.

Declarations (as-implemented, on the root base, inherited via `extends`):

```yaml
schema:
  leads:
    direction:
      type:
        oneof:
          - [float, float, float]                          # broadcast
          - {list: [float, float, float]}                  # per-pin (len via loader)
          - {list: {seq: [{list: int}, [float, float, float]]}}  # grouped (advisory)
      required: true
    body_offset:
      type:
        oneof:
          - float                                          # scalar sugar (0)
          - {list: {oneof: [[float, float], {seq: [int, [float, float]]}]}}
      required: true
    board_offset:
      type:
        oneof:
          - {const: default}
          - {const: solve}
          - {list: {oneof: [[float, float], {seq: [int, [float, float]]}]}}
      default: default
```

**Pin-count-dependent length** (`direction#2`, the offset lists): declare
`{list: <shape>}` **without `len`** — the schema validates only the element
shape; arity is enforced by the loader (`expand_direction` / `expand_offsets`).
Schema and loader do not overlap (no double validation). The grouped
`direction#3` form is a **domain encoding**, not a shape — its index-coverage
invariant lives in `expand_direction`; the schema declares the outer structure as
**advisory**.

**The `{unit, value}` wrapper** (`body_offset` form 3) is a small *mapping*, not
a shape. The loader **normalizes before validation**: `_normalise_body_offset`
folds `{unit,value}` to a bare mm list *before* `schema.validate` (`loader.py`),
so the matcher stays shape-only and the `body_offset` declaration needs no wrapper
branch. `raw_values` keeps the authored wrapper for the Properties panel.

Verified against the **full shipped catalogue**: 132 package families build and
187 components load with zero schema-skips. Tests:
`tests/unit/test_package_schema.py`,
`tests/unit/test_package_loader.py::TestSchemaBodyOffsetUnwrap`.

### 5.6 Panels (schema consumers)

The Properties panel and the place picker are **consumers** of the schema — one
schema-driven field-list widget renders a row per `schema` leaf (label from the
field name, an editor by `type`, read-only when not `editable`).

- **Properties panel** (`gui/panels/properties_panel.py`) — **implemented**:
  source = the selected `ComponentInstance` → its resolved package → `schema` +
  values (`fields_view`); commit through the command path (undo/redo).
- **Place-tool package picker** — **deferred** (tracked in
  [../todo.md](../todo.md)): the same widget, editing a *pending* package spec
  instead of a committed instance.

---

## 6. Procedural `kind` (generators)

> **Implemented** (`package/generators/`: `PackageGenerator`,
> `KIND_GENERATORS`; `package/kind.py`: `build_kind_package` — the driver).

`kind:` in a `package:` block names a **parameterized body** — a C++-template
analogue: the kind is the template, the parameter set *{K}* (pin count, rows,
…) instantiates it. Builtins: `dip_ic` / `wdip_ic` (DIP narrow/wide), `sil`
(header), `dip_switch` (plain dual-row, no notch), `pin_header` / `jst`
(connectors). This is a **separate mechanism** from the YAML/families path
(§5) — a kind synthesises a whole `Package` from `pin_count` + parameters,
bypassing the `dims` parsing in `build_package`. A pure kind cannot be
rendered or assigned — only the generated concrete package can. The renderer
draws the declared look via the kind's registered renderer (the notch for a
DIP-IC — see [../rendering.md](../rendering.md) §4); an unknown kind falls
back to the shape-root renderer's guess.

**The generator contract** (`package/generators/base.py`) mirrors the renderer
registry: a `PackageGenerator` subclass declaring `generates = "<kind>"`
registers itself at class-creation time; later registrations win (a plugin in
a library's `generators/` directory overrides a builtin —
`services/library/plugin_discovery.py`). The subclass declares its parameter
set as a `ParamSchema` (`params`, one source of truth for build validation,
picker inputs and docs) and implements `generate(ctx) -> ctx`:

```python
@dataclass(frozen=True, slots=True)
class GeneratorContext:
    pin_count: int
    values: Mapping[str, object]        # default-filled by the driver
    package: Package | None = None      # the built body (output), XOR
    residual: ResidualKind | None = None  # parameters still to collect
```

`build_kind_package(kind, ctx, schema) -> GeneratorContext` is the driver:
default-fill + required/type checks against the generator's `params`, the
`generate` call, a totality guard, and the `kind` / `lineage` / colour /
schema stamp on the built body.

**Partial application (`ResidualKind`).** A component may fill only part of a
kind's required parameters: the driver then returns a *residual* — the kind +
the values bound so far — instead of failing. The library loader gives such a
component a generic-bbox placeholder (still `_generic` in lineage, so DRC /
bend consumers keep generic behaviour) carrying the pending kind + parameter
state; the place-tool picker collects the remaining holes ("editable iff
null", §5.3) and `build_variant` re-runs the generator, registering the
concrete result in the session catalogue. A generator may also return a
residual naming a **different kind** (a different parameter set) — a
*redirect*; the driver re-dispatches complete redirects (cycle-guarded) and
surfaces incomplete ones to the picker.

**Deferred** (tracked in [../todo.md](../todo.md)): the multi-step chain
driver, `auto`-resolution *through* generators, emitting `dims` from
generators, and partial kinds in `footprints:` option entries (today only the
component-level `package:` supports them).

### Two independent "inheritance" axes

They must be separated so the hierarchy is not brittle:

- **data** — YAML `extends:` merges parameters (DO-35 closes `cylinder_axial`).
  This is a dict merge.
- **behaviour** — the `shape:` field selects *which class* (`Cylinder`/`Prism`/
  `Disc`) animates the data. This is **dispatch by `shape`** (a registry), **not**
  subclassing of the `extends` chain.

Adding an intermediate named package (another `extends` link) must not change
which implementation handles geometry — only `shape` decides.

---

## 7. Architecture: the `Package` interface

All packages derive from one root that **enforces the presence of required
fields** in descendant definitions. A definition that leaves `null` until
instantiation is rejected at load — the "`null` = required" rule enforced at one
validation point.

`Package` defines an **interface** returning geometry derived from the data;
`Cylinder`/`Prism`/`Disc` (`package/shapes.py`) **implement** it, computing the
result from `dims` + `leads` (the axial `board_offset` solver is the body of
`Cylinder.pin_positions()` in the axial variant). The YAML stays a pure data
description; all geometry lives in the implementation.

```python
class Package(ABC):
    def pin_positions(self) -> list[Point]: ...     # board_offset expanded
    def collision_map(self) -> CollisionShape: ...  # body projected onto the grid
    def bbox(self) -> Rect: ...
    def pin_count(self) -> int: ...
```
