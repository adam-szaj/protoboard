# MetaPin and Package Pin Mapping Design

## Goal

Separate a component's logical electrical terminals from the physical pin
numbers and geometry of a package. A component definition describes MetaPins;
each package option independently maps those MetaPins to its physical pins.
Changing a package variant must not change schematic connectivity.

Backward compatibility with the current component-library YAML is not
required.

## Domain Model

`MetaPin` is an immutable value object containing an ordered, non-empty tuple
of non-empty string labels. It has no number or identifier. Its 1-based
position in `ComponentDefinition.meta_pins` is its ordinal within that
component. Labels such as `("15", "PB3", "OC2", "PCINT3", "MOSI")` describe
one logical terminal with several equivalent names or functions.

`ComponentDefinition` replaces `pin_names` and `variants` with:

```python
meta_pins: tuple[MetaPin, ...]
```

`PinVariant` and `ComponentInstance.pin_variant` are removed. The definition
does not expose a physical `pin_count`; its logical cardinality is
`len(meta_pins)`.

`PackageOption` gains:

```python
pin_map: dict[int, tuple[int, ...]]
```

Keys are 1-based MetaPin ordinals. Values are physical pin numbers in the
referenced Package. An omitted MetaPin, or an explicitly empty tuple, has no
physical terminal in that package option. One MetaPin may map to multiple
physical pins. A physical pin may belong to at most one MetaPin.

## Identity and Connectivity

`PinRef` is the instance-level view of a MetaPin. Its numeric member continues
to be 1-based but means `meta_pin_index`, not a Package pin number. Renaming the
member is part of the migration so this distinction is visible in code and
serialized projects.

Schematic nets contain `PinRef(component_id, meta_pin_index)`. Internal
electrical identity is derived from the MetaPin, not from repeated labels.
Labels are descriptive and do not implicitly short two MetaPins together.

The active `PackageOption` supplies both the package reference and the mapping.
Physical interactions use two operations:

- MetaPin to physical pins: routing, pad-state creation, and rendering obtain
  every physical terminal for a logical connection.
- Physical pin to MetaPin: hit testing and route initiation use the validated
  inverse mapping. The result is zero or one MetaPin.

Unmapped physical pins remain mechanical/unconnected package terminals and do
not create a `PinRef`.

## Validation

Model construction rejects empty MetaPin label sets and empty labels. Loader
validation rejects MetaPin indices outside the definition, physical pin
numbers outside the resolved package, duplicate physical assignments, and
non-integer or non-positive indices.

Mapping validation occurs after the referenced package has been resolved, at
the library boundary. Runtime consumers operate on an already valid mapping
and do not duplicate defensive checks.

## YAML Shape

Component YAML uses ordered MetaPins and per-option mappings. For example:

```yaml
name: ArduinoNano
meta_pins:
  - [15, PB3, OC2, PCINT3, MOSI]
  - [GND]
package_options:
  - name: default
    ref: ArduinoNano-package/std
    pin_map:
      1: [15]
      2: [4, 29]
```

A scalar MetaPin entry may be accepted as shorthand for a single label if it
keeps the loader simpler for authored libraries. The normalized domain value
is always `tuple[str, ...]`.

Inline package definitions continue to be catalogued under the established
`<component-name>-package/std` address. Their `pin_map` remains on the
corresponding `PackageOption`, never on `Package` or `ComponentDefinition`.

## Migration Scope

The change updates the component model, library loader, package resolution,
parametric concretization, schematic identity, persistence, routing and DRC,
pad state, renderers, property panels, import/export paths, and fixtures.
Existing compatibility code for `pin_names`, pin variants, and implicit
same-name electrical grouping is removed rather than adapted.

Implementation proceeds test-first in vertical slices: model invariants and
loader normalization; mapping helpers; connectivity and persistence; geometry
consumers; GUI; then library YAML and the remaining test fixtures. Final
verification includes the complete unit and GUI suites, `mypy --strict`,
Pyright, formatting, and GUI smoke tests.

## Package References and Catalogue Structure

The component-to-package authoring model is extended without adding another
runtime representation. `ComponentDefinition.package_options` remains the only
stored set of package choices, and its first entry is the default. Every option
owns both its catalogue address and its `pin_map`; an instance may select only
an address declared directly by an option or a concrete variant expanded from
a family option. Every expanded variant uses the declaring option's `pin_map`.

Component YAML accepts three typed reference forms:

- `@family[/variant]` selects a package family or one concrete variant.
- `#named-package` builds a standalone entry from the package-definition
  registry.
- `$kind` invokes a package generator, including its parameter collection path.

The prefixes are authoring syntax only. The loader resolves or materializes the
reference and stores a canonical, unprefixed catalogue address in
`PackageOption.ref`. Runtime package resolution therefore remains independent
of YAML syntax and never stores inline `Package` geometry on either
`ComponentDefinition` or `PackageOption`.

The component-level `package:` form remains concise authoring sugar. The loader
builds it, registers it as `<component-name>-package/std`, and creates the
corresponding default `PackageOption`. Its top-level `pin_map` becomes that
option's mapping. Declaring both `package:` and `package_options:` is rejected;
authors must express all choices in `package_options`.

Unprefixed authored references are invalid. Unknown prefixes, unresolved
references, and stale catalogue addresses fail at the library boundary with an
actionable error. No aliases or persistence rename maps are provided.

## Namespaced Package Families

Package-family identifiers describe a real domain and standard series, for
example `r-tht/DIN`, `d-tht/DO`, or `chip-smd/imperial`. Variants represent
normalised physical sizes such as `0207`, `35`, or `0805`. The artificial
`S`/`typ`/`L` range around one nominal size is removed; migration retains the
old nominal (`typ`) geometry as the new size variant.

Family-level values provide shared defaults and variants may override them.
Catalogue validation rejects ambiguity where one family identifier equals a
variant address of another family. Family reference resolution chooses the
family default; a variant reference selects exactly that variant.

## Package Picker

The picker is driven exclusively by the active component's
`package_options`. It never searches the global catalogue for packages with a
matching physical or logical pin count. This prevents unrelated two-pin bodies
from being offered merely because their cardinality matches.

Options are displayed as a tree derived from the namespace segments of their
canonical catalogue addresses. A family reference expands to its variants; a
variant reference remains a leaf. Generated and inline families appear only
when the component declares or materializes them. Recent selections remain
leaves and must still correspond to a currently declared option.

## Extended Migration Order

The MetaPin migration is completed and verified before reference syntax or
catalogue addresses change. The follow-up proceeds in dependency order:

1. parse typed references and normalize them to `PackageOption`;
2. validate namespaced families and family-default resolution;
3. migrate catalogue assets, component YAML, fixtures, and saved test projects
   in one pass;
4. remove pin-count fallback and introduce the picker tree;
5. rewrite authoring and architecture documentation and run final repository
   verification.

At no point may these stages reintroduce `PackageFootprint`, `FootprintOption`,
`ComponentDefinition.package`, `pin_names`, or `PinVariant`.
