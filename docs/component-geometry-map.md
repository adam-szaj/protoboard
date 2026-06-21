# Component geometry & body-render — state map (read-only audit)

A cartography of how a component's **pin layout** and **body appearance** are
expressed and resolved today. Written because the model grew tangled: the same
words ("DIP", "SIL") mean several different things, and the *look* of a body is
**inferred from pin geometry** instead of being declared. This document only
describes **what is** (with `file:line`); it proposes nothing. It is the input to
a later decision about untangling.

> One-line diagnosis: *"DIP" denotes 6 different things across the layers, and the
> body silhouette (notch / housing) is **guessed from the pin-column count**, which
> conflates two independent axes — **where the pins are** (SIL/DIL = an offset
> pattern) vs **how the body looks** (notch vs plain).*

---

## 1. The two independent axes (that got mixed)

| Axis | What it means | Should depend on |
|---|---|---|
| **Pin layout** | Where the pads land on the grid. "SIL" = one in-line column, "DIL/DIP" = two columns. Ultimately a list of consecutive offset vectors. | the part's geometry data |
| **Body appearance** | The drawn outline + decorations: DIP notch, SIL housing, pin-1 marker, plain rectangle. | the part's *package type* (is it really a DIP chip?) |

Today these two are **coupled through pin-column geometry**: the renderer counts
pin columns and infers the body look from it (§5). So a 2-row relay — a *pin
layout* fact — forces a DIP *notch* — an *appearance* decision it never asked for.

---

## 2. "Pin layout" is expressed two parallel ways

There are **two systems** that both answer "where are the pins", both returning
`list[Point]` with pin 1 at `(0,0)`:

**(A) `FootprintTemplate` subclasses** — `domain/components/footprints.py`:
- `SingleRow(n, pitch)` (`:41`) — one column (SIL).
- `DualRow(n_per_row, pitch, rows_spacing)` (`:93`) — two columns (DIL/DIP).
- `TwoPin(pitch)` (`:65`), `GridFootprint(rows, cols)` (`:123`),
  `QuadRow(n_per_col, col_spacings)` (`:147`), `TO220Footprint(SingleRow)` (`:219`).
- Factories (not classes): `DIP(n)` → `DualRow` (`:207`); `SIL(n)` → `SingleRow`
  (`:214`); singletons `TO92 = SIL(3)`, `TO220` (`:226`).

**(B) `Package` + `Leads`** — `domain/components/package/shapes.py`:
- `Leads` (`:41`): `direction`, **`body_offset_mm`** (N−1 step vectors, mm),
  **`board_offset`** (N−1 steps in holes, or `"default"`/`"solve"`).
- `Prism`/`Cylinder`/`Disc` implement `Package.pin_positions()` by accumulating
  the offset steps (`offsets.py:cumulative_positions`).

**The bridge:** `PackageFootprint(package)` (`package/adapter.py:23`) wraps a
`Package` *as* a `FootprintTemplate`, so downstream code sees one interface — but
upstream there are two ways to say the same layout.

**SIL/DIL ≈ an offset pattern (confirmed in code).** `_step_offsets(fp)`
(`from_footprint.py:72`) turns any footprint's `pin_positions()` into the
consecutive-difference vectors; for a `DualRow` that is literally
`[3×[0,1], [3,0], 3×[0,-1]]` = "down a column, jump across, down the other". So a
`DualRow` footprint and a `Prism` with that `body_offset` describe the **same**
geometry — "DIL" is shorthand for that offset list, exactly as you said.

---

## 3. How a placed component actually gets its pins

`resolve_component_package(defn, instance, families)`
(`package/component_package.py:38`) precedence:
1. `instance.package_variant` (`"family/variant"`),
2. `defn.package_family_ref`,
3. `defn.package` (inline),
4. else `None`.

At load (`services/library/loader.py:~246`): if a package resolves, the footprint
**is** `PackageFootprint(package)`; otherwise the bare `footprint:` is parsed.
Either way, `pin_world_position(footprint, placement, n)`
(`domain/layout/placement.py:167`) calls `footprint.pin_positions()[n-1]`, then
applies per-pin bend offset → side mirror → rotation → anchor. One path for DRC,
routing and rendering. **The package wins over a bare footprint when present.**

---

## 4. The six faces of "DIP"

| # | "DIP" as… | Where |
|---|---|---|
| a | footprint **class** `DualRow` | `footprints.py:93` |
| b | footprint **factory** `DIP(n)` → `DualRow` | `footprints.py:207` |
| c | YAML footprint **type** `{type: DIP, pins: N}` (parser `_build_dip`) | `loader.py:~472` |
| d | generated **family** `dip-N` / `wdip-N` | `generated_families.py:66` |
| e | catalogued **package** `DIP-8` (extends `prism_dil`) | `assets/packages/generic.yaml` |
| f | render **strategy** `DipSilhouette` (the notch) | `prism_silhouette.py:~126` |

All six eventually become a `DualRow`-shaped pin cloud + a prism body — but the
word carries footprint, factory, YAML tag, family id, catalogue name, and render
strategy meanings at once. That overload is the core of the "everything is mixed"
feeling.

---

## 5. Body appearance is **inferred**, not declared (the crux)

`silhouette_for(prism)` (`gui/renderers/prism_silhouette.py:~168`) chooses the
body look **purely from the pin-column count** (`_columns()` clusters pins by x):

```
1 column, ≥3 pins   → SilSilhouette()      # rounded housing + pin-1 marker
2 columns, ≥2 each  → DipSilhouette(...)    # rectangle with a NOTCH + pin-1 marker
otherwise           → None                  # plain bbox rectangle
```

- A `Prism` is **pure geometry** — it has no `family_id`, no `is_dip`, nothing
  saying "I am a DIP" (`shapes.py:206`). So `silhouette_for` *cannot* know the
  package type; it can only guess from columns.
- Consequence: **any** 2-column part (a 2-row relay, a 2-row header) gets a DIP
  notch; **any** single column ≥3 gets a SIL housing. The look follows pin layout,
  not package identity.
- The one existing escape hatch is **class-based suppression**: in
  `PackageBody._draw_prism` (`body_shapes.py:~792`),
  `component_class == DEVBOARD` forces `sil = None` → plain bbox. That is the
  current (narrow) precedent for "let identity, not geometry, decide the look".

`PackageBody` is where richer context lives (it holds `component_class`), but the
prism it draws does **not** carry its footprint type or family id — so today the
only signal available to gate the silhouette is `component_class`.

---

## 6. `component_class` is a third, orthogonal axis that leaks into rendering

`ComponentClass` (`definition.py:16`) drives, beyond electrical identity:
- refdes prefix — `REFDES_PREFIX` (`definition.py:47`);
- TO-92 body — `_TRANSISTOR_CLASSES` × `SingleRow(n==3)` (`body_shapes.py:~328`,
  dispatch `~1048`);
- polarity marker — `_POLAR_CLASSES` × arrangement (`body_shapes.py:~362`);
- DIP/SIL silhouette **suppression** for `DEVBOARD` (`body_shapes.py:~792`);
- collision margin — `_FLEXIBLE_LEAD_CLASSES` (`placement.py:~27`);
- netlist label (`netlist_panel.py`).

So rendering reads from **all** of: component_class, footprint type, package
shape/arrangement, **and** pin-column geometry — see the crossing table below.

---

## 7. Body-render dispatch (`body_shape_for`, `body_shapes.py:~997`)

In order:
1. resolved **Package** → `PackageBody` (the main path: DIP, SIL, SMD, axial, …);
2. `TO220Footprint` → `TO220Body`;
3. `TwoPin`, no package → `GenericBody` (plain bbox);
4. `SingleRow(n==3)` + transistor class → `TO92Body`;
5. `DualRow`/`SingleRow`/`QuadRow`, no package → `prism_for_footprint(fp)` →
   `PackageBody` (synthesized prism);
6. else → `GenericBody`.

Inside `PackageBody.draw` (`body_shapes.py:~450`) a second tree picks
radial / cylinder / prism / smd-chip / bbox from `arrangement` + shape type +
pin count; the prism arm calls `silhouette_for` (§5).

---

## 8. Tangle map — which inputs each render decision reads

| Decision | component_class | footprint type | pkg shape/arrangement | pin-column geometry | family id |
|---|:--:|:--:|:--:|:--:|:--:|
| Package vs footprint fallback | – | ✓ | ✓ (presence) | – | ✓ |
| TO-92 body | ✓ | ✓ | – | – | – |
| TO-220 body | – | ✓ | – | – | – |
| radial vs cylinder vs prism | – | – | ✓ | – | – |
| polarity marker | ✓ | – | ✓ | – | – |
| **DIP notch / SIL housing** | ✓ (DEVBOARD only) | – | – | **✓** | – |
| dev-board plain bbox + rings | ✓ | – | – | ✓ | – |
| collision margin | ✓ | – | – | – | – |

The bold row is the one you hit: the silhouette is decided by **pin-column
geometry**, with a single `component_class` override bolted on for DEVBOARD.

---

## 9. Known doc drift (flagged, not fixed)

`docs/footprints/README.md` body-shape table still names the deleted
`SvgSILBody` / `SvgDIPBody` / `SvgQuadDIPBody` generators (replaced by
`PackageBody` + silhouettes). A header note already warns about it; the table
rows themselves are stale.

---

## 10. Summary of the entanglements (for the untangling decision)

1. **Two pin-layout systems** (FootprintTemplate vs Package/Leads) bridged by
   `PackageFootprint` — "DIL" exists both as a class and as an offset list.
2. **One word, six meanings** for "DIP" (§4).
3. **Appearance inferred from geometry** (§5): the notch/housing is chosen by
   counting pin columns, not by the part declaring its body type — so a relay or
   header inherits a DIP/SIL look it never asked for. The lone identity-based
   override is the DEVBOARD special case.
4. **component_class is a third axis** that also leaks into rendering (§6).

What a cleanup would need to separate: **(i)** pin layout = an offset pattern
(one representation, not two), **(ii)** body/appearance = a *declared* kind
(notch / housing / plain / …) owned by the package, not guessed from columns, and
**(iii)** component_class = electrical identity, kept out of the look except where
genuinely meaningful (polarity).
