# Authoring components and packages

## Mental model

A component describes electrical intent. A package describes physical
geometry. `PackageOption(ref, pin_map)` is the only runtime link between them.

- `MetaPin.labels` holds logical aliases such as `[15, PB3, MOSI]`.
- `PackageFamily` groups physical variants under a namespaced ID.
- `PackageOption.pin_map` assigns MetaPin ordinals to physical pin numbers.
- `ComponentInstance.package_variant` selects one offered canonical address.

## Minimal component

```yaml
components:
  - name: BC547
    component_class: transistor_npn
    symbol: Q
    meta_pins: [C, B, E]
    package_options:
      - name: TO-92
        ref: "@q-tht/TO-92/std"
        pin_map: {1: [1], 2: [2], 3: [3]}
```

Entries in `meta_pins` may be strings or non-empty label lists. A key in
`pin_map` is a 1-based MetaPin ordinal, not a physical pin number.

## Mapping rules

```yaml
meta_pins: [[GND, VSS], SIGNAL, NC]
package_options:
  - name: dual-ground
    ref: "@example/QFN/8"
    pin_map:
      1: [1, 5]
      2: [2]
      3: []
```

A MetaPin may map to zero, one, or many physical pins. Physical pins must be
within the selected package and may not be assigned to two MetaPins. When
`pin_map` is omitted, MetaPins map to physical pins 1:1 in list order. An
explicit mapping replaces that default entirely: unlisted MetaPins are
unmapped, and `pin_map: {}` leaves all MetaPins unmapped.

## Typed references

| Prefix | Meaning | Example |
| --- | --- | --- |
| `@` | existing family variant | `@chip-smd/imperial/0805` |
| `#` | named entry from `packages:` | `#my-module-body` |
| `$` | registered kind generator | `$sil` |

Quote these references in YAML. Prefixes are removed after resolution.
A concrete option permits only its own variant. A family option permits that
family's existing variants. Geometry, pin mapping, rendering and pre-placement
mechanics use the same option selector; an unrelated catalogue address is
rejected even when its geometry exists or its pin count matches.

## Picker groups

The picker path is `{group}/{name}`, with `group` defaulting to the authored
`ref` (including its prefix). Every `/` in either field creates another tree
level; the last segment is the selectable label. Technical package addresses
are unchanged. Inline options without `ref` or `group` use just `name`.

```yaml
package_options:
  - name: "5mm@7.2x2.5x6.5"
    ref: "$generic-prism"
    group: "Capacitors/Foil"
```

This creates `Capacitors → Foil → 5mm@7.2x2.5x6.5`. Without `group`,
all options using `$generic-prism` share the `$generic-prism` group.

## Inline and parametric packages

Use `package:` for one body. It normalizes to
`<component-name>-package/std` and cannot coexist with `package_options:`.
Every component must declare one of these forms. A missing package is an
error; the loader does not infer physical geometry from the number of MetaPins.
For an intentionally plain pin-cloud body, declare `package: {kind: generic}`
and its `pin_map` explicitly.

For a two-pin radial prism, `$generic-prism` reads the pitch and body dimensions
from the option name. The format is `<pitch>mm@<lx>x<ly>x<lz>`, with all numbers
in millimetres and the pitch a multiple of the 2.5 mm board raster. The loader
maps the two MetaPins to physical pins 1 and 2 in order by default:

```yaml
package_options:
  - {name: "5mm@7.2x2.5x6.5", ref: "$generic-prism"}
```

The generator uses the `prism_radial` geometry template. To specify other
geometry or a different pin mapping, put `package: {extends: prism_radial, ...}`
and `pin_map` inside the option. `ref: "@family/variant"` selects a finished
package; geometry fields beside `ref` are rejected.

C/C_POL radial packages remain rigid. Old component-level `bendable_pins` entries
do not supply mechanics; reach comes from the selected package.

```yaml
components:
  - name: HEADER_6
    meta_pins: [P1, P2, P3, P4, P5, P6]
    package: {kind: pin_header, rows: 2}
    pin_map: {1: [1], 2: [2], 3: [3], 4: [4], 5: [5], 6: [6]}
```

Omit `meta_pins` only for a genuinely variable-count parametric definition;
placement then materializes a concrete definition and identity mapping.

## Families, inheritance, and aliases

Use `/` to organize family namespaces. The picker renders namespace segments
as a tree and expands only explicitly referenced families. `extends:` copies a
component template. Child `meta_pins` and `package_options` replace the whole
parent collection; mappings are never deep-merged. `aliases:` creates
additional names for the resolved definition.

## Validation checklist

- Give every fixed component at least one MetaPin and package choice.
- Author an explicit mapping for every intended electrical connection.
- Ensure each physical pin belongs to at most one MetaPin.
- Run `uv run pytest tests/unit/test_library_loader.py -q` after schema edits.
- Do not add compatibility paths for removed YAML fields.
