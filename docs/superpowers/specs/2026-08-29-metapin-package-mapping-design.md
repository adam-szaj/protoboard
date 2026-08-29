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
