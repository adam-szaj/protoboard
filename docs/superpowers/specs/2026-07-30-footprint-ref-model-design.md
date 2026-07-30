<!-- SPDX-License-Identifier: MIT -->
# Footprint reference model — one concept, prefixed refs, real families

**Date:** 2026-07-30 · **Status:** approved design, pre-implementation
**Supersedes** the component↔package linkage described in
[definition-variants.md](../../footprints/definition-variants.md) §2e/2f and the
package-family catalogue layout under `assets/package_families/`.

## 1. Problem

A component can currently obtain its body three field-level ways —
`package:` (inline / `kind:` / `extends:`), `footprints:` (picker options),
and `package_family_ref:` (catalogue binding) — each with its own loader
branch, precedence rung and documentation section, while the runtime model
already normalises all of them into `defn.package` + catalogue addresses.
The redundancy lives purely in the authoring surface and costs three parsing
paths, a three-rung precedence rule and six documented variants.

The package-family catalogue has the hierarchy inverted: each "family"
(DIN-0207, DO-41, …) is really **one standardised size**, its S/typ/L
"variants" are invented ranges around the norm, and the real family ("THT
resistors per DIN") is scattered across sibling entries related only by a
repeated `extends:`. Because families carry no domain axis, the picker's
fallback offers *every* 2-pin family to any 2-pin part (a resistor is offered
ceramic discs and relay bricks).

## 2. Decisions (approved 2026-07-30)

1. **`footprints:` is the only component-level concept.** First option =
   default geometry. `package:` stays in YAML as pure sugar the loader
   normalises to a one-element `footprints` list. `package_family_ref` is
   removed (hard error).
2. **`ref` is a universal geometry reference with a type prefix:**
   `@` family / family-variant, `#` standalone named package, `$` kind.
   Unprefixed refs are a hard error.
3. **A family is a flat table of standardised sizes.** `family_id` is
   namespaced (`r-tht/DIN`); a variant is **one** normalised size (`0207`);
   the S/typ/L pseudo-variant level is deleted. Family-level keys are shared
   defaults; a variant may override them (label, color, …).
4. **The picker is a tree** over the namespace segments of what the
   component's `footprints:` declare. The pin-count catalogue fallback
   ("offer all 2-pin families") is removed; the shipped library instead
   declares explicit family refs on R/C/D/LED parts. Families structure both
   the library and the picker.
5. **No backward compatibility.** No migration shims, no address-rename maps
   in persistence. Removed fields and stale addresses fail loudly with
   actionable errors; repo libraries/fixtures are rewritten in the same pass.
6. **Deferred:** generated families (`dip-N`/`wdip-N`/`sil-N`) do not join
   the namespace tree globally — they appear only where a component lists
   them; deeper `$kind` ↔ tree integration (offering a kind's generated
   families as variants) is a later stage (tracked in `docs/todo.md`).

## 3. Component model

```python
@dataclass(frozen=True, slots=True)
class FootprintOption:
    name: str
    ref: str | None = None        # "@family[/variant]" | "#package" | "$kind"
    package: Package | None = None  # inline geometry (exactly one of ref/package)
    style: Appearance | None = None
    implicit: bool = False        # synthesised from `package:` sugar
```

- `ComponentDefinition` loses `package_family_ref`. After load, `footprints`
  is **always non-empty**; `defn.package` (the baked default body — still
  needed for `pin_count`, topology validation, previews and ghosts) is the
  built geometry of `footprints[0]`. Invariant: `defn.package` ≡
  `footprints[0]` resolved.
- `implicit=True` marks the option the loader synthesised from a `package:`
  block. It carries today's "the author enumerated no choices" semantics for
  the picker, and is **not** registered in the inline-family catalogue, so a
  sugar-declared part places exactly as it does today (no instance variant
  recorded).

### Loader

- `package: {...}` → build the package exactly as today, wrap as
  `FootprintOption(name=<kind name if kind else "std">, package=pkg,
  implicit=True)`. Zero YAML migration for sugar users.
- `package:` **and** `footprints:` on one component → `SerializationError`
  ("give the inline body as the first footprints: option"). Currently legal
  but unused; removing the combination removes the last precedence rule.
- `package_family_ref:` present → `PackageDefError` with the migration hint
  (`use footprints: [{name: ..., ref: "@family[/variant]"}]`); one part is
  skipped, the library still loads (same pattern as the removed `footprint:`).
- The partially-parameterised-kind path (pending kind → provisional SIL
  footprint) is unchanged; the placeholder also becomes an implicit option.

### Runtime resolution

`resolve_component_package` drops its middle rung:

```
instance.package_variant  →  defn.package
```

Behaviour-preserving: the loader already refuses a component whose only
geometry ref does not resolve, so `defn.package` always equals the referenced
variant's package. Consumers of `package_family_ref` are repointed
(`bom.py`, `properties_panel.py`, `app.py`,
`generated_families._explicit_body` — the latter now reads `defn.package`,
which for a ref-declared part *is* the baked variant package).

`instance.package_variant` stores an **unprefixed family address**
(`family_id/variant_id`) — instances can only ever record a catalogue
choice, so the ref-type prefix is unnecessary there. A `$kind`
parameterisation materialises as today: the built variant registers in the
session catalogue and the instance stores that family address.

## 4. Reference syntax (`ref`)

| form | resolves in | meaning |
|---|---|---|
| `"@r-tht/DIN"` | family catalogue | family ref: default = family's `default` variant; the picker offers the whole family |
| `"@r-tht/DIN/0207"` | family catalogue | pinned variant |
| `"#my_module_base"` | `packages:` registry | exactly that named package; no choice. Closes the gap previously papered over by `package: {extends: name}` |
| `"$dip_ic"` | kind registry | parameterisable body; the picker dialog collects the kind's open parameters. Implementation-wise sugar over the existing inline pending-kind option path |
| anything unprefixed | — | hard `PackageDefError` with a hint |

**Address parsing** (after stripping `@`): try the whole string as a
`family_id` first (family ref); otherwise `rsplit("/", 1)` into
`(family_id, variant_id)` — variant ids must not contain `/`. The loader
validates that no `family_id` equals another family's variant address.

**Inline auto-registration rename:** loader-synthesised one-variant families
for explicit inline options move from `@Defn/option` ids (the `@` now means
"family ref") to an internal `inline/Defn/option` namespace; their address is
`"@inline/Defn/option/std"`. These ids are never hand-authored; repo fixtures
are updated in-pass.

## 5. Family model

`PackageFamily` stays a two-level dataclass (family → variants):

- `family_id` is namespaced with `/` segments (`r-tht/DIN`). The hierarchy
  lives in the id, not in new model classes.
- A variant is one standardised size (`0207`) with normative dims/leads.
  The S/typ/L level is deleted; each legacy family's `typ` dims become the
  single new variant's dims, S/L dims are dropped.
- Family-level keys (`extends`, `color`, `label`, `default`) are shared
  defaults; a **variant may override** them (needed e.g. if glass DO-35 and
  plastic DO-41 share a family). Families keep `extends`-ing generic
  packages (data merge); no kind-generator involvement is required.

## 6. Picker

- The picker renders a **tree** over the namespace segments of the families
  referenced by the component's `footprints:` (plus explicit inline options
  and `$kind` entries). Selecting a family node drills into its variants; a
  family ref preselects the family default, a pinned variant ref preselects
  that leaf.
- Any non-implicit option present → offer exactly the declared entries
  (today's rule, now tree-shaped).
- Only the implicit (sugar) option → today's no-footprints behaviour:
  a `kind` body surfaces one parameterisable entry (address `""`); a plain
  inline body places with no choice; `_generic` is never offered.
- The 2-pin catalogue fallback in `offered_body_families` is **removed**.
  Shipped R/C/D/LED components gain explicit `footprints:` family refs —
  the curated offer replaces the pin-count heuristic. Kind-based bodies keep
  their generator-driven alternatives (`dip-16`/`wdip-16`) as today.

## 7. Catalogue and library migration (single pass, no compat)

Naming scheme: `<class>-<mount>/<series>/<size>`. The prefix names the
body's *primary* domain; cross-domain references stay legal (an inductor or
fuse may reference an `r-tht` body). Proposed mapping of the ~30 authored
families (exact ids reviewable during the migration commit):

| today (family × pseudo-variants) | target |
|---|---|
| DIN-0204/0207/0411/0617/0922 (axial_ceramic) | `r-tht/DIN/{0204,0207,0411,0617,0922}` |
| DO-15/DO-41/DO-201, SOD-27 (axial_plastic) | `d-tht/DO/{15,41,201}`, `d-tht/SOD/27` |
| DO-35 (axial_glass) | `d-tht/DO/35` |
| axial_electrolytic families | `c-tht/CAX/…` |
| radial_cylinder (electrolytics) | `c-tht/CEL/…` |
| radial_disc (ceramics) | `c-tht/DISC/…` |
| radial_brick (film boxes) | `c-tht/BOX/…` |
| radial_led | `led-tht/…` |
| TO-92, TO-220 | `q-tht/TO-92/std`, `q-tht/TO-220/std` (single-size standard = one-variant family) |
| smd_chip families | `chip-smd/imperial/{0402…1206}` (shared by R/C/D SMD parts) |
| HC-49 (crystals) | `xtal-tht/HC-49/std` |

Library components are rewritten in the same pass:

- 6 `package_family_ref` occurrences → `footprints: [{name: ..., ref: "@…"}]`
  with the **final** addresses (no intermediate step).
- Existing `footprints:` refs (`"DIN-0207/typ"` …) → `@`-prefixed final
  addresses.
- R/C/D/LED parts gain their curated family-ref lists (replacing the removed
  2-pin fallback).

Repo fixture projects and tests are updated in-pass. External projects
carrying stale addresses fail loudly at load with the unresolved address in
the error; **no rename map is shipped**.

## 8. Implementation phases (one branch, library touched once)

1. **Component model** — `FootprintOption.implicit`, sugar normalisation,
   `package_family_ref` removal + error, `package:`+`footprints:` error,
   resolution simplification, consumer repointing. Fixtures only.
2. **Ref syntax** — `@`/`#`/`$` parsing, unprefixed-ref error, `#package`
   resolution against the `packages:` registry, `$kind` → pending-kind path,
   inline-family id rename.
3. **Family model** — namespaced ids, family-ref (default-variant)
   resolution, variant-level overrides, family/variant address-collision
   validation.
4. **Catalogue + library migration** — one commit rewriting
   `assets/package_families/*` to the new tree and `library/**` to final
   refs; drop S/L pseudo-variants.
5. **Picker tree** — tree UI over namespace segments; remove the 2-pin
   fallback; curated refs already in place from phase 4.
6. **Docs** — rewrite [definition-variants.md](../../footprints/definition-variants.md)
   (six variants → sugar + full form), [package-model.md](../../footprints/package-model.md)
   family sections, [data-model.md](../../data-model.md) §4 (two-rung
   precedence), [authoring.md](../../footprints/authoring.md),
   [gui.md](../../gui.md) (picker tree).

Each phase lands with green CI; behaviour tests are **repointed before** old
paths are deleted (parity rule from CLAUDE.md §8).

## 9. Testing

- Parity: existing `package_family_ref` behaviour tests migrate to the
  equivalent `footprints:` form (not deleted with the field); picker
  behaviour tests guard the implicit-option semantics; family resolution
  tests move to the new addresses.
- New: ref-prefix parsing (`@`/`#`/`$`, unprefixed error), family-ref vs
  variant-ref resolution, family/variant address-collision validation,
  sugar normalisation (`package:` ≡ one implicit option; both-fields error),
  `package_family_ref` migration error, tree-picker offer content
  (declared entries only; no cross-domain noise for a 2-pin part).

## 10. Out of scope / deferred

- Generated families joining the namespace tree; `$kind` offering its
  generated families as tree variants (→ `docs/todo.md` with date + commit).
- Strict-mode schema validation, kind/extends unification — untouched.
