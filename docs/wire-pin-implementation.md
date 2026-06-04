# Wire-pin implementation plan

Status: design plan, 2026-06-04. Decisions confirmed by user; implementation not started.

User decisions (2026-06-04):
1. **Model**: new `WirePinKnot` dataclass alongside `PinKnot` / `JointKnot`.  Stand-alone (no `ComponentInstance` underneath).  Lifetime tied to the owning wire.
2. **UX trigger**: PPM action "Convert endpoint to wire-pin" on a half-orphan wire endpoint sitting on a plated-through pad.  Reverse action "Convert wire-pin back to free endpoint" mirrors it.

## TL;DR

A *wire-pin* is the persistent solder terminal at one end of a wire — the
logical equivalent of a 1x01 pinheader (goldpin) physically tacked onto a
pad.  It exists to:

- declare a deliberate terminal at a half-orphan wire endpoint without
  pulling a real `ComponentInstance` from the library;
- give group `exported_pins` something to point at when the export sits
  on a routed terminal rather than on a real component pin;
- make rule 6 (no foreign wire over pin pads) apply to that pad so the
  terminal is not crossed by a stray bare wire.

This refactor adds the `WirePinKnot` type, a single command pair
(`ConvertEndpointToWirePinCommand` / its inverse), persistence support
(`kind: "wire_pin"` in `layout.yaml`), DRC enforcement, renderer support
(pad painted in the net colour, no body), and the PPM entry that drives
it.

## 1. Current state

- `Knot` is a `Protocol` (`domain/layout/knot.py`).  Two concrete
  implementations: `PinKnot` (refers to a `PinRef`, resolves position
  through layout) and `JointKnot` (free position, must coincide with a
  pad).
- `drag_classification.py` already speaks of *half-orphan* wires — a
  wire whose endpoint is a `JointKnot` (no `PinRef` anchor).  ~50
  `isinstance(..., PinKnot|JointKnot)` dispatch sites across the
  codebase classify endpoints today.
- `pad_state.Item` has six subtypes: `PinItem`, `BareWireItem`,
  `BareWireCrossing`, `InsulatedWireItem`, `InsulatedWireCrossing`,
  `ComponentBody`.  `_item_is_through_hole` returns `True` for
  `PinItem` only — that's what triggers electrical propagation to all
  physical layers (rule 5).
- `services/pad_strategy.py` already declares
  `is_half_orphan_destination(pad, layer, our_net)` on the
  `PadStrategy` protocol with MVP-return-ALLOW concrete
  implementations — the *autorouter consumes half-orphan endpoints as
  candidate destinations* hook is in the shape but not wired up.
- DRC has no `HALF_ORPHAN_WIRE` check yet; the closest existing entry
  (`_check_floating_pins`, `services/drc/engine.py:407`) is about
  unplaced *component* pins, not wire endpoints.
- Persistence (`project_yaml.py`) encodes knots as
  `{"kind": "pin"|"joint", ...}`.  Loader treats unknown `kind` as an
  error.

So: rule 9 (half-orphan) is not enforced; wire-pin doesn't exist; the
hooks for plugging it into the autorouter and the strategy layer are
already shaped.

## 2. Target semantics

### 2.1 Schema

```python
@dataclass
class WirePinKnot:
    id: uuid.UUID
    position: Point          # world coords — must coincide with a pad
    net_id: NetId | None     # snapshot of owning wire's net at creation
    wire_ids: frozenset[uuid.UUID]  # the single owning wire's id
```

Invariants:

- exactly **one** `wire_id` in `wire_ids`.  Removing that wire ⇒ the
  knot disappears (`Layout.remove_wire` already deletes orphan knots;
  the wire-pin case piggybacks on the same path).
- `position` lies on a pad with `plated_through=True`.  Forbidden on
  off-pad positions and on single-side pads — the wire-pin is the
  *piercing terminal* and rule 5 (through-hole propagation) is its
  whole point.
- `net_id` snapshots the owning wire's net at conversion time.  If the
  owning wire's net changes later (T-junction merge, manual disconnect),
  the wire-pin's `net_id` is updated by the same command that updates
  the wire.

### 2.2 Pad occupancy

New `Item` subtype:

```python
@dataclass(frozen=True, slots=True)
class WirePinItem:
    id: uuid.UUID            # the knot's id
    net: NetId | None
    kind: ClassVar[str] = "wire_pin"
```

`_item_is_through_hole` returns `True` for `WirePinItem` too — wire-pin
pierces the hole like a real pin.  `_item_contributes_electrically`
returns `True` (it's a soldered terminal).

Rule 6 (no foreign wire over pin pads) applies because the pad now has
an item kind that strategy code treats as "has pin".

### 2.3 Group exports

A wire-pin can sit in `ComponentGroup.exported_pins` directly — but the
existing `ExportedPin(component_id, pin_number)` shape names a
*component* pin.  Two routes:

- **MVP**: do **not** allow wire-pins in `exported_pins`.  The use case
  "group exports a terminal that has no component" is deferred — the
  user can always add a real 1x01 goldpin if they want a group export.
  Lower risk; closes the encapsulation refactor's open question
  cleanly.
- **Follow-up**: a separate refactor extends `ExportedPin` to a sum
  type (component pin or wire-pin id).  Touches every place that
  pattern-matches on `ExportedPin`.

Plan picks MVP.  Group export of wire-pins becomes a separate
follow-up; nothing in this refactor changes `exported_pins` semantics.

## 3. Surgical changes per file

### 3.1 `src/protoboard/domain/layout/knot.py`

Add `WirePinKnot`.  No change to the `Knot` protocol — it already only
requires `id` and `wire_ids`.

```python
@dataclass
class WirePinKnot:
    id: uuid.UUID
    position: Point
    net_id: "NetId | None"
    wire_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)
    # __hash__/__eq__ by id — same idiom as the other two
```

### 3.2 `src/protoboard/domain/board/pad_state.py`

Add `WirePinItem` to the `Item` union.  Update `_item_is_through_hole`
to include it.  No other change — propagation, electrical bookkeeping,
side rules fall out for free.

### 3.3 `src/protoboard/domain/layout/layout.py`

Eleven `isinstance(..., PinKnot|JointKnot)` sites.  Audit each:

- Wherever the code asks *"is this anchored to a component pin?"* the
  answer for `WirePinKnot` is **no** — it is a free terminal.  Keep
  the existing branch (treat as the `JointKnot` case).
- Wherever the code asks *"does this knot have a position?"* the
  answer is **yes**, like `JointKnot`.
- Add an explicit branch only where rule 5/6 enforcement requires it
  (PadIndex contribution, see §3.4).

In practice this means dispatching `(PinKnot,)` ↔ component anchor,
`(JointKnot, WirePinKnot)` ↔ free position.  A helper
`knot_is_anchored_to_pin(knot)` documents the intent and clusters the
dispatch.

### 3.4 `src/protoboard/services/drag_detector.py` (PadIndex builder)

When walking knots and registering occupants on pads, emit a
`WirePinItem` for each `WirePinKnot`.  Same hook that today emits
`PinItem` for component pins.

### 3.5 `src/protoboard/services/drc/engine.py`

Two checks:

- **`HALF_ORPHAN_WIRE`** (warning) — for every wire with a `JointKnot`
  endpoint that has only this wire in its `wire_ids` (no t-junction
  mate), emit a warning unless the endpoint is a `WirePinKnot` (then
  it's a deliberate terminal, not orphan).  Rule 9 documented.
- **`WIRE_PIN_OFF_PLATED_THROUGH`** (error) — every `WirePinKnot`'s
  position must be on a plated-through pad.  Defensive; the command
  layer enforces creation-time, but DRC catches the case where the
  underlying board's pad was edited to remove plated-through.

The existing rule 6 check (`_check_bare_wire_cross`, `_find_pin_at`)
gets `WirePinItem` for free once §3.4 lands.

### 3.6 `src/protoboard/domain/commands/wire_cmds.py` (or new file)

Two commands, each undoable:

- **`ConvertEndpointToWirePinCommand(wire_id, endpoint: Literal["a","b"])`**
  - Precondition: the named endpoint is a `JointKnot`, the joint has
    exactly one wire (`wire_ids == {wire_id}`), and its position is on
    a plated-through pad.  Returns `Precondition` failure otherwise so
    the GUI can disable the action grey.
  - Apply: replace the `JointKnot` in `layout.knots` with a
    `WirePinKnot` of the same id and position, snapshot
    `wire.net_id`.  Rebind the wire's `KnotRef.id` is unchanged (id
    reuse keeps wire untouched).  Mark PadIndex dirty.
  - Revert: replace back with a `JointKnot` at the same position.
- **`ConvertWirePinToEndpointCommand(wire_pin_id)`** — inverse.
  Replace the `WirePinKnot` with a `JointKnot` of the same id and
  position.  No precondition beyond *"the knot exists and is a
  wire-pin"*.

ID reuse means the wire's `KnotRef` doesn't change — no need to touch
any wire path, neighbour, or T-junction structure.

### 3.7 `src/protoboard/gui/tools/select_tool.py` (PPM)

In `_show_wire_menu` (line 3459+), detect whether the clicked
endpoint is an eligible half-orphan:

```python
endpoint = _classify_clicked_endpoint(wire, scene_pt, self._session)
if endpoint.is_half_orphan_on_plated_through:
    act = menu.addAction("Convert endpoint to wire-pin")
    act.setData(("convert_to_wire_pin", wire.id, endpoint.side))
elif endpoint.is_wire_pin:
    act = menu.addAction("Convert wire-pin back to free endpoint")
    act.setData(("convert_from_wire_pin", endpoint.knot_id))
```

Where `endpoint.side ∈ {"a", "b"}` keys into `wire.a` / `wire.b`.

The detector `_classify_clicked_endpoint` is a new small helper —
maps the scene click to whichever of the two endpoints is closest
(within a tolerance) and reports the relevant knot type.

### 3.8 `src/protoboard/gui/keybindings.py`

Per `[Every UI action has a keybinding]` memory, add:

```python
ACTION_WIRE_CONVERT_TO_WIRE_PIN   = "wire_convert_to_wire_pin"
ACTION_WIRE_CONVERT_FROM_WIRE_PIN = "wire_convert_from_wire_pin"
```

Empty default keybinding (PPM-only at first).  Title in the menu
label catalog.

### 3.9 `src/protoboard/services/persistence/project_yaml.py`

Encoder: emit `{"kind": "wire_pin", "position": {...}, "net_id":
"..."}`.  Decoder: a third branch in the existing `if kind == "pin"`
chain (`project_yaml.py:832+`).  Bump no schema version — the addition
is additive and unknown kinds were already an error, so old loaders
seeing a wire-pin will fail-fast (acceptable; warn the user "this
project was saved with a newer Protoboard version").

### 3.10 `src/protoboard/gui/renderers/wire_renderer.py`

Where the wire endpoints render: a wire-pin draws the underlying pad
ring filled with the net colour and a small inner mark distinct from
a regular pin (e.g. concentric ring).  No component body.  The
existing knot-rendering path is the right place to fork; the pad
itself is painted by the board renderer, so the wire renderer just
needs an emphasized terminal cap.

### 3.11 `src/protoboard/services/routing/autorouter.py`

Plug the existing `is_half_orphan_destination` hook into target
collection.  When a net to be routed has a half-orphan endpoint of
the same net (either a bare `JointKnot` or a `WirePinKnot`), include
that endpoint in the target set.  For `WirePinKnot` specifically:
*prefer* it over the matching `JointKnot` (cost bonus) — the user
chose this terminal deliberately.

This is the optional second half of the refactor.  Can land in a
later commit; the MVP doesn't need autorouter changes.

## 4. Plan — commits

| # | Commit | Files |
|---|---|---|
| 1 | `feat(knot): WirePinKnot type + WirePinItem pad occupant` | `knot.py`, `pad_state.py`, `layout.py` dispatch audit, unit tests for both new types |
| 2 | `feat(commands): convert endpoint to/from wire-pin` | new commands, undoable, with precondition tests |
| 3 | `feat(persistence): wire-pin kind in layout.yaml` | encoder + decoder, round-trip tests |
| 4 | `feat(drc): HALF_ORPHAN_WIRE warning + WIRE_PIN_OFF_PLATED_THROUGH error` | DRC engine, tests |
| 5 | `feat(ppm): "Convert endpoint to wire-pin" wire menu action` | select_tool, keybindings, hover hints |
| 6 | `feat(renderer): wire-pin glyph` | wire_renderer, theme touch-ups |
| 7 | *(optional)* `feat(autoroute): wire-pin / half-orphan as target` | autorouter wiring, regression fixture |

All commits small, independent enough to revert individually.  Test
suite green at every step (baseline 1436 unit tests).

## 5. Test plan

New tests in `tests/unit/`:

- `test_knot.py` (new file): `WirePinKnot` construction, equality, hash.
- `test_pad_state.py`: `WirePinItem` through-hole propagation matches
  `PinItem`.
- `test_wire_pin_commands.py`: convert/revert round-trip, precondition
  failures (anchored endpoint, off-plated-through pad, multi-wire joint).
- `test_drc.py`: `HALF_ORPHAN_WIRE` fires for bare orphan, suppressed
  by wire-pin; `WIRE_PIN_OFF_PLATED_THROUGH` fires when the board edit
  strips plated-through after wire-pin creation.
- `test_project_yaml.py`: wire-pin round-trip, unknown-kind rejection.
- `test_wire_pin_renderer.py` (pytest-qt): clicking PPM "Convert
  endpoint to wire-pin" on a half-orphan produces the expected
  command.

## 6. Open questions

1. **Net snapshot drift.**  `WirePinKnot.net_id` snapshots the wire's
   net at conversion.  When the wire's net changes (manual disconnect
   re-assigns the wire to a new net), should the wire-pin auto-update
   or detach?  Plan picks auto-update — the wire-pin is *the wire's*
   terminal; if the wire's electrical identity changes, the terminal
   moves with it.  Implement by having `SetWireNetCommand` also patch
   any `WirePinKnot` whose `wire_ids` contains the wire.
2. **Disband group ↔ wire-pin in exported_pins**.  Out of scope for
   MVP (wire-pins can't sit in `exported_pins` yet).  Re-open when
   doing the `ExportedPin` sum-type follow-up.
3. **Wire-pin name**.  None.  The wire-pin has no user-visible name;
   it's identified by its position + owning wire's net.  If the
   follow-up "group export wire-pin" lands, a name field becomes
   useful for the panel display.

## 7. Effort estimate

7 small commits, ~1300 LOC including tests, ~3 hours focused.  No
schema version bump.  Strategy hook
(`is_half_orphan_destination`) already shaped — autorouter wiring is
the cheapest part.

## 8. Out of scope

- Kapton tape (separate follow-up, named in
  `pad-pin-wire-junction-semantics.md`).
- Hierarchical autoroute interactions
  ([[hierarchical-autoroute-followup]]).
- Wire-pins as `ExportedPin` members (separate sum-type refactor).
