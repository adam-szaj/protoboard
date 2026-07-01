# Data model

## 1. Units

```python
# protoboard/units.py
class RasterUnit(NewType("RasterUnit", float)):
    """Raster unit. 1.0 = one hole pitch (typically 2.54 mm)."""

class Millimeter(NewType("Millimeter", float)):
    """Absolute physical dimension."""
```

- Internally all positions are `RasterUnit` (float — *not* int; fractional and
  negative gaps are legal).
- Conversion `RasterUnit ↔ mm` goes through `Board.raster_pitch_mm` (default 2.54).
- The UI offers a display unit toggle: HP (raster units), mm, inches.

## 2. Board box model

A board is a tree of TeX-style boxes; layout is computed in `RasterUnit`.

```python
# protoboard/domain/board/boxes.py
class SizePolicy(Enum):
    FIXED = "fixed"      # explicit width / height
    HUG   = "hug"        # fit to contents
    FILL  = "fill"       # stretch to fill the parent

@dataclass(frozen=True, slots=True)
class Box:
    width_policy: SizePolicy
    height_policy: SizePolicy
    width: float | None  = None  # required when policy=FIXED
    height: float | None = None
    margin: Insets = Insets.zero()  # may be negative

@dataclass(frozen=True, slots=True)
class HBox(Box):
    children: list[Box]
    gap: float = 1.0  # fractional, negative and zero values are legal

@dataclass(frozen=True, slots=True)
class VBox(Box):
    children: list[Box]
    gap: float = 1.0

@dataclass(frozen=True, slots=True)
class BoardGrid(Box):
    cell: Box            # cell template
    rows: int
    cols: int
    row_gap: float = 1.0
    col_gap: float = 1.0

# Atoms (tree leaves):
@dataclass(frozen=True, slots=True)
class PadAtom(Box):
    """A single hole."""
    layers: PadLayers   # see §3
    label: str | None = None

@dataclass(frozen=True, slots=True)
class StripAtom(Box):
    """A continuous strip of N galvanically connected holes."""
    length: int | Literal["fill"]
    orientation: Literal["horizontal", "vertical"]
    layers: PadLayers
    cuttable: bool = True   # whether the strip may be cut
    isolated: bool = False  # True = the copper between this atom and its
                            # siblings has been cut/scraped out
    label: str | None = None

@dataclass(frozen=True, slots=True)
class GapAtom(Box):
    """Empty space — no pads."""
```

A **stripboard cut** is modelled by splitting the original `StripAtom` into
two atoms (the post-cut and the pre-cut segment) and rendering a small gap
between them. There is no separate `cuts: list[StripCut]` field on `Layout`
— cuts are part of the board tree.

**Layout engine (pure function):**

```python
def layout(box: Box, available: Size) -> LayoutResult:
    """Return the position of every atom in RasterUnit."""
```

Two-pass algorithm: (1) intrinsic-size pass — compute HUG sizes bottom-up;
(2) layout pass — distribute FILL space.

**Examples:**

```yaml
# 30 × 20 perfboard:
type: BoardGrid
rows: 20
cols: 30
row_gap: 1.0
col_gap: 1.0
cell:
  type: PadAtom
  layers: {top: true, bottom: true, plated_through: true}

# 30 × 20 stripboard:
type: VBox
gap: 1.0
children:
  - {type: StripAtom, length: 30, orientation: horizontal, layers: ...}
  # ... repeated 20 times (or via BoardGrid with StripAtom as the cell)

# breadboard with power rails (gap < 1 between rails, gap > 1 between blocks):
type: VBox
gap: 0.0
children:
  - {type: StripAtom, length: fill, orientation: horizontal, label: "+"}
  - {type: StripAtom, length: fill, orientation: horizontal, label: "−", margin: {top: -0.5}}
  - {type: GapAtom, height: 1.5}
  - {type: BoardGrid, rows: 5, cols: 30, cell: {type: StripAtom, length: 5, orientation: vertical}}
  # ...
```

## 3. Pads and layers

```python
@dataclass
class PadLayers:
    top: bool = True              # pad exists on the top layer
    bottom: bool = False          # pad exists on the bottom layer
    plated_through: bool = False  # galvanically connects top ↔ bottom
    # plated_through only makes sense when top=True and bottom=True
```

**Connectivity implications:**

- `top=True, bottom=False`: one node on the top layer.
- `top=True, bottom=True, plated=True`: one node (top and bottom are electrically
  the same).
- `top=True, bottom=True, plated=False`: **two independent nodes**; the user
  must connect them manually.

Layers are addressed by `LayerId` strings: `"top"`, `"bottom"`, and (future)
`"tape:top+1"`, `"tape:bottom+1"` for kapton-tape layers. The MVP only uses
`"top"` and `"bottom"`.

## 4. Components and footprints

Full geometry — including rotation, flip and bbox — is documented in
[geometry.md](geometry.md). The package model a definition *points at* (bodies,
leads, `kind`, schema) is in
[footprints/package-model.md](footprints/package-model.md); the YAML-authoring
guide in [footprints/authoring.md](footprints/authoring.md).

```python
@dataclass(frozen=True, slots=True)
class ComponentDefinition:
    name: str                          # "LM358"
    footprint: Footprint               # DIP(8) — topology + the default geometry
    pin_names: dict[int, str]          # {1: "OUT_A", 2: "IN-_A", ...}
    description: str = ""
    component_class: ComponentClass = ComponentClass.OTHER
    body_blocks_routing: bool = False  # SMD / tight-fit packages set this True
    # Package model (see footprints/package-model.md). Any of these may point at
    # the body used for placement geometry:
    package: Package | None = None            # inline parametric package
    package_family_ref: str | None = None     # "{family}/{variant_id}", catalogue ref
    footprints: tuple[FootprintOption, ...] = ()  # picker choices (first = default)
    symbol: str | None = None          # refdes prefix + picker label root
    variants: tuple[PinVariant, ...] = ()      # alt pin-name mappings
    appearance: Appearance | None = None       # visual-only override (colour/radius)
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True, slots=True)
class ComponentInstance:
    id: UUID
    definition_ref: str                # name from the library
    refdes: str                        # "U1", "R5"
    value: str                         # "LM358", "10k"
    package_variant: str | None = None # per-instance override of the family variant
    pin_variant: str | None = None     # per-instance pin-name variant
    appearance: Appearance | None = None
    # Live back-references, bound by `bind()`, NEVER serialized (see below):
    definition: ComponentDefinition | None = None
    families: dict[str, PackageFamily] | None = None
```

Library: a `library/` directory of YAML files (`opamps.yaml`,
`transistors.yaml`, `connectors.yaml`). Custom user library:
`~/.config/protoboard-designer/library/`.

### 4.1 Placement geometry — the source of truth (decided 2026-06-28)

A **placed** component always resolves its concrete package/footprint through its
*instance*, never through `ComponentDefinition.footprint`. `defn.footprint` is
the *default variant's* pin cloud, which is the wrong body whenever the instance
chose another variant (e.g. an `R` placed as `SMD-0805`: the default axial
3.2-hole pitch vs the SMD 1-hole pitch). Every "components drift / pin-pad
highlight off / reach ring wrong / can't place" bug in this area was the same
root: a consumer read `defn.footprint` instead of the resolved variant.

`defn.footprint` legitimately stays for **topology only**: `pin_count`,
`pin_names` validation, union-find membership, BOM type name — identical across
all variants.

**The mechanism — the instance carries its definition.** Rather than thread a
`families` catalogue through a dozen signatures, a placed instance holds a live
back-reference to the definition it was placed from and the package-family
catalogue. These fields are bound by `instance.bind(definition, families)` when
the instance enters a project (the same ritual as `build_pin_identities`) and are
**never serialized** — the loader reconstructs them from `definition_ref` + the
session catalogue. So the instance can resolve its own variant geometry:

- `instance.resolved_footprint()` — the single source of truth for "the footprint
  this placed component actually uses". Requires `bind()`; an **unbound** instance
  raises `UnboundInstanceError` (falling back to the definition default in a
  placement context is the exact silent-wrong-body bug this removes). When the
  instance *is* bound but the part declares no package/variant, `defn.footprint`
  is its genuine default geometry — not a fallback — so it is returned.

**Resolution precedence** (`resolve_component_package`, in
`domain/components/package/component_package.py`):

```
instance.package_variant  →  defn.package_family_ref  →  inline defn.package  →  defn.footprint (topology default)
```

The first three resolve to a real `Package` via the catalogue; the last is the
bare topology default used only when the part carries no package at all.

## 5. Schematic — the logical graph (netlist)

```python
@dataclass
class PinRef:
    component_id: UUID
    pin_number: int

@dataclass
class Net:
    id: UUID
    name: str                # "VCC", "N$12"
    pins: set[PinRef]
    color: str | None = None
    is_global: bool = False  # rails like VCC/GND — treated as automatically
                             # exported by every group

@dataclass
class Schematic:
    components: dict[UUID, ComponentInstance]
    nets: dict[UUID, Net]
    # No geometry — this is the pure requirements graph.
```

## 6. Layout — the physical realisation

```python
@dataclass
class ComponentPlacement:
    component_id: UUID
    position: Point          # world position of pin 1 (more precisely: of
                             # the component origin — see geometry.md)
    rotation: Literal[0, 90, 180, 270]
    side: Literal["top", "bottom"]
    pin_offsets: dict[int, Point] = field(default_factory=dict)
    shared_pins: frozenset[int] = field(default_factory=frozenset)
    label_offset: Point = field(default_factory=Point.origin)
    vertical_flipped: bool = False
    locked: bool = False     # ignored by drag / autoroute when True

@dataclass
class Layout:
    placements: dict[UUID, ComponentPlacement]
    wires: WireCollection          # dict-keyed wires (see §6.1)
    groups: dict[UUID, ComponentGroup]
    knots: dict[UUID, Knot]        # endpoint hubs — see §6.1
    pin_knots: dict[PinRef, UUID]  # fast PinRef → PinKnot lookup
```

`Layout` has no `cuts` field — strip cuts are encoded in the board tree via
`StripAtom.isolated=True` (see §2).

### 6.1 Wire model — `Wire`, `Fragment` and `Knot`

A wire is the atomic *artefact to be soldered*. One physical piece of
insulated or bare wire — even when it traverses two board sides through a
plated-through pad — is **one** `Wire` instance with multiple `Fragment`s.

```python
@dataclass(frozen=True, slots=True)
class Fragment:
    path: tuple[Point, ...]
    side: Literal["top", "bottom"]

@dataclass(frozen=True, slots=True)
class KnotRef:
    id: UUID

@dataclass
class Wire:
    id: UUID                          # stable id (survives path edits)
    a: KnotRef                        # endpoint 0
    b: KnotRef                        # endpoint 1
    fragments: list[Fragment]         # ≥ 1; fragment boundary = plated-through via
    insulated: bool                   # per-wire — one artefact = one value
    color: str | None = None
```

A **knot** is the shared end-point hub through which multiple wires terminate.
There are three concrete kinds:

```python
@dataclass
class PinKnot:
    id: UUID
    pin: PinRef                       # identity; position resolved lazily
    wire_ids: frozenset[UUID]

@dataclass
class JointKnot:
    id: UUID
    position: Point                   # must coincide with a pad (DRC: OFF_PAD_JOINT)
    wire_ids: frozenset[UUID]

@dataclass
class WirePinKnot:
    id: UUID
    position: Point                   # must coincide with a plated-through pad
    net_id: NetId | None              # snapshotted from the owning wire
    wire_ids: frozenset[UUID]         # exactly one
```

A `WirePinKnot` is a *virtual goldpin* — a deliberate solder terminal at a
wire endpoint, equivalent to soldering a 1×01 pin-header into the hole.
See [pad-pin-wire-junction-semantics.md](pad-pin-wire-junction-semantics.md)
for the full semantics.

**Invariants:**

- A `Wire` has exactly two endpoints. Branching is realised by `JointKnot`s.
- A fragment boundary appears **only** where the wire physically crosses from
  one side of the board to the other through a plated-through pad (a real via).
- `insulated` is per-wire: an insulated jumper that crosses through a via stays
  one artefact (one value).
- A `JointKnot.position` must coincide with a pad — off-pad joints are not
  allowed (`_check_off_pad_joint` in DRC).
- `WirePinKnot.position` must coincide with a plated-through pad (DRC:
  `WIRE_PIN_OFF_PLATED_THROUGH`).
- `Layout.add_wire` / `remove_wire` own the back-reference invariant:
  `Knot.wire_ids == {w.id for w in wires if knot in (w.a, w.b)}`.
- `Layout.replace_wire_fragments(wire_id, frags)` **preserves** `wire.id` — this
  is the fix that closed the segment-proliferation bug in commit `d33ea56`.

**Auto-collapse `JointKnot` (degree 2):** `Layout.try_collapse_joint(knot_id, …)`
collapses a joint into a single wire when both incident wires share the same
`insulated` value **and** either (a) the same `side` at the boundary, or
(b) the joint position is plated-through (the via is preserved as a fragment
boundary). If neither holds, the joint stays and the user can remove it
manually via "Simplify joints".

## 7. ConnectivityGraph

```python
class ConnectivityGraph:
    """The electrical graph derived from Board + Layout.
    Every node represents one electrically distinct point."""

    def __init__(self, board: Board, layout: Layout,
                 components: dict[UUID, ComponentDefinition]): ...

    def electrical_nets(self) -> list[set[PinRef]]: ...
    def shorts(self) -> list[Short]: ...
```

**DRC = compare `Schematic.nets` against `ConnectivityGraph.electrical_nets()`**.

The implementation is a union-find structure with rollback; see
[connectivity.md](connectivity.md) for details.

## 8. Pad conflicts and automatic net creation

### Placement / drag conflicts

`domain/layout/pad_conflict.py` — pure domain helpers:

- `find_conflicts()` — detects when a new component's pin lands on an
  existing component's pad.
- `find_wire_conflicts()` — detects when a new component's pin lands on a
  point of a `Wire.path`.
- `find_wires_crossing_pads()` — detects bare wires running through the new
  pad in a forbidden direction.
- `find_skewed_wires()` — detects wires that would lose their ortho/diagonal
  shape after the move (deprecated path; the unified drag model classifies
  them upstream).

`ConflictKind`:

| Value | Meaning | Action |
|---|---|---|
| `SAME_NET` | both pins on the same net | auto-OK |
| `DIFFERENT_NETS` | different nets | block |
| `ONE_UNCONNECTED` | one pin has no net | dialog |
| `BOTH_UNCONNECTED` | neither pin has a net | dialog — creates a new net |
| `PIN_ON_WIRE_SAME_NET` | pin and wire on the same net | auto-OK |
| `PIN_ON_WIRE_DIFFERENT_NET` | different nets | block |
| `PIN_ON_WIRE_WITH_NET` | wire has a net, pin doesn't (or vice versa) | auto-join, no dialog |
| `PIN_ON_WIRE_UNCONNECTED` | neither has a net | auto-create a new net |

`ComponentPlacement.shared_pins: frozenset[int]` records which pins were
deliberately shared with another component.

`gui/placement_conflict.py::check_and_resolve()` runs the domain helpers,
opens a `PadConflictDialog` if needed, mutates `session.schematic.nets`, and
returns the resolved `ComponentPlacement` (or `None` if blocked).

### Automatic net creation while drawing wires

`domain/schematic/wire_net_ops.py`:

- `find_pin_at(pt, session)` — returns `(comp_id, pin_num)` if the point
  lands on a pin (tolerance 1e-4).
- `classify_wire_nets(wire, session)` → `WireNetKind`.

`gui/wire_net_resolver.py::resolve_wire_nets(session, wire, cmd)` mutates the
schematic and records the diff via `cmd.record_net_change()` so that undo
restores both layout and schematic atomically.

**Flow:** `RouteTool._commit()` → `AddWireWithNetsCommand` →
`resolve_wire_nets()` → `stack.execute(cmd)`.

## 9. Component groups (`ComponentGroup`)

```python
@dataclass(frozen=True)
class ExportedPin:
    component_id: uuid.UUID
    pin_number: int

@dataclass(frozen=True)
class ComponentGroup:
    id: uuid.UUID
    name: str
    component_ids: frozenset[uuid.UUID]
    child_group_ids: frozenset[uuid.UUID]
    exported_pins: frozenset[ExportedPin]
    lock_internal_wires: bool = False
```

**Root group:** `ROOT_GROUP_ID = UUID("00000000-0000-0000-0000-000000000001")`.
Created by `Layout.ensure_root_group()`; not shown in `GroupsPanel`.

**GUI — `SelectTool._group_stack`:**

- `[ROOT_GROUP_ID]` — root level; everything accessible.
- `[ROOT_GROUP_ID, g1]` — inside g1; the rest is dimmed (alpha 0.2/0.45).
- Double-click → push; Esc → pop.

**Encapsulated `exported_pins`:** every `ExportedPin ep ∈ G.exported_pins`
must satisfy either
(a) `ep.component_id ∈ G.component_ids` (direct ownership), **or**
(b) `∃ child ∈ G.child_group_ids: ep ∈ child.exported_pins` (re-exported
through a direct child's interface).

`domain/layout/group.py::exposable_surface(groups, gid)` returns the legal
exportable set; `validate_exported_pins(groups, gid)` returns the invalid
entries. Both are consumed by the DRC check (`GROUP_INVALID_EXPORT`) and by
the persistence loader, which silently strips violations on load.

**`lock_internal_wires`:** when `True`, owned wires of the group are added
to the *hard* collision mask. A foreign drag that would touch them is HARD-blocked
instead of being resolved as SOFT (auto-remove). See
[drag-pipeline-architecture.md](drag-pipeline-architecture.md).

**Persistence:** `layout.yaml` serialises every group with `id`, `name`,
`component_ids`, `child_group_ids`, `exported_pins`, and
`lock_internal_wires`.
