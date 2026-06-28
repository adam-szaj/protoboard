<!-- SPDX-License-Identifier: MIT -->
# Schema proposal — `leads.direction`, `leads.body_offset`, `leads.board_offset`

**Status:** design / proposal. Nothing here is implemented. This extends the
agreed model in [schema-design.md](schema-design.md); read §2.1 (the `type`
grammar) and §2.3 (`auto`) there first. The three `leads` fields below were
**deliberately left undeclared** in the Phase-1 migration because their YAML
grammar is richer than the current `TypeSpec` can express — see the NOTE comment
in `assets/packages/generic.yaml` and project history. This document catalogs
every form actually used and proposes the minimum `TypeSpec` extensions needed
to declare them.

The format of all three fields is canonically documented in
[spec.md](spec.md); this file only adds the *schema declaration* layer on top.

---

## 0. Why they are undeclared today

The current matcher (`schema.py:_matches_shape`, lines 166–174) supports exactly
two things:

* a **primitive** (`"float"`, `"int"`, `"str"`, `"bool"`), and
* a **fixed-length vector** of shapes — `[float, float]` is a 2-vector,
  validated with `len(value) != len(spec)` (an *exact* length check).

`_matches` (lines 177–194) adds a **union** of such shapes (a list where ≥1
element is itself a list). That is the whole grammar.

None of the three `leads` fields fit, for three distinct reasons:

| Field | What it needs that the matcher lacks |
|---|---|
| `direction` | a **variable-length list** of `[float,float,float]` (per-pin form, length = pin count, not a fixed N); and a third *grouped* form `[[indices],[vec]]…` that is a domain encoding, not a shape. |
| `body_offset` | a **variable-length list** whose elements are a **union of two shapes** (`[x,y]` bare *or* `[count,[x,y]]` run-length); plus a `{unit, value}` wrapper dict; plus scalar sugar (`0`). |
| `board_offset` | the same variable-length run-length list, **or** one of two **string literals** (`"default"`, `"solve"`) — literal-value matching the matcher has no concept of. |

So three capabilities are missing: **(A)** variadic (repeated-shape) lists,
**(B)** element-level unions inside a list, **(C)** literal-constant matching.
All three are small, orthogonal additions.

---

## 1. Catalog of accepted forms (ground truth)

Every form below is taken from a real library file or the loader's own
docstring/tests. Canonical normalised type is what the loader produces.

### 1.1 `leads.direction` → `tuple[Vector3D, ...]` (one per pin)

Expanded by `offsets.py:expand_direction` (lines 88–131). Three forms:

| # | Form | Example | Source |
|---|---|---|---|
| 1 | single 3-vector, broadcast to all pins | `[0, 0, -1]` | `generic.yaml:78` (`prism_dil`) |
| 2 | per-pin list of 3-vectors (len = pin count) | `[[0,0,-1],[0,0,1]]` | `generic.yaml:57` (`cylinder_axial`), `:110` (`prism_smd`) |
| 3 | grouped: `[[indices],[x,y,z]]…`, 1-based pin indices, full coverage | `[[[1,2,3,4],[0,0,-1]],[[5,6,7,8],[0,0,1]]]` | `tests/unit/test_package_loader.py:54` |

### 1.2 `leads.body_offset` → `tuple[Vector, ...]` in **mm** (N−1 steps)

Expanded by `offsets.py:expand_offsets` then scaled by `unit`
(`loader.py:_body_offset_to_mm`, ~lines 119–135). Forms:

| # | Form | Example | Source |
|---|---|---|---|
| 1 | scalar sugar `s` → single step `[[s,0]]` | `0` | `generic.yaml:58` (`cylinder_axial`) |
| 2 | run-length list (`[count,[x,y]]`) and/or bare (`[x,y]`), mixed | `[[14,[0,1]],[7,0],[14,[0,-1]]]` | `library/Modules/boards.yaml` (Arduino Nano) |
| 3 | `{unit: U, value: <form 1 or 2>}` wrapper (scales to mm) | `{unit: 2.5, value: [[1,0]]}` | `generic.yaml:70` (`cylinder_radial`), `to92.yaml` |
| 4 | multiline list under `value:` (same grammar as form 2) | STM32G4 in `library/Modules/boards.yaml` | `boards.yaml` |

Run-length vs bare is distinguished by the **type of the 2nd element**: a list
⇒ `[count, vec]`; a number ⇒ `[x, y]` (`offsets.py:63`). `unit` defaults to 1.0
and **only** applies to `body_offset` (board is already in holes).

### 1.3 `leads.board_offset` → `tuple[Vector, ...]` in **holes**, or sentinel

Resolved by `loader.py:_board_steps_in_holes` / `shapes.py` (sentinels
`BOARD_DEFAULT = "default"`, `BOARD_SOLVE = "solve"`, `shapes.py:39-40`). Forms:

| # | Form | Example | Source |
|---|---|---|---|
| 1 | `"default"` — reuse body steps, read in the holes domain | `default` | `generic.yaml:71` (`cylinder_radial`) |
| 2 | `"solve"` — axial solver computes the layout (body = 0) | `solve` | `generic.yaml:59` (`cylinder_axial`) |
| 3 | explicit run-length / bare list, in holes (no `unit`) | `[2,[1,0]]` | `to92.yaml` |

Note `board_offset` takes **no `{unit,value}` dict** — holes are unitless.

---

## 2. Proposed `TypeSpec` extensions

Three new constructors, each a dict so they never collide with the existing
list-is-vector / list-is-union rules. They compose with the current grammar.

### (A) Variadic list — `{list: <shape>}`, optional `len`

Matches a list of **any length** whose every element matches `<shape>`.

```yaml
{list: [float, float, float]}          # any number of 3-vectors
{list: [float, float], len: pin_count} # len tied to a named count (see §3)
```

Matcher: `isinstance(value, (list, tuple))` and every element matches the inner
shape; if `len:` is a number, also check exact length; if `len: pin_count`
(symbolic) the **loader** supplies the count at validation time (schema alone
cannot know it — see §3).

### (B) One-of (element/value union) — `{oneof: [<shape-or-literal>, …]}`

Matches if the value matches **any** branch. Branches may be shapes *or*
literals (see C). This is the *named* form of today's implicit union, so an
inner-list element can itself be a union without the "≥1 nested element" heuristic
becoming ambiguous.

```yaml
{oneof: [[float, float], [int, [float, float]]]}   # bare [x,y] OR run-length [count,[x,y]]
```

### (C) Literal constant — `{const: <value>}` (or sugar inside `oneof`)

Matches iff `value == <value>`. Lets sentinels be declared.

```yaml
{const: default}
{oneof: [{const: default}, {const: solve}, {list: {oneof: […]}}]}
```

> All three are **additive**: a `type:` that is a string, a flat list, or a
> nested list keeps its current meaning. The new forms are only triggered by the
> `list` / `oneof` / `const` keys, which a plain shape never has.

---

## 3. The `pin_count` problem (variable length)

Forms `direction#2` and the offset lists have lengths bound to the **pin count**,
which is not known to a static schema — it depends on the concrete family /
placed instance. Two honest options:

* **3a — length-agnostic (recommended).** Declare `{list: <shape>}` with **no
  `len`**. Schema validates *element shape only*; the loader's `expand_*`
  functions already enforce the count (`expand_direction` raises
  "has N vectors but M pins"; `expand_offsets` produces N−1 steps). Schema and
  loader stay non-overlapping (schema-design.md §2.1: "loose, no double
  validation"). This is the minimal, ship-now choice.

* **3b — symbolic `len: pin_count`.** Add a symbolic length the loader resolves
  against the resolved pin count before `validate()`. More precise error
  messages, but introduces a schema↔loader coupling and a new resolution step.
  Defer unless the UI panels (schema-design.md §8) need the exact arity.

The grouped `direction#3` form (`[[indices],[vec]]…`) is a **domain encoding**,
not a geometric shape; even with A/B/C its index-coverage invariant (every pin
assigned exactly once, 1-based, in range) lives in `expand_direction`. Schema
can at best assert the outer structure `{list: [{list: int}, [float,float,float]]}`
— proposed as *advisory only*, with `expand_direction` remaining the validator.

---

## 4. Proposed schema declarations

Placed in whichever abstract base first declares the `leads.*` value
(in `generic.yaml` today that is the per-arrangement bases — `cylinder_axial`,
`cylinder_radial`, `prism_dil`, `prism_smd`, … — not the bare `package` base,
which carries no `leads` block). Schema is path-mirrored to the value path
`leads.*` and `extends`-overriding (schema-design.md §2.4), so a base may declare
it once and descendants inherit. Declare it on the highest base that actually
holds a `leads` value.

```yaml
schema:
  leads:
    direction:
      type:
        oneof:
          - [float, float, float]                       # form 1: broadcast
          - {list: [float, float, float]}               # form 2: per-pin (len via loader)
          - {list: [{list: int}, [float, float, float]]} # form 3: grouped (advisory)
      required: true

    body_offset:
      type:
        oneof:
          - float                                        # scalar sugar (0)
          - {list: {oneof: [[float, float],              # bare [x,y]
                            [int, [float, float]]]}}      # run-length [count,[x,y]]
          - {unit: float, value: {list: {oneof: [[float, float],
                                                 [int, [float, float]]]}}}
      # NOTE: the {unit, value} wrapper is itself a section-shaped dict; see §5.
      required: true

    board_offset:
      type:
        oneof:
          - {const: default}
          - {const: solve}
          - {list: {oneof: [[float, float], [int, [float, float]]]}}
      default: default
```

`required: true` is satisfied by `auto` and by any concrete value
(schema-design.md §2.3/§2.5). `board_offset` gets a real `default: default`
because most families inherit it.

---

## 5. The `{unit, value}` wrapper — open sub-decision

`body_offset` form 3 is a small **mapping** (`{unit, value}`), not a shape. Two
ways to declare it, pick one:

* **5a — treat it as a shape branch** with a dict-shape matcher (a new
  `{fields: {unit: float, value: <typespec>}}` constructor that matches a mapping
  with those keys). Cleanest, but adds a fourth constructor.
* **5b — normalise before validation.** The loader already unwraps
  `{unit, value}` in `_body_offset_to_mm`. Move that unwrap *ahead* of
  `schema.validate`, so by validation time `body_offset` is always a bare list
  and the wrapper never reaches the matcher. Then the `body_offset` `type:`
  drops its third `oneof` branch entirely. **Recommended** — keeps the matcher
  shape-only and matches the existing loader order (schema-design.md §3).

Under 5b the `body_offset` declaration simplifies to:

```yaml
    body_offset:
      type:
        oneof:
          - float
          - {list: {oneof: [[float, float], [int, [float, float]]]}}
      required: true
```

---

## 6. Alternative: keep them loose (the status-quo baseline)

The cheapest option remains **declaring nothing** — exactly today's state. The
loader's `expand_direction` / `expand_offsets` already fully validate these
fields and raise precise errors. The *only* things a schema buys here are:

1. **UI** — `fields_view` (schema-design.md §8) can render an editor row for
   `direction` / offsets. Without a schema entry these fields are invisible to
   the Properties panel and the place-tool picker.
2. **Documentation as data** — the accepted grammar lives in the schema instead
   of a prose comment.

If neither is needed yet, do nothing and revisit when the panels land. If they
*are* needed, §2 (A+B+C) + §4 with the §3a / §5b recommendations is the minimal
implementation.

---

## 7. Recommendation summary

| Decision | Recommendation |
|---|---|
| Extensions | Add **A** (variadic `{list}`), **B** (`{oneof}`), **C** (`{const}`). |
| Variable length | **3a** — element-shape only, no `len`; loader owns arity. |
| `{unit,value}` | **5b** — unwrap in loader before `validate`; matcher stays shape-only. |
| Grouped `direction` | Advisory outer-structure only; `expand_direction` stays the validator. |
| If panels not needed yet | **§6** — keep loose, revisit with §8 panels. |

All extensions are additive to `_matches` and break no existing `schema:` block.
The matcher changes touch only `schema.py`; no loader behaviour changes under
3a + 5b (loader still owns count/index/sentinel semantics).

---

## 8. Tests to add when implemented

* `_matches({list: [...]}, …)` — empty list, all-match, one-mismatch.
* `_matches({oneof: [...]}, …)` — each branch, none-match.
* `_matches({const: default}, "default")` true; `"solve"` false.
* Round-trip: every real YAML form in §1 validates against the §4 declaration.
* Regression: existing flat-list-as-vector and nested-list-as-union still pass
  (schema-design.md §9 list).
