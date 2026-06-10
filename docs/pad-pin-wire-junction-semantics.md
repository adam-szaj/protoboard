# Pad, pin, wire and joint semantics

Many automatic and manual actions need a precise answer to "what is
allowed, what is forbidden, and what is merely *discouraged*?" when
placing components or routing wires. A frequent source of bugs is each
algorithm having its own informal answer.

This document is the **single source of truth** for the pad / pin / wire /
joint rules. It defines the conventions, the hard rules (0–9), the soft
rules, the wire-pin model, the kapton-tape modifier, and the cross-check
table that maps each rule to its enforcement site.

## Conventions

- **Net** = `schematic.Net` — a logical net identified by `Net.id`. A wire
  whose endpoints don't resolve to any pin and which has no `PinKnot`
  anywhere on its path is treated as an **orphan net** — *not* part of any
  named `Net`, but *also not a new net*. A freshly drawn wire is orphan
  until one of its endpoints lands on a pin / joint of a named net (then
  it inherits that net). Joining an orphan wire with a named-net node is
  permitted; joining two named-net nodes from different nets is the
  classic short.

- **Solder node** = a component pin **or** a pad-joint (same-side or
  cross-side via) **or** a pad with a bare-wire vertex (endpoint or
  middle point) on the active side. Every physical soldering point.

  A bare wire on a metallised pad on its own side is **always**
  electrically bonded to the pad regardless of whether it's an endpoint
  or a middle point. The difference between endpoint and middle point:

  - **middle point** — no extra penetration; the connection is just the
    factory metallisation of the hole (which, for `plated_through=True`,
    bridges both sides).
  - **endpoint** — adds its own penetration (soldering); galvanically
    bridges both sides even when the hole is not plated-through.

- **Active side** of a pad = the side for which `PadLayers` returns True.
  `plated_through=True` means both sides are bonded at the factory (no
  via required).

- **Pin sidedness:** pins (component pins and wire-pins) **have no side**.
  Logically they are present on both sides of the board. The component
  body sits on one side but its solder-pads (pin-joint, t-joint, wire-pin)
  exist on both.

## Term glossary

### Solder node subtypes

- **Pin** — a component pin (`PinKnot`). Position derived from placement.
- **Same-side pad-joint** — all incident wires on one side (`JointKnot`,
  `position == pad`).
- **Via** — a pad-joint with incident wires on both sides. A pin also
  effectively acts as a via — see rule 3 (galvanic bonding via physical
  penetration).
- **Bare-wire vertex** — an endpoint or middle point of a bare wire on a
  metallised pad on the wire's side (see *Conventions / Solder node*
  above).

Pins, pad-joints and vias share the same net-conflict semantics: every
incident wire / pin must be net-compatible. A bare-wire vertex creates an
electrical contact without creating a structural joint (a joint is a
model mutation; a vertex is just a position on a pad).

### Net compatibility (rule 0 extended)

Two elements (wire / pin / joint) are **net-compatible** when:

- they share `Net.id`, **or**
- one is orphan and the other has a named net (the orphan inherits), **or**
- both are orphan (loose chain, no named net on either end).

**Conflict** = both have a named net and `net_a.id != net_b.id`.

---

## Hard rules

### Rule 0 — No shorts

Two different nets must not form an electrical connection. This is the
meta-rule above rules 1–9; each of them is a specialisation.

### Rule 1 — No foreign wire on a pin

A wire of a different net must not run over a component pin. See rule 6
for the full formalisation (covers every vertex of the wire, not just the
endpoint).

### Rule 2 — A pad-joint refuses foreign nets

An existing pad-joint is treated like a component pin. Two detail rules:

**2a.** A pad-joint may not be created on a pad that already contains
*anything* belonging to another net. Example: on pad X, a bottom-side
wire `W_a` belongs to `Net_a` and a top-side wire `W_b` belongs to
`Net_b`. That layout is legal, but no third wire may attach to X to
form a pad-joint — any additional mate forces a "which net does the pad
belong to?" decision and there is no good answer.

**Exception 2a.1** — fragment replacement. When the endpoint of a
replaced fragment lands on pad X and the transient joint reduces
atomically inside the same mutation (`make_t_junction` → split +
re-anchor in a single step, post-mutation degree ≤ 1), the operation is
legal. The caller must guarantee the immediate reduction.

**2b.** A joint may not be created on a pad that holds a foreign-net
wire (degenerate 2a — no extra mates needed).

### Rule 3 — Vias and pins bond both sides

A via is a special pad-joint that switches sides. Vias and component pins
make a galvanic connection on both sides of the board, even on
single-sided boards and on pads that are *not* `plated_through`.

### Rule 4 — Bare wire and plated-through

A bare wire creates a galvanic connection on the side it runs on, provided
the pad is metallised on that side (see *Solder node*). If the hole is
plated-through, the connection appears on the opposite side too.

On a pad with a bare wire you may:

- form a same-net t-joint,
- place a component pin of the same net,
- place an insulated wire:
  - **same net** → creates a pad-joint (and a via if the sides differ),
  - **foreign net** → allowed only when no other constraint kicks in
    (no via, no pad-joint, the insulation isolates the wire from the
    pad).

### Rule 5 — Galvanic penetration via a pin

A component pin **always** bonds both sides of the pad regardless of
`PadLayers.plated_through`. Implementation: when asking "what is on side
X of this pad?", pins must be counted **independent of `placement.side`**.

This is rule 3 made explicit because:

- a via is **optional** (deliberately created by user / autorouter),
- a pin is **always** present once the component is placed.

Pin penetration is independent of bare/insulated wires — the pin bonds
the pad by being there.

### Rule 6 — No wire over a foreign pin pad

**No wire whatsoever may pass over a component-pin pad** (regular pin or
wire-pin) — regardless of insulation, regardless of whether the vertex
is an endpoint or an interior point, regardless of net.

Rationale (we deliberately go beyond pure electrical zero):

- **physical**: routing over a pin is hard or impossible — the pin
  protrudes above the pad, the insulation does not bend around it,
- **aesthetic and serviceability**: a layout with wires over pins is
  harder to read and to rework,
- **electrical** (a slice of rule 0): a foreign-net bare wire over a pin
  is an immediate short.

There is **no insulated-interior exception**. Every vertex (endpoint,
middle point) of every wire (bare, insulated) is forbidden on a pin pad
— with the sole exception of the wire whose endpoint **is** that pin
(legal solder).

The same rule applies to wire-pins. Physically a wire-pin is a chunk of
wire soldered to a pad; anything running over it has the same physical
and aesthetic problems as a regular pin.

### Rule 7 — Pad-joint exclusivity per net

A pad-joint of any kind (including vias) is **exclusive to one named
net** once formed. A new incident wire must be net-compatible with the
existing joint.

Rule 2 covers **creation**; rule 7 covers **existing** joints — you can't
add a foreign wire to a live joint, and even more so to a live via.

Exception 2a.1 (fragment replacement) applies: legal as long as the
joint **reduces immediately** — after the operation the pad is no longer
a joint (degree → 0 or 1). The caller must guarantee the atomic
reduction of the transient joint inside the same mutation.

### Rule 8 — A component pin refuses incoming foreign wires

A component pin (analogous to a joint in rule 7) **does not accept a
foreign-net wire** as an incoming endpoint.

`_find_joint_at` enforces this via its net gate after four rounds of
t-junction analysis. `_find_pin_at` does not yet have an equivalent —
the implementation assumes that if an incoming wire endpoint hits a pin,
the wire is "of the same net" (or orphan inheriting the pin's net).
This rule requires an explicit gate:

```
incoming wire endpoint on pin P:
- net(incoming) == net(P)                          → SAFE
- net(incoming) == None, net(P) != None            → ASSIGN (incoming inherits net of P)
- net(incoming) != None, net(P) == None            → ASSIGN (pin inherits net of incoming)
- net(incoming) != net(P) (both named)             → REJECT (foreign wire on pin)
```

It is the direct counterpart of `can_form_t_junction`, but for pin
endpoints rather than joint interiors.

### Rule 9 — Half-orphan wire (warning, not error)

A wire whose **one or both endpoints have no solder mate** (no pin, no
joint, no second wire endpoint at the same solder node) is a
**half-orphan**.

DRC raises a **warning** rather than an error. The user decides:

- delete it (the wire was meant to go away),
- continue it (add another wire),
- convert it to a wire-pin (virtual goldpin — see below).

A lone endpoint on a pad is treated as a **prospective via**: if
something attached to it later, it would *become* a via. Until then the
electrical net is the wire's net locally, with no galvanic spread to the
other side.

Half-orphans frequently appear after a component move — conflicts with
other pins, or geometric mismatches, cause incident wires to be dropped.
**The autorouter must treat half-orphan wires as candidate destinations**
for the net being completed (not only as prospective vias — also as
prospective targets for a new segment of the same net).

---

## Soft rules

Situations that are legal but discouraged. Some are unrestricted for one
tool and forbidden for another.

**Example:** insulated wire crossings (same-side or opposite-side) on a
pad. The autorouter accepts them as a legal-but-expensive routing option;
the "simplify wire path" tool tightens the rule — simplification tries to
clean up both the path *and* the soldering job, so crossings on pads are
penalised harder. The user may still opt in to a crossing on a pad
(subject to the hard rules).

Off-pad crossings are neutral as long as they sit on opposite sides of
the board.

A pad covered by kapton tape accepts insulated crossings without
restriction (the wire does not physically touch the pad — the tape
isolates it). See *Pad modifiers (kapton)* below.

---

## Wire-pin (virtual goldpin)

A **wire-pin** is a virtual single-pin component representing the
endpoint of a wire as a stable solder point. Logical equivalent of a
1×01 pin-header (goldpin); physically just a piece of wire soldered into
the hole.

### Use cases

- Declaring a deliberate group `exported_pins` terminal without adding a
  real goldpin component.
- Turning a half-orphan wire endpoint into a deliberate terminal.
- Anchoring a net locally: the wire has a terminal, the joint is
  formalised.

### Semantics

A wire-pin has:

- a position (must coincide with a plated-through pad),
- a net (inherited from the owning wire at creation),
- **no side** — like a regular pin, present on both sides.

It bonds the pad like a regular pin (rule 5). Rule 6 applies identically
— no wire may run over a wire-pin pad except the one whose endpoint *is*
the wire-pin.

### Lifetime

A wire-pin is **tightly coupled to the wire**:

- wire deleted → wire-pin deleted,
- wire endpoint moved → wire-pin removed from the old position.

A wire-pin is *not* an independent project object like a
`ComponentInstance`.

### Data model

`WirePinKnot` is a third `Knot` kind next to `PinKnot` and `JointKnot`,
backed by `WirePinItem` in `PadIndex`. The visualisation paints the pad
in the net colour with a small inner mark (no component body).

---

## Pad modifiers (kapton tape) — **planned**

Kapton tape (or any manually applied insulation) changes the per-pad
electrical behaviour. Tape may be applied to top, bottom, or both sides.

**Logically a tape creates an extra layer.** Tape on "top" creates a
`tape:top+1` layer, conceptually a copy of the top layer of a
single-sided board. The board's wire layers are now `top`, `bottom`,
`tape:top+1` (and so on for multi-layer tapes).

### Semantics

**A tape entirely isolates the pad from a bare wire** on the side it
covers. That holds even on `plated_through=True` pads — the tape blocks
the bare wire despite the through-hole connection.

**Exceptions that pierce the tape** (physical penetration in the hole):

- component pin,
- via,
- wire-pin.

They bond the pad regardless of the tape (the pin punctures the tape
over the hole metallurgically).

**Pad-joint over tape** (rule 7 + modifier):

- **Default**: allowed (creates a structural node on the routing layer
  above the tape; no electrical connection to the pad *below* the tape,
  no bond with the opposite side — the tape isolates).
- **Per-project knob `strict_tape_joints`**: when on, a joint over tape
  is an error (conservative).
- Subtlety: a joint over tape is the only case where a joint does not
  bond to the pad or the opposite side. The default ALLOW reflects the
  tape's intent (isolate one side from the other) but warrants attention
  in electrical analysis.

Tape is a **per-side modifier** for bare wires and (optionally) joint
creation. It does not affect pins, vias or wire-pins — those penetrate
it physically.

### Edge cases, DRC, autoroute, simplify

Documented inline at the original notes (see the historic
`pad-state-refactor-questions.md` archive). Summary:

- bare wire on a taped pad is *off-pad* electrically;
- bare wire on the opposite (untaped) side is unaffected;
- `_check_short` must understand tape so it doesn't flag the now-isolated
  bare wire as a missing bond;
- `_check_bare_wire_interior_cross` over a tape becomes a soft "prefer
  not to" instead of an error;
- a new `KAPTON_REDUNDANT` info-level check warns when tape covers a pad
  that has nothing to isolate;
- the autoroute cost model gets a `kapton_bare_freedom` knob (default
  neutral).

### Data model and persistence (planned)

```python
@dataclass(frozen=True)
class PadModifiers:
    kapton_top: bool = False
    kapton_bottom: bool = False
    # future: solder_mask, …

# Layout.pad_modifiers: dict[Point, PadModifiers]
```

YAML:

```yaml
pad_modifiers:
  - position: {x: 4.5, y: 3.5}
    kapton_top: true
  - position: {x: 5.5, y: 3.5}
    kapton_top: true
    kapton_bottom: true
```

An empty list means "no tape" and is not written.

GUI: two new modes — "Add kapton tape" and "Remove kapton tape" — work on
a click or a rubber-band of pads. Visualisation: a translucent yellow
glow over the side that carries tape.

The MVP supports a single tape per side; multi-layer tapes (`top+2`,
`top+3`, …) are a follow-up that extends `PadModifiers` from booleans to
a list of layers.

### Open questions

1. Wire-pin as a new `Knot` subtype (done — `WirePinKnot`) vs a virtual
   `ComponentInstance`. The first option won.
2. Net inheritance for an orphan-orphan join — does the chain stay
   orphan or get a synthetic net id?
3. Tape persistence and undo — special-case in save / load?
4. Half-orphan DRC visualisation — icon, colour, dialog ("delete / extend
   / convert to wire-pin")?
5. Can tape be applied to off-pad regions? The physical tape covers area,
   not points — but per-pad modelling is simpler.
6. Multi-layer tape — single per side in the MVP, plan the schema so a
   future extension is non-breaking.
7. `strict_tape_joints` knob — project-wide (`Layout.settings`) or
   per-pad override? Default ALLOW; storage location TBD.

---

## Cross-check checklist

For each call site (autoroute cost, DRC checks, simplify, drag-conflict,
T-junction gates, joint reuse, pin reuse) verify conformance with rules
5–9 and kapton.

Status meanings: **OK** = enforced; **partial** = some cases not covered;
**missing** = not enforced; **n/a** = the rule doesn't apply.

The most recent full audit was 2026-05-31 on `main`. After the unified
drag pipeline + wire-pin + group-export-encapsulation work landed the
table needs another pass; the remaining `partial`/`missing` cells now
mostly concern kapton (which is still unimplemented). The summary
columns:

- **Rule 5 (pin penetration)** — enforced everywhere except where
  kapton would matter.
- **Rule 6 (no foreign wire over pin)** — enforced by `DRCEnforcingStrategy`
  consumed in the drag pipeline and by the DRC `BARE_WIRE_INTERIOR_CROSS`
  + the T-junction gate. The autoroute cost model still treats
  `wire_over_foreign_wire_pad` as a soft penalty rather than a hard ban.
- **Rule 7 (joint exclusivity)** — enforced at creation
  (`can_form_t_junction`) and at re-use (`_find_joint_at`).
- **Rule 8 (pin gate)** — enforced for drag (via the strategy);
  autorouter relies on it indirectly.
- **Rule 9 (half-orphan)** — DRC raises `HALF_ORPHAN_WIRE`. Autorouter
  consumption of half-orphan endpoints as targets is implemented via
  `PadStrategy.is_half_orphan_destination` but currently returns ALLOW
  for the MVP.
- **Kapton** — out of scope until the modifier ships.
