<!-- SPDX-License-Identifier: MIT -->
# Domain data model

The entities of the domain (`src/protoboard/domain/`) and their relations,
faithful to the code. The core idea: the model is a graph of **logical nets**
(the schematic / netlist); the **physical view** is the projection of that graph
onto a grid of holes on the board.

Three conceptual groups:

- **Schematic** (logical) — `Schematic → Net → PinRef`. No geometry.
- **Layout** (physical) — `Placement → Instance → Definition → Footprint`,
  wires, knots, groups.
- **Board** (substrate) — a tree of boxes resolving to pad positions.
- **Connectivity** (projection) — union-find over pad points; `wire → net` map.

`PadIndex` and `ConnectivityGraph` are **runtime projections** built from
Board + Layout + Schematic, not persisted state (dashed relations below).

```mermaid
classDiagram
    direction TB

    %% ===== SCHEMATIC (logical netlist) =====
    class Schematic {
      +dict~UUID,Net~ nets
    }
    class Net {
      +UUID id
      +str name
      +set~PinRef~ pins
      +bool global_net
    }
    class PinRef {
      +UUID component_id
      +int pin_number
      +PinIdentity identity
    }
    class PinIdentity {
      +tuple key
      +UUID net_id
    }
    Schematic "1" *-- "many" Net : nets
    Net "1" o-- "many" PinRef : pins
    PinRef "many" --> "0..1" PinIdentity : shared same-name

    %% ===== COMPONENTS =====
    class ComponentInstance {
      +UUID id
      +str definition_ref
      +str refdes
      +str value
      +str package_variant
    }
    class ComponentDefinition {
      +str name
      +Package package
      +dict pin_names
      +str component_class
      +tuple footprints
    }
    class Footprint {
      +pin_positions() list~Point~
      +pin_count() int
      +flip_transform() Transform2D
    }
    ComponentInstance --> ComponentDefinition : definition_ref (by name)
    ComponentDefinition --> Footprint : default_footprint()
    PinRef --> ComponentInstance : component_id (by id)

    %% ===== LAYOUT (physical) =====
    class Layout {
      +PlacementCollection placements
      +WireCollection wires
      +dict~UUID,ComponentGroup~ groups
      +dict~UUID,Knot~ knots
      +dict~PinRef,UUID~ pin_knots
      +preview_copy() Layout
    }
    class ComponentPlacement {
      +UUID component_id
      +Point position
      +int rotation
      +Side side
      +bool vertical_flipped
      +bool locked
    }
    class Wire {
      +UUID id
      +list~Point~ path
      +bool insulated
      +list~Fragment~ fragments
      +KnotRef a
      +KnotRef b
    }
    class Fragment {
      +list~Point~ path
      +Side side
    }
    class KnotRef {
      +UUID id
    }
    class Knot {
      +UUID id
      +frozenset~UUID~ wire_ids
      +world_position(resolve_pin)
    }
    class PinKnot {
      +PinRef pin
    }
    class JointKnot {
      +Point position
    }
    class WirePinKnot {
      +Point position
      +UUID net_id
    }
    class ComponentGroup {
      +UUID id
      +frozenset~UUID~ component_ids
      +frozenset~UUID~ child_group_ids
      +frozenset~PinRef~ exported_pins
      +Transform2D transform
    }
    class GroupTree {
      +world_transform(cid)
      +would_create_cycle()
    }

    Layout "1" *-- "many" ComponentPlacement : placements
    Layout "1" *-- "many" Wire : wires
    Layout "1" *-- "many" Knot : knots
    Layout "1" *-- "many" ComponentGroup : groups
    Layout --> PinKnot : pin_knots[PinRef]
    ComponentPlacement --> ComponentInstance : component_id (by id)
    Wire "1" *-- "1..n" Fragment : fragments
    Wire "1" --> "2" KnotRef : a / b endpoints
    KnotRef --> Knot : id (in Layout.knots)
    Knot <|.. PinKnot
    Knot <|.. JointKnot
    Knot <|.. WirePinKnot
    Knot "1" --> "many" Wire : wire_ids (back-ref hub)
    PinKnot --> PinRef : pin
    ComponentGroup o-- "many" ComponentInstance : component_ids
    ComponentGroup o-- "many" ComponentGroup : child_group_ids (nesting)
    GroupTree ..> ComponentGroup : reads tree
    Layout ..> GroupTree : world_transform (compose)

    %% ===== BOARD =====
    class Board {
      +Box root
      +float raster_pitch_mm
      +pad_positions() list~Point~
      +atoms() Iterator
    }
    class Box {
      +SizePolicy width_policy
      +SizePolicy height_policy
    }
    class PadAtom {
      +PadLayers layers
    }
    class StripAtom {
      +length
      +PadLayers layers
      +bool isolated
    }
    class BoardGrid {
      +Box cell
      +int rows
      +int cols
    }
    class PadLayers {
      +bool top
      +bool bottom
      +bool plated_through
    }
    class Pad {
      +Point position
      +PadLayers layers
      +dict electrical_net
    }
    class PadIndex {
      +pad_at(pos) Pad
    }

    Board *-- Box : root (box tree)
    Box <|-- PadAtom
    Box <|-- StripAtom
    Box <|-- BoardGrid
    PadAtom o-- PadLayers
    BoardGrid o-- Box : cell
    Pad o-- PadLayers
    PadIndex "1" *-- "many" Pad : pads
    PadIndex ..> Board : atoms() → pads
    PadIndex ..> Layout : projects placements+wires
    PadIndex ..> Schematic : pin → net

    %% ===== CONNECTIVITY (projection) =====
    class ConnectivityGraph {
      +add_wire() / remove_wire()
      +node_for_pin(ref)
    }
    class build_wire_net_map {
      +wire.id → net.id
    }
    ConnectivityGraph ..> Board : build board nodes
    ConnectivityGraph ..> Layout : wires + pin positions
    ConnectivityGraph --> PinRef : pin_index
    build_wire_net_map ..> ConnectivityGraph : node_for_pin
    build_wire_net_map ..> Schematic : Net pins
    build_wire_net_map ..> Wire : contact points → Net
```

## Fidelity notes

- **`ComponentDefinition` has no `footprint` field** — the only stored geometry
  is `package: Package`; a `Footprint` is obtained via `default_footprint()` /
  `ComponentInstance.resolved_footprint()`. (Historical `defn.footprint` was
  removed 2026-07-04.)
- **`ComponentInstance.definition_ref` is a `str`** (the definition *name*), not
  a UUID — bound through a `dict[str, ComponentDefinition]` catalogue.
- **`Knot` is a `Protocol`** with three frozen implementers (`PinKnot` /
  `JointKnot` / `WirePinKnot`). `Wire.a`/`b` are `KnotRef` (id-only), resolved
  against `Layout.knots` — deliberately indirect.
- **`PadIndex` / `Pad` / `ConnectivityGraph` are runtime projections** (mutable,
  rebuilt from Board + Layout + Schematic), not part of the persisted model —
  shown with dashed dependency relations.
- **`PlacementCollection` composes group transforms on read**: value reads
  return the world placement (local folded through the ancestor-group
  transform); writes store the raw local. `Layout.preview_copy()` (E5) forks a
  scratch layout that copies placements / wire path+fragments / knots and shares
  `groups` + resolvers.
- **Frozen vs mutable**: `PinRef`, `KnotRef`, the three `Knot`s,
  `ComponentGroup`, `ComponentInstance`, `ComponentDefinition` are frozen;
  `Layout`, `ComponentPlacement`, `Wire`, `Fragment`, `Net`, `Schematic`, `Pad`
  are mutable (the runtime-edited state).
