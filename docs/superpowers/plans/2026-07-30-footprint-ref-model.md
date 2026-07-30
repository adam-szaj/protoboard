<!-- SPDX-License-Identifier: MIT -->
# Footprint Reference Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce the component→package linkage to a single concept (`footprints:`, first option = default) with prefixed refs (`@family`, `#package`, `$kind`), rebuild the family catalogue as namespaced size tables, and turn the picker into a tree — per the approved spec `docs/superpowers/specs/2026-07-30-footprint-ref-model-design.md`.

**Architecture:** All authoring variants normalise at load time into `defn.footprints` (+ the baked `defn.package` default). `#`/`$` refs materialise into inline-package options during `_parse_footprints`, so runtime resolution only ever sees `@`-family addresses and built packages. The catalogue/library YAML is rewritten in exactly one commit (Task 4); until then bare (unprefixed) refs keep resolving so every commit stays green.

**Tech Stack:** Python 3.11, `ruamel.yaml`, PySide6 (picker dialog only), pytest + pytest-qt.

## Global Constraints

- Branch: `feat/footprint-ref-model` (this worktree). Commit per task, Conventional Commits. **Never `git add -A`** — use `git add -u` + explicit new files, then check `git diff --cached --name-only`.
- `mypy --strict`, `ruff`, `black` must stay green. Domain (`src/protoboard/domain/`) must not import Qt.
- Test runs: `uv run pytest tests/unit tests/gui tests/property -q` (NEVER bare `tests/` — `tests/benchmarks` hangs). Single-file runs: `uv run pytest tests/unit/test_x.py -x -q`.
- **No backward compatibility** (user decision): no shims, no rename maps; removed fields / stale addresses fail loudly. Repo fixtures updated in-pass.
- **Parity rule (CLAUDE.md §8):** behaviour tests are repointed at the replacement *before* the old path is deleted — never deleted in the same breath.
- All docs/comments in English.
- Spec is the authority: `docs/superpowers/specs/2026-07-30-footprint-ref-model-design.md`.

---

### Task 1: `FootprintOption.implicit` + `package:` sugar normalisation

**Files:**
- Modify: `src/protoboard/domain/components/definition.py` (~line 170, `FootprintOption`)
- Modify: `src/protoboard/services/library/loader.py` (geometry chain ~722-759; `inline_option_families` ~388)
- Modify: `src/protoboard/gui/footprint_choice.py` (~line 144, `options()`)
- Test: `tests/unit/test_library_loader.py`
- Modify: `CHANGELOG.md` (new entry at top)

**Interfaces:**
- Consumes: existing `FootprintOption(name, ref, package, style)`, `_build_component_package`, `_pending_kind_placeholder`.
- Produces: `FootprintOption.implicit: bool = False` (keyword field, default False). Loader invariant: a component whose YAML declares `package:` loads with `footprints == (FootprintOption(name=<kind-name or "std">, package=<built>, implicit=True),)`. `package:` + `footprints:` together → `SerializationError`. (Pending-kind components keep today's empty-footprints path — their body has no concrete geometry yet.)

- [ ] **Step 1: CHANGELOG entry**

Add at the top of `CHANGELOG.md`:

```markdown
## Unreleased — footprint reference model
- `footprints:` is the only component→package concept; `package:` is loader sugar.
- Refs take type prefixes: `@family[/variant]`, `#package`, `$kind`.
- Package families become namespaced size tables (`r-tht/DIN/0207`); S/typ/L pseudo-variants removed.
- `package_family_ref` removed. Picker becomes a tree. No backward compatibility.
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/unit/test_library_loader.py` (reuse the file's existing tmp-library fixture helpers; if it has a `_write_lib(tmp_path, text)`-style helper, use it, otherwise write the YAML with `tmp_path` directly as below):

```python
class TestPackageSugar:
    def _load(self, tmp_path, body: str):
        (tmp_path / "lib.yaml").write_text(body, encoding="utf-8")
        from protoboard.services.library.loader import load_library
        return load_library([tmp_path], {})

    def test_package_block_becomes_implicit_option(self, tmp_path):
        lib = self._load(tmp_path, """
components:
  - name: U_DIP14
    component_class: ic
    pins: {count: 14}
    package: {kind: dip_ic}
    pin_names: {}
""")
        defn = lib.get("U_DIP14")
        assert len(defn.footprints) == 1
        opt = defn.footprints[0]
        assert opt.implicit is True
        assert opt.package is defn.package
        assert opt.name == "dip_ic"

    def test_plain_inline_package_option_named_std(self, tmp_path):
        lib = self._load(tmp_path, """
components:
  - name: MOD
    component_class: devboard
    package:
      shape: prism
      dims: {lx: 12.5, ly: 12.5, lz: 1.6}
      leads:
        direction: [0, 0, -1]
        body_offset: {unit: 2.5, value: [[1, 0]]}
        board_offset: default
      bend: 0
    pins: {count: 2}
    pin_names: {}
""")
        opt = lib.get("MOD").footprints[0]
        assert opt.implicit and opt.name == "std"

    def test_package_plus_footprints_is_error(self, tmp_path):
        from protoboard.errors import SerializationError
        with pytest.raises(SerializationError, match="both 'package:' and 'footprints:'"):
            self._load(tmp_path, """
components:
  - name: BAD
    component_class: ic
    pins: {count: 14}
    package: {kind: dip_ic}
    footprints:
      - {name: dip, ref: "dip-14/std"}
    pin_names: {}
""")

    def test_implicit_option_not_registered_as_inline_family(self, tmp_path):
        lib = self._load(tmp_path, """
components:
  - name: U_DIP14
    component_class: ic
    pins: {count: 14}
    package: {kind: dip_ic}
    pin_names: {}
""")
        from protoboard.services.library.loader import inline_option_families
        assert inline_option_families(lib) == {}
```

Note: if the loader raises a different concrete error type for the mixed case
(`PackageDefError`), match that — the assertion that matters is the loud failure.

- [ ] **Step 3: Run tests — expect FAIL**

Run: `uv run pytest tests/unit/test_library_loader.py::TestPackageSugar -x -q`
Expected: FAIL (`implicit` attribute missing / footprints empty).

- [ ] **Step 4: Implement**

`definition.py` — add the field to `FootprintOption` (after `style`):

```python
    style: Appearance | None = None
    # Synthesised by the loader from a component-level ``package:`` block
    # (sugar). Implicit options carry "the author enumerated no choices"
    # semantics: the picker treats them as the no-footprints case and they
    # are never registered as inline families.
    implicit: bool = False
```

`loader.py` — in the geometry chain (current lines ~722-735), wrap the built
inline package as an implicit option and reject mixing:

```python
    if package_obj is not None and footprints:
        raise SerializationError(
            f"{source}: component {data['name']!r} declares both 'package:' "
            f"and 'footprints:' — give the inline body as the first "
            f"footprints: option instead")
    if package_obj is not None:
        footprint = PackageFootprint(package_obj)
        sugar_name = getattr(package_obj, "kind", None) or "std"
        footprints = (FootprintOption(
            name=str(sugar_name), package=package_obj, implicit=True), )
    elif footprints:
        ...  # existing branch unchanged
```

Leave the `pending_kind` branch (provisional `SIL`) exactly as it is.

`loader.py::inline_option_families` — skip implicit options:

```python
        for option in defn.footprints:
            if option.package is None or option.implicit:
                continue
```

`gui/footprint_choice.py::options()` — key the explicit-choices branch off
non-implicit options (line ~144):

```python
        explicit = [o for o in defn.footprints if not o.implicit]
        if explicit:
            for opt in explicit:
                ...  # existing loop body unchanged, iterating `explicit`
```

- [ ] **Step 5: Run tests — expect PASS, then full suite**

Run: `uv run pytest tests/unit/test_library_loader.py -x -q` → PASS.
Run: `uv run pytest tests/unit tests/gui tests/property -q` → all green
(the sugar is behaviour-preserving; any failure here is a regression — fix
before proceeding, do not skip).
Run: `uv run mypy src/protoboard && uv run ruff check src tests` → clean.

- [ ] **Step 6: Commit**

```bash
git add -u && git diff --cached --name-only
git commit -m "feat(library): package: block normalises to an implicit footprints option"
```

---

### Task 2: Prefixed refs `@` / `#` / `$`

**Files:**
- Modify: `src/protoboard/domain/components/definition.py` (`FootprintOption`)
- Modify: `src/protoboard/services/library/loader.py` (`_parse_footprints` ~127-212, `_geometry_from_option` ~103-124, `inline_option_address` ~383, `inline_option_families` ~401)
- Modify: `src/protoboard/gui/footprint_choice.py` (address reads `opt.ref` → `opt.ref_address`)
- Test: `tests/unit/test_library_loader.py`, `tests/unit/test_footprints.py`

**Interfaces:**
- Consumes: `resolve_variant(families, address)` (already whole-string-family-first + `rpartition`), `resolve_extends(name, registry)`, `build_package(merged, name, lineage=...)`, `extends_chain(name, registry)` from `protoboard.domain.components.package.loader`; `_build_component_package(cname, raw, pin_count, generics)`.
- Produces: `FootprintOption.ref_address -> str | None` (ref with any `@#$` sigil stripped). Parse-time dispatch: `#name` → inline-package option built from the `packages:` registry; `$kind` → inline option via the kind machinery (built or pending placeholder), option name defaults to the bare address; `@addr` → stored as authored, resolved via `ref_address`. Bare refs still resolve (family semantics) — the hard error lands in Task 4. Inline family ids move `@{defn}/{opt}` → `inline/{defn}/{opt}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_library_loader.py`:

```python
class TestRefPrefixes:
    def _load(self, tmp_path, body: str, families=None):
        (tmp_path / "lib.yaml").write_text(body, encoding="utf-8")
        from protoboard.services.library.loader import load_library
        return load_library([tmp_path], families if families is not None else {})

    def test_at_ref_resolves_against_catalogue(self, tmp_path):
        lib = self._load(tmp_path, """
packages:
  - name: my_base
    extends: prism
    dims: {lx: 4.5, ly: 4.5, lz: 5.2}
    leads:
      direction: [0, 0, -1]
      body_offset: {unit: 2.5, value: [[1, 0]]}
      board_offset: default
    bend: 0

families:
  - family: q-tht/TO-92
    extends: my_base
    variants:
      - id: std

components:
  - name: Q1
    component_class: transistor
    pins: {count: 2}
    footprints:
      - {name: TO92, ref: "@q-tht/TO-92/std"}
    pin_names: {}
""")
        defn = lib.get("Q1")
        assert defn.footprints[0].ref == "@q-tht/TO-92/std"
        assert defn.footprints[0].ref_address == "q-tht/TO-92/std"
        assert defn.package is not None  # resolved through the catalogue

    def test_hash_ref_builds_named_package(self, tmp_path):
        lib = self._load(tmp_path, """
packages:
  - name: my_module_base
    extends: prism
    dims: {lx: 10.0, ly: 10.0, lz: 2.0}
    leads:
      direction: [0, 0, -1]
      body_offset: {unit: 2.5, value: [[1, 0]]}
      board_offset: default
    bend: 0

components:
  - name: MOD_A
    component_class: devboard
    pins: {count: 2}
    footprints:
      - {ref: "#my_module_base"}
    pin_names: {}
""")
        opt = lib.get("MOD_A").footprints[0]
        assert opt.ref is None and opt.package is not None
        assert opt.name == "my_module_base"
        assert not opt.implicit  # explicit author choice

    def test_dollar_ref_is_kind_option(self, tmp_path):
        lib = self._load(tmp_path, """
components:
  - name: U1
    component_class: ic
    pins: {count: 14}
    footprints:
      - {ref: "$dip_ic"}
    pin_names: {}
""")
        opt = lib.get("U1").footprints[0]
        assert opt.ref is None and opt.package is not None
        assert opt.name == "dip_ic"
        assert getattr(opt.package, "kind", None) == "dip_ic"

    def test_unknown_hash_ref_is_loud(self, tmp_path):
        from protoboard.errors import SerializationError
        with pytest.raises(SerializationError, match="#nope"):
            self._load(tmp_path, """
components:
  - name: BAD
    component_class: other
    pins: {count: 2}
    footprints:
      - {ref: "#nope"}
    pin_names: {}
""")

    def test_inline_family_ids_use_inline_namespace(self, tmp_path):
        lib = self._load(tmp_path, """
components:
  - name: R
    component_class: resistor
    pins: {count: 2}
    footprints:
      - name: SMD
        package:
          shape: prism
          dims: {lx: 2.0, ly: 1.25, lz: 0.6}
          leads:
            direction: [0, 0, -1]
            body_offset: {unit: 2.5, value: [[1, 0]]}
            board_offset: default
          bend: 0
    pin_names: {}
""")
        from protoboard.services.library.loader import (
            inline_option_address, inline_option_families)
        assert inline_option_address("R", "SMD") == "inline/R/SMD/std"
        fams = inline_option_families(lib)
        assert set(fams) == {"inline/R/SMD"}
```

(If the prism dims spelling in these fixtures needs `width/length/height`
instead of `lx/ly/lz` on this branch, copy the spelling used by the existing
passing fixtures in this test file — the axis-geometry branch renamed them.)

- [ ] **Step 2: Run — expect FAIL**

Run: `uv run pytest tests/unit/test_library_loader.py::TestRefPrefixes -x -q`
Expected: FAIL (`ref_address` missing; `#`/`$` unhandled; `@` id scheme).

- [ ] **Step 3: Implement**

`definition.py` — on `FootprintOption`:

```python
    @property
    def ref_address(self) -> str | None:
        """The catalogue address of ``ref`` — the ``@``/``#``/``$`` sigil
        stripped. ``None`` for inline options."""
        if self.ref is None:
            return None
        return self.ref[1:] if self.ref[:1] in "@#$" else self.ref
```

`loader.py` — a module-level helper + dispatch inside `_parse_footprints`.
Replace the current `elif entry.get("ref") is not None:` handling (and the
string-entry case) so both funnel through one dispatcher:

```python
def _named_package(
    cname: str, bare: str,
    generics: dict[str, dict[str, Any]] | None, source: Path,
) -> Package:
    """Build the standalone ``packages:`` entry *bare* for a ``#`` ref."""
    if not generics or bare not in generics:
        raise SerializationError(
            f"{source}: {cname!r}: unknown package ref '#{bare}'")
    from protoboard.domain.components.package.loader import (
        build_package, extends_chain, resolve_extends)
    merged = resolve_extends(bare, generics)
    return build_package(merged, bare,
                         lineage=tuple(extends_chain(bare, generics)))
```

Inside the entry loop, after `ref`/`name_raw` are read (both the plain-string
and the mapping form set `ref`), before constructing the option:

```python
        if ref is not None and ref[:1] == "#":
            bare = ref[1:]
            package = _named_package(cname, bare, generics, source)
            ref = None
            if name_raw in (None, f"#{bare}"):
                name_raw = bare
        elif ref is not None and ref[:1] == "$":
            bare = ref[1:]
            built = _build_component_package(cname, {"kind": bare},
                                             pin_count, generics)
            if isinstance(built, ResidualKind):
                if not pin_count:
                    raise SerializationError(
                        f"{source}: '$' ref for {cname!r} leaves kind "
                        f"{built.kind!r} parameters unfilled and the "
                        f"component declares no pin count")
                package = _pending_kind_placeholder(built, SIL(n=pin_count))
            else:
                package = built
            ref = None
            if name_raw in (None, f"${bare}"):
                name_raw = bare
        # '@'-prefixed and (temporarily) bare refs stay as authored; they
        # resolve via FootprintOption.ref_address. The bare form becomes a
        # hard error in the catalogue-migration commit.
```

`_geometry_from_option` — resolve through the stripped address:

```python
    if option.ref is not None:
        variant = resolve_variant(families, option.ref_address or "")
```

`inline_option_address` / `inline_option_families` — new id scheme:

```python
def inline_option_address(defn_name: str, option_name: str) -> str:
    return f"inline/{defn_name}/{option_name}/{_INLINE_VARIANT}"
...
            family_id = f"inline/{name}/{option.name}"
```

`gui/footprint_choice.py` — every place reading `opt.ref` as an address uses
`opt.ref_address` instead (`options()` line ~147: `addr = opt.ref_address`,
and `_ref_is_kind(opt.ref_address)`).

- [ ] **Step 4: Sweep old `@`-address assumptions**

Run: `grep -rn '"@' src/protoboard tests --include='*.py' | grep -v 'ref: "@'`
and `grep -rn '@R/\|@Defn\|f"@{' src tests`. Update any test or fixture
asserting the old `@{defn}/{option}` inline ids to `inline/{defn}/{option}`
(known candidates: `tests/unit/test_library_loader.py`,
`tests/unit/test_recent_footprints.py`, `tests/gui/` picker tests). This is
the no-back-compat rename — do not add an alias.

- [ ] **Step 5: Run — expect PASS, then full suite + linters**

Run: `uv run pytest tests/unit/test_library_loader.py tests/unit/test_footprints.py tests/unit/test_recent_footprints.py -q` → PASS.
Run: `uv run pytest tests/unit tests/gui tests/property -q && uv run mypy src/protoboard && uv run ruff check src tests` → green.

- [ ] **Step 6: Commit**

```bash
git add -u && git diff --cached --name-only
git commit -m "feat(library): prefixed footprint refs — @family, #package, \$kind"
```

---

### Task 3: Namespaced families — collision validation + family-ref regression tests

**Files:**
- Modify: `src/protoboard/services/library/package_family_loader.py`
- Test: `tests/unit/test_package_family.py`, `tests/unit/test_package_family_loader.py`

**Interfaces:**
- Consumes: `PackageFamily`, `resolve_variant` (no change needed — verified: whole-string family match first, then `rpartition("/", 1)`; family-level keys already inherit into variants with per-variant override in `parse_family`).
- Produces: `validate_catalogue(families: dict[str, PackageFamily]) -> dict[str, PackageFamily]` — returns the dict with any family whose `family_id` equals another family's variant address dropped + warned (`package_family.address_collision`). Called from `load_full_package_catalogue`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_package_family_loader.py` (build families with the
file's existing helper for constructing a `PackageFamily` with a dummy
package; if none exists, build via `parse_family` on a minimal entry as the
other tests in the file do):

```python
class TestNamespacedCatalogue:
    def test_family_ref_resolves_default_with_namespaced_id(self, two_variant_family):
        # two_variant_family: family_id "r-tht/DIN", variants "0204","0207",
        # default "0207" — construct with the file's package-building helper.
        from protoboard.domain.components.package.family import resolve_variant
        fams = {two_variant_family.family_id: two_variant_family}
        assert resolve_variant(fams, "r-tht/DIN").variant_id == "0207"
        assert resolve_variant(fams, "r-tht/DIN/0204").variant_id == "0204"

    def test_family_id_colliding_with_variant_address_is_dropped(
            self, two_variant_family, make_family):
        # make_family("r-tht/DIN/0204", ["x"]) collides with the 0204 variant
        # address of "r-tht/DIN".
        from protoboard.services.library.package_family_loader import (
            validate_catalogue)
        clash = make_family("r-tht/DIN/0204", ["x"])
        out = validate_catalogue({
            two_variant_family.family_id: two_variant_family,
            clash.family_id: clash,
        })
        assert "r-tht/DIN/0204" not in out
        assert "r-tht/DIN" in out
```

Write the two fixtures (`two_variant_family`, `make_family`) in the same test
class/file using the cheapest real package the file already builds (e.g. via
`parse_family` with `extends: cylinder_axial` and dims per variant).

- [ ] **Step 2: Run — expect FAIL** (`validate_catalogue` missing).

Run: `uv run pytest tests/unit/test_package_family_loader.py -x -q`

- [ ] **Step 3: Implement**

`package_family_loader.py`:

```python
def validate_catalogue(
    families: dict[str, PackageFamily], ) -> dict[str, PackageFamily]:
    """Drop families whose id shadows another family's variant address.

    ``resolve_variant`` matches a whole address as a family id first, so a
    family named ``"X/v"`` would silently shadow variant ``v`` of family
    ``X``. Ambiguity is an authoring error: the shadowing family is dropped
    with a ``package_family.address_collision`` warning.
    """
    out: dict[str, PackageFamily] = {}
    for fid, fam in families.items():
        head, sep, tail = fid.rpartition("/")
        other = families.get(head) if sep else None
        if other is not None and other.get_variant(tail) is not None:
            _log.warning("package_family.address_collision",
                         family_id=fid, shadows=f"{head}/{tail}")
            continue
        out[fid] = fam
    return out
```

Wire it into `load_full_package_catalogue` (wrap the merged dict):

```python
    return validate_catalogue({
        **generate_inline_families(),
        **load_package_families(directory),
    })
```

- [ ] **Step 4: Run — PASS, full suite, linters** (same commands as Task 2 Step 5).

- [ ] **Step 5: Commit**

```bash
git add -u && git diff --cached --name-only
git commit -m "feat(families): namespaced ids — catalogue address-collision validation"
```

---

### Task 4: Catalogue + library single-pass migration; bare refs become a hard error

**Files:**
- Rewrite: `assets/package_families/*.yaml` (11 files → domain-named files)
- Modify: `library/Passive/resistors.yaml`, `library/Passive/inductors.yaml`, `library/Passive/fuses.yaml`, `library/Diodes/zener.yaml`, `library/Diodes/leds.yaml`, `library/Transistors/generic.yaml`, `library/Crystals/crystals.yaml`, `library/ICs/regulators.yaml` + every other `library/**` file with `footprints:` or `package_family_ref` (enumerate: `grep -rln 'footprints:\|package_family_ref' library/`)
- Modify: `src/protoboard/services/library/loader.py` (bare-ref hard error)
- Test: fixtures under `tests/` referencing old addresses; new loader error test

**Interfaces:**
- Consumes: everything from Tasks 1-3.
- Produces: the final catalogue tree; every library ref `@`-prefixed at final addresses; `_parse_footprints` raises `SerializationError` on an unprefixed ref.

- [ ] **Step 1: Build the migration map**

The complete old→new address map. For every NEW variant, copy the dims/leads
of the old family's **`typ`** variant (or its only variant) **verbatim** from
the old asset file; family-level keys (`extends`, `color`, arrangement, edge)
move to the new family level; per-variant colour differences become
variant-level overrides (already supported by `parse_family` inheritance).
S/L pseudo-variants are dropped entirely.

| old family (file) | new family / variant |
|---|---|
| DIN-0204, DIN-0207, DIN-0411, DIN-0617, DIN-0922 (`axial_ceramic.yaml`) | `r-tht/DIN` / `0204`, `0207`, `0411`, `0617`, `0922` (default `0207`) |
| DO-15, DO-41, DO-201 (`axial_plastic.yaml`) | `d-tht/DO` / `15`, `41`, `201` |
| SOD-27 (`axial_plastic.yaml`) | `d-tht/SOD` / `27` |
| DO-35 (`axial_glass.yaml`) | `d-tht/DO` / `35` (variant-level colour override — glass vs plastic) |
| families in `axial_electrolytic.yaml` | `c-tht/CAX` / one variant per old family |
| families in `radial_cylinder.yaml` | `c-tht/CEL` / one variant per old family |
| families in `radial_disc.yaml` | `c-tht/DISC` / one variant per old family |
| families in `radial_brick.yaml` | `c-tht/BOX` / one variant per old family |
| family in `radial_led.yaml` | `led-tht/<series>` / sizes (inspect the file: if variants are 3mm/5mm sizes keep them as variants of one `led-tht/round` family) |
| TO-92 (`to92.yaml`) | `q-tht/TO-92` / `std` |
| TO-220 (`to220.yaml`) | `q-tht/TO-220` / `std` |
| families in `smd_chip.yaml` | `chip-smd/imperial` / `0402`…`1206` (one variant per old chip family) |
| HC-49 (lives in `library/Crystals/crystals.yaml` `families:` section — verify with `grep -rn "HC-49" library/ assets/`) | `xtal-tht/HC-49` / `std` |

Rules for collapsing: where the old family had S/typ/L, the new variant is the
`typ` data under the size-derived id; where the old file held N sibling
families of one domain, they fuse into ONE new family with N variants. If a
fused family's members had different `extends` targets or lead structures
(check before fusing!), keep them as separate families under the same
namespace (`c-tht/CEL-6.3x11` style is NOT allowed — instead make the
distinguishing trait the variant, or split the series segment, e.g.
`c-tht/CEL` vs `c-tht/CEL-BIPOLAR`). Record any judgment call in the commit
message body.

- [ ] **Step 2: Rewrite the asset files**

New files (delete the 11 old ones): `r_tht.yaml`, `d_tht.yaml`, `c_tht.yaml`,
`led_tht.yaml`, `q_tht.yaml`, `chip_smd.yaml` under
`assets/package_families/`. Worked example — `r_tht.yaml` (dims/leads copied
verbatim from today's `axial_ceramic.yaml` `typ` variants):

```yaml
# SPDX-License-Identifier: MIT
# THT resistor bodies — DIN sizes, one variant per normalised size.
families:
  - family: r-tht/DIN
    extends: cylinder_axial
    label: "THT resistors (DIN)"
    default: "0207"
    variants:
      - id: "0204"
        label: "DIN 0204 (1.6 x 3.6 mm)"
        dims: {diameter: 1.6, length: 3.6}      # ← copy exact typ values
        leads: {lead_r: 0.2, lead_length: 28.0}  # ← from axial_ceramic.yaml
      - id: "0207"
        label: "DIN 0207 (2.5 x 6.3 mm)"
        dims: {diameter: 2.5, length: 6.3}
        leads: {lead_r: 0.27, lead_length: 32.0}
      # … 0411, 0617, 0922 likewise
```

(The dims/labels above are illustrative of the SHAPE — the executor copies
the actual `typ` numbers from the old files, which are the source of truth.
Family-level `color:`/`edge_radius` etc. from the old files carry over the
same way.)

- [ ] **Step 3: Rewrite library refs (final addresses, curated lists)**

Enumerate: `grep -rn 'footprints:\|package_family_ref\|ref:' library/`.
Transformations:

- `package_family_ref: "DO-35/typ"` → 
  ```yaml
  footprints:
    - {name: DO-35, ref: "@d-tht/DO/35"}
  ```
  (all 6 occurrences: crystals → `"@xtal-tht/HC-49/std"`, inductors →
  `"@r-tht/DIN/0207"`, fuses → `"@r-tht/DIN/0617"`, zener ×2 →
  `"@d-tht/DO/35"` / `"@d-tht/DO/41"`, regulators TO-92 →
  `"@q-tht/TO-92/std"`).
- Existing `footprints:` refs → `@` + final address, e.g. `resistors.yaml`:
  ```yaml
  footprints:
    - {name: DIN, ref: "@r-tht/DIN"}            # family ref → tree offers sizes
    - {name: SMD, ref: "@chip-smd/imperial/0805", style: "color: #222"}
  ```
- Curated family refs for the parts that relied on the 2-pin fallback
  (R, C, D, LED — the fallback itself dies in Task 6, the curation lands
  now): each gets the family refs of its plausible bodies, e.g. C radial →
  `[{name: CEL, ref: "@c-tht/CEL"}, {name: DISC, ref: "@c-tht/DISC"}, {name: BOX, ref: "@c-tht/BOX"}]`.
- `library/Crystals/crystals.yaml` `families:` section: rename its family id
  to `xtal-tht/HC-49`, collapse variants per the rules.

- [ ] **Step 4: Flip bare refs to a hard error**

`loader.py`, in the `_parse_footprints` dispatcher from Task 2 — replace the
"temporarily stays as authored" bare branch:

```python
        elif ref is not None and ref[:1] != "@":
            raise SerializationError(
                f"{source}: {cname!r}: footprint ref {ref!r} has no type "
                f"prefix — use '@family[/variant]', '#package' or '$kind'")
```

Add the test to `tests/unit/test_library_loader.py::TestRefPrefixes`:

```python
    def test_bare_ref_is_a_hard_error(self, tmp_path):
        from protoboard.errors import SerializationError
        with pytest.raises(SerializationError, match="no type prefix"):
            self._load(tmp_path, """
components:
  - name: BAD
    component_class: resistor
    pins: {count: 2}
    footprints:
      - {ref: "DIN-0207/typ"}
    pin_names: {}
""")
```

- [ ] **Step 5: Sweep fixtures and tests to the new addresses**

Run: `grep -rn 'DIN-02\|DIN-04\|DIN-06\|DIN-09\|DO-35\|DO-41\|DO-15\|DO-201\|SOD-27\|HC-49\|TO-92/\|TO-220/\|SMD-08\|SMD-06\|SMD-04' tests/ src/protoboard --include='*.py' --include='*.yaml'`
and rewrite every address per the Step 1 map (known hotspots:
`tests/unit/test_load_stale_path_snap.py`,
`tests/unit/test_connectivity_variant_footprint.py`,
`tests/gui/test_body_shapes.py`, `tests/gui/test_panels.py`, fixture project
YAMLs under `tests/`). Stale addresses in saved fixture projects are simply
edited — no rename shim (user decision).

- [ ] **Step 6: Verify the catalogue loads whole**

Run:

```bash
uv run python -c "
from protoboard.services.library.package_family_loader import load_full_package_catalogue
from protoboard.services.library.loader import load_library
cat = load_full_package_catalogue()
assert 'r-tht/DIN' in cat and cat['r-tht/DIN'].default_variant().variant_id == '0207', sorted(cat)
lib = load_library.__wrapped__ if hasattr(load_library, '__wrapped__') else load_library
print(len(load_library([__import__('protoboard.paths', fromlist=['x']).BUILTIN_LIBRARY_DIR
      if hasattr(__import__('protoboard.paths', fromlist=['x']), 'BUILTIN_LIBRARY_DIR') else 'library']).names()), 'components')
"
```

(If the paths import differs, just run the existing loader smoke test:
`uv run pytest tests/unit/test_library_loader.py tests/unit/test_package_builtin_yaml.py -q` —
it loads the full shipped catalogue and library.)
Then: `uv run pytest tests/unit tests/gui tests/property -q` → green.
Then the app smoke: invoke the `run-protoboard-designer` skill flow if
feasible headless, or at minimum `uv run pytest tests/gui -q`.

- [ ] **Step 7: Commit**

```bash
git add -u
git add assets/package_families/r_tht.yaml assets/package_families/d_tht.yaml \
        assets/package_families/c_tht.yaml assets/package_families/led_tht.yaml \
        assets/package_families/q_tht.yaml assets/package_families/chip_smd.yaml
git diff --cached --name-only   # verify: old asset files deleted, new added, library rewritten
git commit -m "feat(catalogue)!: namespaced family tree — one family per domain, one variant per normalised size

Single-pass migration: assets/package_families rebuilt as r-tht/d-tht/c-tht/
led-tht/q-tht/chip-smd/xtal-tht namespaces; S/typ/L pseudo-variants dropped
(typ dims kept); all library refs @-prefixed at final addresses; bare refs
are now a hard error. No back-compat by design."
```

---

### Task 5: Remove `package_family_ref`

**Files:**
- Modify: `src/protoboard/domain/components/definition.py` (field ~253, `__post_init__` validation ~294-299)
- Modify: `src/protoboard/services/library/loader.py` (parse ~704-711, geometry-chain branch ~736-750, docstrings)
- Modify: `src/protoboard/domain/components/package/component_package.py` (`resolve_component_package`)
- Modify: `src/protoboard/domain/components/package/generated_families.py` (`_explicit_body` ~157-161)
- Modify: `src/protoboard/services/export/bom.py` (~37), `src/protoboard/gui/panels/properties_panel.py` (~661-669)
- Test: `tests/unit/test_component_package_resolve.py`, `tests/unit/test_library_loader.py`, `tests/unit/test_set_package_variant_command.py`, `tests/gui/test_panels.py`, `tests/gui/test_body_shapes.py`

**Interfaces:**
- Consumes: Task 4's library (no `package_family_ref` left in YAML).
- Produces: `resolve_component_package(defn, instance, families)` precedence = `instance.package_variant → defn.package`; loader raises `PackageDefError` on the field; `ComponentDefinition` has no `package_family_ref`.

- [ ] **Step 1: Repoint behaviour tests FIRST (parity — old code still present)**

In `tests/unit/test_component_package_resolve.py` and every other listed test
that constructs a `ComponentDefinition(package_family_ref="X/y")`: rewrite the
construction to the equivalent post-Task-4 form — a definition loaded (or
hand-built) whose `footprints=(FootprintOption(name=..., ref="@X/y"),)` and
whose `package` is the referenced variant's package (mirroring the loader
invariant). The assertions about *behaviour* (which body a placed/unplaced
component resolves) stay identical. Tests that specifically asserted the
**middle-rung live re-resolution** (defn-level ref consulted at resolve time)
assert the equivalent loader-level behaviour instead: loading a component
whose only geometry is `@family/variant` bakes that variant's package into
`defn.package`. Run the suite — must be green against the OLD code before
proceeding:

Run: `uv run pytest tests/unit/test_component_package_resolve.py tests/unit/test_set_package_variant_command.py tests/gui/test_panels.py tests/gui/test_body_shapes.py -q` → PASS.

Commit the repoint alone:

```bash
git add -u && git commit -m "test: repoint package_family_ref behaviour tests at the footprints form"
```

- [ ] **Step 2: Write the failing migration-error test**

```python
    def test_package_family_ref_is_a_hard_error(self, tmp_path):
        from protoboard.domain.components.package.loader import PackageDefError
        (tmp_path / "lib.yaml").write_text("""
components:
  - name: OLD
    component_class: diode
    pins: {count: 2}
    package_family_ref: "d-tht/DO/35"
    pin_names: {}
""", encoding="utf-8")
        from protoboard.services.library.loader import load_library
        lib = load_library([tmp_path], {})   # PackageDefError → part skipped
        assert lib.get_optional("OLD") is None if hasattr(lib, "get_optional") \
            else "OLD" not in lib.names()
```

Run: `uv run pytest tests/unit/test_library_loader.py -k package_family_ref -x -q` → FAIL.

- [ ] **Step 3: Delete the field and repoint consumers**

`loader.py` — replace the parse block (~704-711) and geometry-chain `elif`
(~736-750) with the loud error next to the existing `footprint:` check:

```python
    if "package_family_ref" in data:
        raise PackageDefError(
            f"{data['name']}: 'package_family_ref' was removed — use "
            f"footprints: [{{name: ..., ref: \"@family[/variant]\"}}]")
```

Drop `package_family_ref=...` from the `ComponentDefinition(...)` call
(~952) and the local parse variable.

`definition.py` — delete the field (~253) and its `__post_init__` block
(~294-299).

`component_package.py::resolve_component_package`:

```python
def resolve_component_package(
    defn: "ComponentDefinition",
    instance: "ComponentInstance | None",
    families: dict[str, PackageFamily],
) -> Package | None:
    """The concrete package for a placed component, by precedence.

    instance.package_variant → defn.package. A ``package_variant`` address
    that does not resolve in *families* falls through to the definition
    default (baked from the definition's own footprints at load).
    """
    if instance is not None and instance.package_variant:
        variant = resolve_variant(families, instance.package_variant)
        if variant is not None:
            return variant.package
    return defn.package
```

`generated_families.py::_explicit_body` — drop the ref branch (~157-161);
the function starts at the `is_generic_package(defn.package)` check (for a
ref-declared part `defn.package` IS the baked variant package).

`bom.py` (~37):

```python
        if defn is not None:
            first = defn.footprints[0] if defn.footprints else None
            fp_name = (inst.package_variant
                       or (first.ref_address if first is not None else None)
                       or type(defn.default_footprint()).__name__)
```

`properties_panel.py` (~661-669) — primary family from the declared refs:

```python
        refs = [o.ref_address for o in defn.footprints
                if o.ref is not None and o.ref_address is not None]
        primary_pkg_id: str | None = None
        for fam in offered:
            if any(r == fam.family_id or r.startswith(fam.family_id + "/")
                   for r in refs):
                primary_pkg_id = fam.family_id
                break
        ordered = sorted(offered, key=lambda f: f.family_id != primary_pkg_id)
```

Update the surrounding docstrings/comments that narrate the three-rung
precedence (`instance.py` ~45-47, `component_cmds.py` ~1221-1225,
`body_shapes.py` ~114-133, `reach.py` ~196-202, `app.py` ~167-169) to the
two-rung story.

- [ ] **Step 4: Run — PASS, full suite, linters**

Run: `uv run pytest tests/unit tests/gui tests/property -q && uv run mypy src/protoboard && uv run ruff check src tests` → green.
Also: `grep -rn "package_family_ref" src/ tests/ library/ assets/ docs/data-model.md` → only the loader error string and (until Task 7) docs prose remain.

- [ ] **Step 5: Commit**

```bash
git add -u && git diff --cached --name-only
git commit -m "feat(model)!: remove package_family_ref — footprints is the only linkage"
```

---

### Task 6: Picker tree + retire the 2-pin fallback

**Files:**
- Modify: `src/protoboard/domain/components/package/generated_families.py` (`offered_body_families` ~200-206)
- Modify: `src/protoboard/gui/footprint_choice.py` (`FootprintOptionView`, `options()`)
- Modify: `src/protoboard/gui/dialogs/footprint_picker_dialog.py` (list → tree)
- Test: `tests/unit/test_generated_families.py`, `tests/gui/test_footprint_picker.py` (create if absent; check `ls tests/gui | grep -i picker` first and extend the existing file if one exists)

**Interfaces:**
- Consumes: `defn.footprints` with `@` refs and `implicit`; `PackageFamily.variants`; existing `FootprintChoiceProvider` machinery (`_ref_is_kind`, `kind_params`, `build_variant`, recents).
- Produces: `FootprintOptionView(label, address, is_kind, group: tuple[str, ...] = ())` — `group` is the namespace path for tree placement (`("r-tht", "DIN")`), `()` = root. `offered_body_families` returns kind-generated alternatives (unchanged) OR the families named by the component's explicit refs — never a pin-count scan. `FootprintPickerDialog` renders a `QTreeWidget`; leaf activation semantics (Enter/Space/double-click, Alt/kind → `KindVariantDialog`, search filter, recents-first) unchanged.

- [ ] **Step 1: Write the failing domain test**

In `tests/unit/test_generated_families.py`:

```python
class TestDeclaredOffer:
    def test_offer_follows_declared_refs_not_pin_count(self, families_catalogue, defn_factory):
        # defn_factory builds a 2-pin defn with
        # footprints=(FootprintOption(name="DIN", ref="@r-tht/DIN"),)
        # families_catalogue holds r-tht/DIN plus an unrelated 2-pin family
        # c-tht/DISC — build both with the file's existing package helpers.
        from protoboard.domain.components.package.generated_families import (
            offered_body_families)
        defn = defn_factory(footprint_refs=["@r-tht/DIN"])
        offered = offered_body_families(defn, families_catalogue)
        assert [f.family_id for f in offered] == ["r-tht/DIN"]   # no DISC noise

    def test_no_refs_means_no_offer(self, families_catalogue, defn_factory):
        from protoboard.domain.components.package.generated_families import (
            offered_body_families)
        defn = defn_factory(footprint_refs=[])   # implicit-only part
        assert offered_body_families(defn, families_catalogue) == []
```

Run: `uv run pytest tests/unit/test_generated_families.py -x -q` → FAIL
(current code returns every 2-pin family).

- [ ] **Step 2: Implement `offered_body_families`**

Replace the `if defn.pin_count == 2:` block (~200-206) with:

```python
    out: list[PackageFamily] = []
    for opt in defn.footprints:
        if opt.implicit or opt.ref is None:
            continue
        addr = opt.ref_address or ""
        fam = families.get(addr)
        if fam is None:
            head, sep, _tail = addr.rpartition("/")
            fam = families.get(head) if sep else None
        if fam is not None and fam not in out:
            out.append(fam)
    return out
```

(The kind-generated branch above it stays untouched.) Run the test → PASS.
Run `tests/gui/test_panels.py` — the properties-panel offer now follows
declared refs; repoint any assertion that expected the 2-pin noise (parity:
the asserted behaviour changes BY SPEC decision 4 — cite the spec in the
test docstring).

- [ ] **Step 3: `FootprintOptionView.group` + provider expansion**

`footprint_choice.py` — extend the view dataclass:

```python
@dataclass(frozen=True)
class FootprintOptionView:
    label: str
    address: str
    is_kind: bool
    group: tuple[str, ...] = ()   # namespace path for tree placement
```

In `options()`, replace the explicit-options loop: a `@family` ref (whole
string matches a family id) expands to one leaf per variant grouped under the
family path; a `@family/variant` ref stays one leaf; inline/`$`-derived
options stay root-level:

```python
        explicit = [o for o in defn.footprints if not o.implicit]
        if explicit:
            for opt in explicit:
                if opt.ref is not None:
                    addr = opt.ref_address or ""
                    fam = self._families.get(addr)
                    if fam is not None:            # family ref → all variants
                        grp = tuple(fam.family_id.split("/"))
                        for v in fam.variants:
                            out.append(FootprintOptionView(
                                label=v.label or v.variant_id,
                                address=fam.address(v.variant_id),
                                is_kind=False, group=grp))
                        continue
                    out.append(FootprintOptionView(
                        label=f"{root}_{opt.name}", address=addr,
                        is_kind=self._ref_is_kind(addr),
                        group=tuple(addr.split("/")[:-1])))
                else:
                    addr = inline_option_address(defn.name, opt.name)
                    out.append(FootprintOptionView(
                        label=f"{root}_{opt.name}", address=addr,
                        is_kind=self._package_is_kind(opt.package)))
            return out
```

- [ ] **Step 4: Tree dialog**

`footprint_picker_dialog.py` — replace the `QListWidget` with a
`QTreeWidget`; behaviour contract unchanged (recents flat on top with ★,
first leaf pre-selected, search hides non-matching leaves and collapses empty
groups, Enter/Space/double-click confirm, kind/Alt → `KindVariantDialog`,
`chosen_address` on accept):

```python
from PySide6.QtWidgets import (QApplication, QDialog, QLineEdit, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        group_nodes: dict[tuple[str, ...], QTreeWidgetItem] = {}

        def _group_node(path: tuple[str, ...]) -> QTreeWidgetItem | None:
            if not path:
                return None
            node = group_nodes.get(path)
            if node is None:
                parent = _group_node(path[:-1])
                node = (QTreeWidgetItem(parent) if parent is not None
                        else QTreeWidgetItem(self._tree))
                node.setText(0, path[-1])
                node.setFlags(node.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                group_nodes[path] = node
            return node

        first_leaf: QTreeWidgetItem | None = None
        for view in recent:
            leaf = QTreeWidgetItem(self._tree)
            leaf.setText(0, f"★ {view.label}")
            leaf.setData(0, _VIEW_ROLE, view)
            first_leaf = first_leaf or leaf
        for view in options:
            parent = _group_node(view.group)
            leaf = (QTreeWidgetItem(parent) if parent is not None
                    else QTreeWidgetItem(self._tree))
            leaf.setText(0, view.label)
            leaf.setData(0, _VIEW_ROLE, view)
            first_leaf = first_leaf or leaf
        self._tree.expandAll()
        if first_leaf is not None:
            self._tree.setCurrentItem(first_leaf)
        self._tree.itemActivated.connect(self._confirm_item)
        self._tree.itemDoubleClicked.connect(self._confirm_item)
        layout.addWidget(self._tree)
```

`_confirm_item(item, column=0)`: ignore group nodes
(`item.data(0, _VIEW_ROLE) is None` → `item.setExpanded(not item.isExpanded()); return`),
otherwise today's logic on the stored view. `_on_search`: walk leaves
(`QTreeWidgetItemIterator`), hide non-matching, hide a group node when all
its children are hidden, select the first visible leaf. Space in
`keyPressEvent` confirms `currentItem()` as today.

- [ ] **Step 5: GUI test**

`tests/gui/test_footprint_picker.py` (pytest-qt; model the setup on the
existing dialog tests in `tests/gui/`):

```python
def test_family_ref_renders_tree_and_returns_variant(qtbot, families_catalogue, defn_with_din_ref):
    from protoboard.gui.footprint_choice import FootprintChoiceProvider
    from protoboard.gui.dialogs.footprint_picker_dialog import FootprintPickerDialog
    provider = FootprintChoiceProvider(families_catalogue)
    options = provider.options(defn_with_din_ref)
    assert {o.group for o in options} == {("r-tht", "DIN")}
    dlg = FootprintPickerDialog(provider, recent=[], options=options)
    qtbot.addWidget(dlg)
    leaves = [dlg._tree.topLevelItem(0).child(i).text(0)
              for i in range(dlg._tree.topLevelItem(0).childCount())]
    # one group node "r-tht" → child "DIN" → variant leaves; adjust indexing
    # to the two-level group nesting and assert every variant id appears.
```

Run: `uv run pytest tests/gui/test_footprint_picker.py tests/gui -q` → PASS.

- [ ] **Step 6: Full suite, linters, smoke**

`uv run pytest tests/unit tests/gui tests/property -q && uv run mypy src/protoboard && uv run ruff check src tests`.
Then run the app headless via the `run-protoboard-designer` skill and
screenshot a placement flow with the R component (tree picker visible).

- [ ] **Step 7: Commit**

```bash
git add -u && git add tests/gui/test_footprint_picker.py
git diff --cached --name-only
git commit -m "feat(gui): footprint picker tree over family namespaces; retire 2-pin offer fallback"
```

---

### Task 7: Documentation

**Files:**
- Modify: `docs/footprints/definition-variants.md`, `docs/footprints/package-model.md`, `docs/footprints/authoring.md`, `docs/data-model.md` (§4), `docs/gui.md`, `docs/todo.md`, `CLAUDE.md` (§7 open questions if touched)

**Interfaces:** none (prose only, English).

- [ ] **Step 1: Rewrite the authoring story**

- `definition-variants.md`: §2's six variants collapse to **two spellings** —
  `package:` (sugar, §2a-2d examples kept but explicitly labelled sugar for a
  one-element `footprints`) and `footprints:` (full form) with the `@`/`#`/`$`
  ref table from the spec §4. Delete §2f. Update §6 precedence rules.
- `package-model.md`: §4.5 composition + the family sections describe
  namespaced families (one family per domain, one variant per normalised
  size, variant-level overrides); update the `resolve_component_package`
  reference to the two-rung precedence.
- `data-model.md` §4: `ComponentDefinition` snippet loses
  `package_family_ref`, gains the `implicit` note; precedence block becomes
  `instance.package_variant → defn.package (baked from footprints[0])`.
- `authoring.md`: ref syntax table + a worked family-authoring example
  (`r-tht/DIN`).
- `gui.md`: picker = tree; note the removed 2-pin fallback.

- [ ] **Step 2: Track the deferred work**

Append to `docs/todo.md` (dated 2026-07-30, referencing this branch):

```markdown
- **2026-07-30 (feat/footprint-ref-model):** generated families
  (`dip-N`/`wdip-N`/`sil-N`) do not join the family namespace tree; a `$kind`
  ref does not yet offer its generated families as tree variants. Deferred by
  spec decision 6 (docs/superpowers/specs/2026-07-30-footprint-ref-model-design.md).
```

- [ ] **Step 3: Verify + commit**

`grep -rn "package_family_ref" docs/` → only historical/spec/archive mentions.
`uv run pytest tests/unit -q` (docs-only change; cheap sanity).

```bash
git add -u && git diff --cached --name-only
git commit -m "docs: footprint reference model — two spellings, prefixed refs, family tree"
git push
```

---

## Self-Review (done at write time)

- **Spec coverage:** decisions 1-2 → Tasks 1-2+5; decision 3 → Tasks 3-4;
  decision 4 → Tasks 4 (curation) + 6 (tree, fallback removal); decision 5 →
  Tasks 2 (no alias on inline rename), 4 (fixture edits, bare-ref error), 5
  (hard error); decision 6 → Task 7 todo entry. Spec §3 loader/invariant →
  Task 1; §4 parsing rule → verified already implemented (`resolve_variant`),
  regression-tested in Task 3.
- **Ordering:** library YAML is rewritten only in Task 4 (bare refs stay
  legal until then); `package_family_ref` code dies in Task 5 *after* its
  YAML users are gone and *after* the parity repoint (Task 5 Step 1 commits
  separately, old code still green).
- **Type consistency:** `ref_address` introduced in Task 2 and used in Tasks
  5 (bom, properties) and 6 (offer, provider); `implicit` introduced in Task
  1, consumed in Tasks 2, 6; `validate_catalogue` self-contained in Task 3;
  `FootprintOptionView.group` introduced and consumed in Task 6.
- **Known judgment points for executors** (explicitly delegated, not
  placeholders): exact fused-family membership where old families differ
  structurally (Task 4 Step 1 rule), dims copied verbatim from old `typ`
  variants, and matching this branch's prism-dims spelling in test fixtures.
