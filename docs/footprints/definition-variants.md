# Component and package definition variants

Library YAML has three top-level sections: `packages:` contains reusable
geometry templates, `families:` exposes named package variants, and
`components:` describes logical parts and the packages they explicitly offer.

## Components and MetaPins

Every fixed component declares `meta_pins`. A MetaPin is a logical terminal
with one or more labels; its list position is its stable ordinal.

```yaml
components:
  - name: ATmega328P
    component_class: ic
    meta_pins:
      - [PC6, RESET]
      - [PD0, RXD, PCINT16]
      - [PD1, TXD, PCINT17]
```

Geometry is offered through `package_options`. Each option owns the mapping
from MetaPin ordinals to physical package pins. One MetaPin may map to zero,
one, or many pins; a physical pin may occur in only one mapping.

```yaml
    package_options:
      - name: DIP-28
        ref: "@ic-tht/DIP/28"
        pin_map: {1: [1], 2: [2], 3: [3]}
      - name: TQFP-32
        ref: "@ic-smd/TQFP/32"
        pin_map: {1: [29], 2: [30], 3: [31]}
```

Only declared options appear in the picker. Equal pin counts do not make an
unrelated package eligible.

## Authoring references

- `@family/variant` selects an existing catalogue variant.
- `#package-name` materializes a named entry from `packages:`.
- `$kind` runs a registered package generator.

Prefixes exist only in YAML. Runtime references are canonical, unprefixed
catalogue addresses. Bare and unresolved references are rejected.

## Inline package sugar

A component may declare one `package:` instead of `package_options:`. The
loader registers it as `<component-name>-package/std` and creates the default
option. Put its mapping beside the package.

```yaml
components:
  - name: HEADER_4
    meta_pins: [VCC, GND, SDA, SCL]
    package: {kind: pin_header, rows: 1}
    pin_map: {1: [1], 2: [2], 3: [3], 4: [4]}
```

The two forms are mutually exclusive. A generator-backed `package:` without
authored `meta_pins` is a variable-count component; concretization creates
`P1`…`Pn` MetaPins and an identity mapping.

## Namespaced families

Family IDs may contain `/`; the final path segment remains the variant ID.

```yaml
families:
  - family: r-tht/DIN
    extends: cylinder_axial
    variants:
      - id: "0207"
        dims: {diameter: 2.5, length: 6.3}
```

The address is `r-tht/DIN/0207`. A family ID must not equal another family's
complete variant address.

## Removed schema

There is no backward-compatible parsing for component fields `pins`,
`pin_names`, `footprints`, or `package_family_ref`. Component-owned package
geometry and pinout variants are not part of the runtime model.
