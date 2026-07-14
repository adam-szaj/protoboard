<!-- SPDX-License-Identifier: MIT -->
# Body-printed value + hover-reveal — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render each component's `value` on its body (R/L/C as a code, other classes as the raw part number), luminance-driven text colour; and reveal a component's hidden label (tooltip) or pin-names (per-component layer override) on ~1 s hover.

**Architecture:** Three pure/gui units for Feature 1 — `value_text` (what to print, pure), `text_contrast` (what colour, pure math), `body_label` (how to draw, Qt) — wired into `PackageRenderer.render_body` via the already-resolved `self.paint.fill`. Feature 2 adds a dwell timer + `mods`-carrying `HoverEvent`; a hidden-`labels` dwell shows a cursor tooltip, a hidden-`pin_names`+Alt dwell sets a per-component reveal id on `LayerState` so the existing pin-name render path draws for that one component.

**Tech Stack:** Python 3.11+, PySide6 (QGraphicsScene), pytest / pytest-qt. Timers via `gui/throttle.py` (no raw `QTimer`).

## Global Constraints

- SPDX header `# SPDX-License-Identifier: MIT` on every new file.
- `mypy --strict` must pass; `frozen=True` dataclasses where practical.
- Domain/pure units must import zero Qt and be testable headless.
- All new GUI timers go through `gui/throttle.py` (`TimerWrap` / `ParkLatestThrottle`) — no raw `QTimer`.
- Conventional Commits (`feat:`, `test:`, `refactor:`, `docs:`).
- Never `git add -A`/`.`; stage explicit paths.
- Component classes are `ComponentClass.RESISTOR`, `.INDUCTOR`, `.CAPACITOR_POLAR`, `.CAPACITOR_NONPOLAR` (there is **no** bare `CAPACITOR`). Enum lives in `src/protoboard/domain/components/definition.py`.

---

## File structure

**Feature 1 (body-printed value):**
- `src/protoboard/gui/renderers/value_codes.py` (**modify**) — add `parse_capacitance()` (pF) + extend `parse_value()` policy to capacitors.
- `src/protoboard/domain/components/value_text.py` (**create**) — `body_print_text(component_class, value) -> str | None`. Pure, no Qt.
- `src/protoboard/gui/renderers/text_contrast.py` (**create**) — `best_ink(body_color) -> QColor` + Qt-free luminance/contrast core.
- `src/protoboard/gui/renderers/packages/body_label.py` (**create**) — `make_body_label(...) -> QGraphicsSimpleTextItem`, geometric scale-to-fit.
- `src/protoboard/gui/renderers/packages/base.py` (**modify**) — call the helper at the end of `render_body` (generic path).
- `src/protoboard/gui/renderers/packages/prism_kinds.py` (**modify**) — refactor `SmdChipRenderer._code_label` to route through `best_ink` + `make_body_label`.

**Feature 2 (hover-reveal):**
- `src/protoboard/controller/ports/hover.py` (**modify**) — add `mods` to `HoverEvent`.
- `src/protoboard/gui/tool_host.py` (**modify**) — pass `mods` into `HoverEvent`; add the ~1 s dwell timer arming.
- `src/protoboard/gui/layer_state.py` (**modify**) — add `pin_names_reveal_cid` + make `alpha_for_pin_names` honour it.
- `src/protoboard/gui/renderers/component_renderer.py` (**modify**) — thread the current component id into the pin-name alpha check.
- `src/protoboard/gui/hover_reveal.py` (**create**) — the `hover_published` subscriber: decides tooltip (2A) vs pin-name reveal (2B) and drives them.
- `src/protoboard/gui/hover_tooltip.py` (**create**) — the cursor-anchored `refdes value` widget (2A).

**Tests:**
- `tests/unit/test_value_codes.py` (**modify**) — capacitor parsing/coding.
- `tests/unit/test_value_text.py` (**create**) — `body_print_text` policy.
- `tests/unit/test_text_contrast.py` (**create**) — luminance/palette/ink choice.
- `tests/gui/test_body_label.py` (**create**) — scaled item + SmdChip parity.
- `tests/unit/test_layer_state_pin_reveal.py` (**create**) — pin-name reveal override.
- `tests/gui/test_hover_reveal.py` (**create**) — dwell → tooltip / reveal branch.

---

## Task 1: Capacitor value coding

Add capacitor decoding to the existing shared value-code helpers. Capacitor markings are conventionally in **pF** (EIA 3-digit: `100 nF` → `104`), and `code_digits`/`smd_code` are unit-agnostic, so `parse_capacitance` must return **pF**.

**Files:**
- Modify: `src/protoboard/gui/renderers/value_codes.py`
- Test: `tests/unit/test_value_codes.py`

**Interfaces:**
- Consumes: `_scaled_number`, `code_digits`, `smd_code`, `ComponentClass` (all already in `value_codes.py`).
- Produces:
  - `parse_capacitance(text: str) -> float | None` — capacitance in **pF**, `None` if unparseable.
  - `parse_value(component_class: str, text: str) -> float | None` — extended so `CAPACITOR_POLAR`/`CAPACITOR_NONPOLAR` decode via `parse_capacitance`.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_value_codes.py`:

```python
from protoboard.gui.renderers.value_codes import parse_capacitance, parse_value
from protoboard.domain.components.definition import ComponentClass


class TestParseCapacitance:

    @pytest.mark.parametrize(("text", "pf"), [
        ("100nF", 100_000.0),
        ("100n", 100_000.0),
        ("1uF", 1_000_000.0),
        ("1u", 1_000_000.0),
        ("22pF", 22.0),
        ("22p", 22.0),
        ("4p7", 4.7),
        ("4.7pF", 4.7),
        ("10µF", 10_000_000.0),
        ("1nF", 1_000.0),
    ])
    def test_parses_to_pf(self, text: str, pf: float) -> None:
        assert parse_capacitance(text) == pytest.approx(pf)

    @pytest.mark.parametrize("text", ["?", "", "abc", "10n5n"])
    def test_rejects(self, text: str) -> None:
        assert parse_capacitance(text) is None


class TestParseValueCapacitor:

    @pytest.mark.parametrize("cls", [
        ComponentClass.CAPACITOR_POLAR,
        ComponentClass.CAPACITOR_NONPOLAR,
    ])
    def test_capacitor_decodes(self, cls: ComponentClass) -> None:
        # 100 nF = 100000 pF -> smd_code -> "104"
        assert parse_value(cls, "100nF") == pytest.approx(100_000.0)

    def test_other_class_none(self) -> None:
        assert parse_value(ComponentClass.RESISTOR, "100nF") != 100_000.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_value_codes.py::TestParseCapacitance -v`
Expected: FAIL with `ImportError: cannot import name 'parse_capacitance'`.

- [ ] **Step 3: Write minimal implementation**

In `src/protoboard/gui/renderers/value_codes.py`, add `parse_capacitance` after `parse_inductance`, extend `parse_value`, and add `parse_capacitance` to `__all__`:

```python
def parse_capacitance(text: str) -> float | None:
    """A capacitor ``value`` string → picofarads (``None`` when unparseable).

    Capacitor markings are conventionally expressed in pF (the EIA 3-digit
    code), so pF is the unit that feeds ``code_digits`` / ``smd_code``.  A bare
    number means pF; ``p``/``n``/``u``/``µ``/``m`` scale up from there.
    """
    s = text.strip()
    s = s.removesuffix("F").removesuffix("f").strip()
    return _scaled_number(s, {
        "": 1.0, "p": 1.0, "P": 1.0,
        "n": 1e3, "N": 1e3,
        "u": 1e6, "µ": 1e6, "U": 1e6,
        "m": 1e9, "M": 1e9,
    })
```

Then in `parse_value`, before the final `return None`:

```python
    if component_class in (
        ComponentClass.CAPACITOR_POLAR,
        ComponentClass.CAPACITOR_NONPOLAR,
    ):
        return parse_capacitance(text)
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_value_codes.py -v`
Expected: PASS (existing + new).

- [ ] **Step 5: Commit**

```bash
git add src/protoboard/gui/renderers/value_codes.py tests/unit/test_value_codes.py
git commit -m "feat(renderers): capacitor value coding (pF EIA) in value_codes"
```

---

## Task 2: `value_text.body_print_text` — what to print

The single policy for the on-body string: R/L/C → code, everything else → raw `value`, unset → nothing.

**Files:**
- Create: `src/protoboard/domain/components/value_text.py`
- Test: `tests/unit/test_value_text.py`

**Interfaces:**
- Consumes: `parse_value`, `smd_code` from `value_codes.py`; `ComponentClass`.
- Produces: `body_print_text(component_class: str, value: str) -> str | None` — the string to print on the body, or `None` (draw nothing).

**Note on layering:** `value_codes.py` lives under `gui/renderers/` but is pure (no Qt). Importing it from `domain/` is a pre-existing wrinkle, not introduced here — `value_codes` imports only `re` + `ComponentClass`. Keep `value_text.py` Qt-free; a follow-up may relocate `value_codes` to `domain/`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_value_text.py`:

```python
# SPDX-License-Identifier: MIT
"""body_print_text policy: R/L/C code, other classes raw, unset -> None."""

from __future__ import annotations

import pytest

from protoboard.domain.components.definition import ComponentClass
from protoboard.domain.components.value_text import body_print_text


class TestBodyPrintText:

    def test_resistor_codes(self) -> None:
        assert body_print_text(ComponentClass.RESISTOR, "22k") == "223"

    def test_inductor_codes(self) -> None:
        assert body_print_text(ComponentClass.INDUCTOR, "10") == "100"

    def test_capacitor_codes(self) -> None:
        assert body_print_text(ComponentClass.CAPACITOR_NONPOLAR, "100nF") == "104"

    def test_part_number_passthrough(self) -> None:
        assert body_print_text("transistor_bjt", "BC547") == "BC547"

    @pytest.mark.parametrize("value", ["", "?"])
    def test_unset_none(self, value: str) -> None:
        assert body_print_text("transistor_bjt", value) is None

    def test_unparseable_resistor_falls_back_to_raw(self) -> None:
        # A resistor whose value won't decode still prints something legible.
        assert body_print_text(ComponentClass.RESISTOR, "wat") == "wat"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_value_text.py -v`
Expected: FAIL with `ModuleNotFoundError: ... value_text`.

- [ ] **Step 3: Write minimal implementation**

Create `src/protoboard/domain/components/value_text.py`:

```python
# SPDX-License-Identifier: MIT
"""What text a component prints on its body.

One policy point: resistors/inductors/capacitors print their coded marking
(``22k`` → ``223``); every other class prints its raw ``value`` (the part
number for a concrete part like ``BC547``).  An unset value (``""`` / ``"?"``)
prints nothing.  Pure — no Qt — so the policy is testable headless.
"""

from __future__ import annotations

from protoboard.gui.renderers.value_codes import parse_value, smd_code


def body_print_text(component_class: str, value: str) -> str | None:
    """The string to print on the body, or ``None`` to draw nothing."""
    text = value.strip()
    if not text or text == "?":
        return None
    decoded = parse_value(component_class, text)
    if decoded is not None:
        code = smd_code(decoded)
        if code is not None:
            return code
        # A coded class whose value did not fit a code still prints the raw
        # string rather than nothing (better a legible value than a blank body).
    return text
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_value_text.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/protoboard/domain/components/value_text.py tests/unit/test_value_text.py
git commit -m "feat(components): body_print_text policy (code vs raw part number)"
```

---

## Task 3: `text_contrast.best_ink` — luminance-driven ink colour

Pure WCAG luminance + best-contrast-from-palette. The luminance/contrast core takes `(r,g,b)` floats (zero Qt); `best_ink` is the thin `QColor` boundary.

**Files:**
- Create: `src/protoboard/gui/renderers/text_contrast.py`
- Test: `tests/unit/test_text_contrast.py`

**Interfaces:**
- Consumes: `QColor` (PySide6) at the boundary only.
- Produces:
  - `relative_luminance(rgb: tuple[float, float, float]) -> float` — WCAG L, channels 0..1.
  - `contrast_ratio(l1: float, l2: float) -> float`.
  - `DARK_INKS: list[str]`, `LIGHT_INKS: list[str]` — hex palettes.
  - `best_ink(body_color: QColor) -> QColor` — the chosen ink.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_text_contrast.py`:

```python
# SPDX-License-Identifier: MIT
"""Luminance-driven ink: palette by luminance, ink by best contrast."""

from __future__ import annotations

from PySide6.QtGui import QColor

from protoboard.gui.renderers.text_contrast import (
    DARK_INKS,
    LIGHT_INKS,
    best_ink,
    contrast_ratio,
    relative_luminance,
)


def _hex(c: QColor) -> str:
    return c.name().lower()


class TestLuminance:

    def test_white_is_one(self) -> None:
        assert relative_luminance((1.0, 1.0, 1.0)) == 1.0

    def test_black_is_zero(self) -> None:
        assert relative_luminance((0.0, 0.0, 0.0)) == 0.0

    def test_green_brighter_than_blue(self) -> None:
        assert relative_luminance((0.0, 1.0, 0.0)) > relative_luminance((0.0, 0.0, 1.0))


class TestContrastRatio:

    def test_white_black_is_21(self) -> None:
        assert contrast_ratio(1.0, 0.0) == 21.0


class TestBestInk:

    def test_light_body_gets_dark_ink(self) -> None:
        ink = _hex(best_ink(QColor("#f0f0f0")))
        assert ink in {h.lower() for h in DARK_INKS}

    def test_dark_body_gets_light_ink(self) -> None:
        ink = _hex(best_ink(QColor("#101010")))
        assert ink in {h.lower() for h in LIGHT_INKS}

    def test_saturated_blue_body_gets_light_ink(self) -> None:
        # Blue has low luminance -> treated as a dark body -> light ink.
        ink = _hex(best_ink(QColor("#0000ff")))
        assert ink in {h.lower() for h in LIGHT_INKS}

    def test_white_body_picks_black_ink(self) -> None:
        # Black is the highest-contrast dark ink on a white body.
        assert _hex(best_ink(QColor("#ffffff"))) == "#000000"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_text_contrast.py -v`
Expected: FAIL with `ModuleNotFoundError: ... text_contrast`.

- [ ] **Step 3: Write minimal implementation**

Create `src/protoboard/gui/renderers/text_contrast.py`:

```python
# SPDX-License-Identifier: MIT
"""Ink colour for body-printed text, chosen from the body's luminance.

WCAG relative luminance picks the palette (light body → a dark ink, dark body
→ a light ink); the highest WCAG contrast ratio picks the ink within it.  The
two-stage rule is correctness-by-construction: a dark body can never receive a
dark ink regardless of arithmetic.  Palettes are plain hex lists, tunable in
one line.  The luminance / contrast core is Qt-free ``(r, g, b)`` math; only
``best_ink`` touches ``QColor`` (a value type) at the boundary.
"""

from __future__ import annotations

from PySide6.QtGui import QColor

#: Inks for light bodies (black, graphite, navy).
DARK_INKS: list[str] = ["#000000", "#2b2b2b", "#0d1b3e"]
#: Inks for dark bodies (white, light-grey, yellow).
LIGHT_INKS: list[str] = ["#ffffff", "#c8c8c8", "#ffd400"]

_LUMA_THRESHOLD = 0.5


def _chan_lin(c: float) -> float:
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb: tuple[float, float, float]) -> float:
    """WCAG relative luminance for sRGB channels in 0..1."""
    r, g, b = (_chan_lin(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(l1: float, l2: float) -> float:
    """WCAG contrast ratio between two relative luminances."""
    hi, lo = (l1, l2) if l1 >= l2 else (l2, l1)
    return (hi + 0.05) / (lo + 0.05)


def _rgb01(c: QColor) -> tuple[float, float, float]:
    return (c.redF(), c.greenF(), c.blueF())


def best_ink(body_color: QColor) -> QColor:
    """The highest-contrast ink for *body_color* from the luminance palette."""
    body_lum = relative_luminance(_rgb01(body_color))
    palette = DARK_INKS if body_lum > _LUMA_THRESHOLD else LIGHT_INKS
    best = max(
        palette,
        key=lambda hexs: contrast_ratio(
            body_lum, relative_luminance(_rgb01(QColor(hexs)))
        ),
    )
    return QColor(best)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_text_contrast.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/protoboard/gui/renderers/text_contrast.py tests/unit/test_text_contrast.py
git commit -m "feat(renderers): luminance-driven best_ink for body-printed text"
```

---

## Task 4: `body_label.make_body_label` — geometric scale-to-fit label

Generalise `SmdChipRenderer._code_label`'s scale-to-fit into a shared helper any body renderer can call. This task adds the helper + tests it in isolation; Tasks 5–6 wire it in.

**Files:**
- Create: `src/protoboard/gui/renderers/packages/body_label.py`
- Test: `tests/gui/test_body_label.py`

**Interfaces:**
- Consumes: `QGraphicsSimpleTextItem`, `QTransform`, `QBrush`, `QColor` (PySide6); `Point` (`protoboard.geometry`); `BodyCanvas.Z_STEP`.
- Produces:
  - `make_body_label(text: str, ink: QColor, center: Point, angle_deg: float, avail_len: float, avail_w: float, *, alpha: float, z: float) -> QGraphicsSimpleTextItem | None` — a scaled, centred, rotated text item, or `None` if it can't fit / measure. `ink` alpha is multiplied by `alpha`.

- [ ] **Step 1: Write the failing test**

Create `tests/gui/test_body_label.py`:

```python
# SPDX-License-Identifier: MIT
"""make_body_label: scaled, centred, alpha-multiplied text item."""

from __future__ import annotations

from PySide6.QtGui import QColor

from protoboard.geometry import Point
from protoboard.gui.renderers.packages.body_label import make_body_label


def test_returns_item_scaled_within_bounds(qtbot) -> None:
    item = make_body_label(
        "223", QColor("#000000"), Point(0.0, 0.0),
        angle_deg=0.0, avail_len=4.0, avail_w=2.0, alpha=1.0, z=5.0,
    )
    assert item is not None
    br = item.mapRectToScene(item.boundingRect())
    assert br.width() <= 4.0 + 1e-6
    assert br.height() <= 2.0 + 1e-6


def test_alpha_multiplies_ink(qtbot) -> None:
    item = make_body_label(
        "223", QColor("#ffffff"), Point(0.0, 0.0),
        angle_deg=0.0, avail_len=4.0, avail_w=2.0, alpha=0.5, z=5.0,
    )
    assert item is not None
    # QColor stores alpha as a 16-bit integer channel, so setAlphaF(0.5) does
    # NOT round-trip to exactly 0.5 (~0.50000763). Assert with a tight approx.
    assert item.brush().color().alphaF() == pytest.approx(0.5, abs=1e-4)


def test_empty_text_is_none(qtbot) -> None:
    assert make_body_label(
        "", QColor("#000000"), Point(0.0, 0.0),
        angle_deg=0.0, avail_len=4.0, avail_w=2.0, alpha=1.0, z=5.0,
    ) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/gui/test_body_label.py -v`
Expected: FAIL with `ModuleNotFoundError: ... body_label`.

- [ ] **Step 3: Write minimal implementation**

Create `src/protoboard/gui/renderers/packages/body_label.py`:

```python
# SPDX-License-Identifier: MIT
"""The value text printed on a component body, scaled to fit.

Generalises the SMD chip's ``_code_label``: given the string, ink, the body
centre and axis angle, and the available (length × width) box, produce a
``QGraphicsSimpleTextItem`` scaled to fit and centred, flipped upright so it
never reads upside-down.  Shared by every body renderer that prints a value.
"""

from __future__ import annotations

from PySide6.QtGui import QBrush, QColor, QTransform
from PySide6.QtWidgets import QGraphicsSimpleTextItem

from protoboard.geometry import Point


def make_body_label(
    text: str,
    ink: QColor,
    center: Point,
    angle_deg: float,
    avail_len: float,
    avail_w: float,
    *,
    alpha: float,
    z: float,
) -> QGraphicsSimpleTextItem | None:
    """A scaled, centred, upright value label, or ``None`` if it can't fit."""
    if not text or avail_len <= 0.0 or avail_w <= 0.0:
        return None
    label = QGraphicsSimpleTextItem(text)
    pen_ink = QColor(ink)
    pen_ink.setAlphaF(pen_ink.alphaF() * alpha)
    label.setBrush(QBrush(pen_ink))
    br = label.boundingRect()
    if br.width() < 1e-9 or br.height() < 1e-9:
        return None
    scale = min(avail_len / br.width(), avail_w / br.height())
    angle = angle_deg
    if not -90.0 < angle <= 90.0:
        angle += 180.0 if angle <= 0 else -180.0
    t = QTransform()
    t.translate(center.x, center.y)
    t.rotate(angle)
    t.scale(scale, scale)
    t.translate(-br.width() / 2.0, -br.height() / 2.0)
    label.setTransform(t)
    label.setZValue(z)
    return label
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/gui/test_body_label.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/protoboard/gui/renderers/packages/body_label.py tests/gui/test_body_label.py
git commit -m "feat(renderers): make_body_label scale-to-fit body value helper"
```

---

## Task 5: Refactor `SmdChipRenderer` onto the shared helpers (parity)

Route the SMD chip's on-body code through `best_ink` + `make_body_label` instead of its bespoke `_code_label` + hardcoded `CODE_COLOR`. Geometry/rotation stay identical; the only visible change is the ink is now luminance-driven (still light on the dark chip body).

**Files:**
- Modify: `src/protoboard/gui/renderers/packages/prism_kinds.py`
- Test: `tests/gui/test_value_code_renderers.py` (existing — repoint/parity)

**Interfaces:**
- Consumes: `body_print_text` (Task 2), `best_ink` (Task 3), `make_body_label` (Task 4), `self.paint.fill` / `self.paint.alpha` / `self.paint.z` (base renderer).

- [ ] **Step 1: Add a parity test for the SMD code still rendering**

Inspect `tests/gui/test_value_code_renderers.py` for the existing SMD-code assertion. Add (or adapt) a test that a placed SMD resistor with `value="22k"` still produces a text item reading `"223"`:

```python
def test_smd_resistor_prints_code(qtbot) -> None:
    # Reuse whatever helper the file already uses to build an SmdChipRenderer
    # for a 2-pin prism_smd resistor with value "22k"; assert a
    # QGraphicsSimpleTextItem with text "223" is among render_body's items.
    ...
```

If the file already asserts this, keep that assertion as the parity guard and skip adding a duplicate.

- [ ] **Step 2: Run the existing SMD renderer tests (baseline green)**

Run: `uv run pytest tests/gui/test_value_code_renderers.py -v`
Expected: PASS (records the pre-refactor baseline).

- [ ] **Step 3: Refactor `_code_label`**

In `prism_kinds.py`:
- Add imports: `from protoboard.domain.components.value_text import body_print_text`, `from protoboard.gui.renderers.text_contrast import best_ink`, `from protoboard.gui.renderers.packages.body_label import make_body_label`.
- Replace the body of `_code_label` so it computes the text via `body_print_text(self._component_class, self._instance.value)` (return `None` when no instance), the ink via `best_ink(self.paint.fill)`, the centre as `Point((p1.x+p2.x)/2, (p1.y+p2.y)/2)`, the angle as `(p2 - p1).angle_deg()`, `avail_len = body_len * 0.9 - 2.0 * cap`, `avail_w = body_w * 0.75`, `z = self.paint.z + 2 * BodyCanvas.Z_STEP`, and delegate to `make_body_label(...)`:

```python
    def _code_label(
        self,
        p1: Point,
        p2: Point,
        body_len: float,
        body_w: float,
        cap: float,
    ) -> QGraphicsItem | None:
        """The printed value code, scaled into the body between the caps."""
        if self._instance is None:
            return None
        text = body_print_text(self._component_class, self._instance.value)
        if text is None:
            return None
        center = Point((p1.x + p2.x) / 2.0, (p1.y + p2.y) / 2.0)
        return make_body_label(
            text,
            best_ink(self.paint.fill),
            center,
            (p2 - p1).angle_deg(),
            body_len * 0.9 - 2.0 * cap,
            body_w * 0.75,
            alpha=self.paint.alpha,
            z=self.paint.z + 2 * BodyCanvas.Z_STEP,
        )
```
- Remove the now-unused `_smd_code_text` method and the `CODE_COLOR` constant and the `parse_value`/`smd_code` import if nothing else in the file uses them (grep first; `qcolor_from_css` may still be used by the caps code — keep it).

- [ ] **Step 4: Run tests to verify parity**

Run: `uv run pytest tests/gui/test_value_code_renderers.py tests/unit/test_smd_mount_mechanic.py -v`
Expected: PASS — the `"223"` code still renders.

- [ ] **Step 5: Commit**

```bash
git add src/protoboard/gui/renderers/packages/prism_kinds.py tests/gui/test_value_code_renderers.py
git commit -m "refactor(renderers): SmdChip code via body_print_text + best_ink + make_body_label"
```

---

## Task 6: Print value on the generic body path

Wire the helper into the base `render_body` so **every** body renderer (TO92, TO220, DIP-N, cylinders, discs, …) that falls through the generic path prints its value. The body's axis and box come from the package bbox.

**Files:**
- Modify: `src/protoboard/gui/renderers/packages/base.py`
- Test: `tests/gui/test_body_label.py` (extend) or a small new `tests/gui/test_generic_body_value.py`

**Interfaces:**
- Consumes: `body_print_text`, `best_ink`, `make_body_label`; `bbox_or_none` (already imported in `base.py`); `self.paint`, `self._component_class`, `self._instance`, `self._pkg`.
- Produces: generic body renderers now append a value label to `render_body`'s items.

**Design note:** add a small helper on `PackageRenderer`, `_maybe_value_label(positions) -> QGraphicsItem | None`, and append it in `render_body`'s bbox branch (and radial branch). Use the package bbox for the box: `bb = bbox_or_none(self._pkg)`; centre = bbox centre in world via the placement transform; `avail_len`/`avail_w` from bbox dims × the same 0.9 / 0.75 factors. For the generic (non-2-pin-axis) case, angle = the placement rotation of the body (screen-aligned text is acceptable — matches how the SMD path derives angle from geometry). Keep it defensive: any missing dims → return `None` (no label, never a crash).

- [ ] **Step 1: Write the failing test**

Create `tests/gui/test_generic_body_value.py`:

```python
# SPDX-License-Identifier: MIT
"""A generic (non-SMD) body prints its raw value/part number."""

from __future__ import annotations

from PySide6.QtWidgets import QGraphicsSimpleTextItem


def _texts(items) -> list[str]:
    return [it.text() for it in items if isinstance(it, QGraphicsSimpleTextItem)]


def test_to92_prints_part_number(qtbot, <build-helpers>) -> None:
    # Build a TO92 (or any non-prism_smd body) renderer for an instance with
    # value="BC547"; render_body should include a text item reading "BC547".
    renderer = <build a TO92 renderer with instance.value="BC547">
    items = renderer.draw(<positions>, <fill>, <border>, 1.0, 0.0)
    assert "BC547" in _texts(items)


def test_empty_value_prints_nothing(qtbot, <build-helpers>) -> None:
    renderer = <same body, instance.value="?">
    items = renderer.draw(<positions>, <fill>, <border>, 1.0, 0.0)
    assert _texts(items) == []
```

> Implementer: fill `<build-helpers>` / `<positions>` from the existing renderer test fixtures — grep `tests/gui/` for how a `PackageRenderer` is constructed with an instance and drawn (e.g. `test_value_code_renderers.py`, `test_modular_renderers*`). Reuse that exact construction idiom.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/gui/test_generic_body_value.py -v`
Expected: FAIL — no `"BC547"` text item yet.

- [ ] **Step 3: Implement `_maybe_value_label` and call it in `render_body`**

In `base.py`, add imports (`body_print_text`, `best_ink`, `make_body_label`, `Point` is already imported) and:

```python
    def _maybe_value_label(
        self, positions: list[Point],
    ) -> QGraphicsItem | None:
        """The component value printed on the body, or ``None``.

        Text from :func:`body_print_text`, ink from :func:`best_ink` on the
        resolved body fill, scaled into the package bbox.  Any missing dims →
        ``None`` (never blocks the body drawing).
        """
        if self._instance is None:
            return None
        text = body_print_text(self._component_class, self._instance.value)
        if text is None:
            return None
        bb = bbox_or_none(self._pkg)
        if bb is None:
            return None
        (lo_x, lo_y), (hi_x, hi_y) = bb
        body_len = hi_x - lo_x
        body_w = hi_y - lo_y
        if body_len <= 0.0 or body_w <= 0.0:
            return None
        local_center = Point((lo_x + hi_x) / 2.0, (lo_y + hi_y) / 2.0)
        center = self._to_world(local_center)   # existing local→world seam
        angle = self._body_angle_deg()          # existing rotation accessor
        return make_body_label(
            text, best_ink(self.paint.fill), center, angle,
            body_len * 0.9, body_w * 0.75,
            alpha=self.paint.alpha,
            z=self.paint.z + 3 * BodyCanvas.Z_STEP,
        )
```

> Implementer: `_to_world` / `_body_angle_deg` are placeholders for the renderer's existing local→world transform and body rotation. **First** grep `base.py` for how `_draw_bbox` / `render_body` maps local package coords to world — look for `_qtransform`, `self._placement`, `Transform2D`, `to_qtransform`, and the `Point` the bbox rect is drawn at. The body outline is already drawn in world coords by `_draw_bbox`, so there is an existing transform that maps a local `Point` → world; call exactly that. For the angle, use the same placement rotation `_draw_bbox` applies (often 0 for an axis-aligned bbox — screen-aligned text is acceptable, matching the SMD path which derives angle from geometry). If a clean transform accessor does not exist, compute the world centre as the centroid of `positions` (the pins are already world-space) rather than inventing a transform — the pin centroid is a good-enough body centre and needs no new seam. Do NOT invent a new `Transform2D`.

Then, in `render_body`, append the label to both branches before returning:

```python
    def render_body(self, positions: list[Point]) -> list[QGraphicsItem]:
        radial = self._maybe_radial(positions)
        items = radial if radial is not None else self._draw_bbox(positions)
        label = self._maybe_value_label(positions)
        if label is not None:
            items.append(label)
        return items
```

Note: `SmdChipRenderer.render_body` calls `super().render_body(...)`, so it would now get a *second* label from the generic path. Guard against that — in `SmdChipRenderer`, override `_maybe_value_label` to return `None` (it draws its own between-caps label), OR have the base `_maybe_value_label` skip when `self._pkg` is a `prism_smd`. Prefer the override (explicit, local):

```python
    # In SmdChipRenderer:
    def _maybe_value_label(self, positions):
        return None  # the between-caps _code_label handles this body
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/gui/test_generic_body_value.py tests/gui/test_value_code_renderers.py -v`
Expected: PASS — TO92 prints `BC547`, SMD still prints `223` exactly once.

- [ ] **Step 5: Run the renderer suite for regressions**

Run: `uv run pytest tests/gui/ -k "render or body or value" -q`
Expected: PASS (no double-labels, no crashes on bodies without dims).

- [ ] **Step 6: Commit**

```bash
git add src/protoboard/gui/renderers/packages/base.py src/protoboard/gui/renderers/packages/prism_kinds.py tests/gui/test_generic_body_value.py
git commit -m "feat(renderers): print component value on the generic body path"
```

---

## Task 7: `HoverEvent` carries `mods`

The dwell branch needs to know if Alt is held. `_fire_hover` already has `mods`.

**Files:**
- Modify: `src/protoboard/controller/ports/hover.py`
- Modify: `src/protoboard/gui/tool_host.py` (`_fire_hover`)
- Test: `tests/unit/test_hover_event_mods.py` (create)

**Interfaces:**
- Consumes: `Mods` (`protoboard.controller.ports.inputs`).
- Produces: `HoverEvent.mods: Mods` (defaulted so existing constructors still work); `empty_action` implementations may set it, and `_fire_hover` passes the throttled `mods` into the published event.

**Design note:** `empty_action(point, mods)` is the tool method; the simplest wiring is for `_fire_hover` to attach `mods` to the returned `HoverEvent` via `dataclasses.replace`, avoiding touching every tool's `empty_action`. Keep `HoverEvent` frozen; use `replace`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_hover_event_mods.py`:

```python
# SPDX-License-Identifier: MIT
"""HoverEvent carries the modifier state (Alt) for hover-reveal branching."""

from __future__ import annotations

import dataclasses

from protoboard.controller.ports.hover import HoverEvent
from protoboard.controller.ports.inputs import Mods
from protoboard.geometry import Point


def test_hover_event_default_mods_empty() -> None:
    ev = HoverEvent(point=Point(0.0, 0.0))
    assert ev.mods == Mods()  # or whatever the empty Mods sentinel is


def test_hover_event_replace_sets_mods() -> None:
    ev = HoverEvent(point=Point(0.0, 0.0))
    ev2 = dataclasses.replace(ev, mods=Mods(alt=True))
    assert ev2.mods.alt is True
```

> Implementer: check `controller/ports/inputs.py` for the real `Mods` shape (fields, empty construction). Adapt the two asserts to the actual API — the point is only that `mods` round-trips.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_hover_event_mods.py -v`
Expected: FAIL — `HoverEvent` has no `mods` field.

- [ ] **Step 3: Add `mods` to `HoverEvent` and wire `_fire_hover`**

In `hover.py`:

```python
if TYPE_CHECKING:
    from protoboard.geometry import Point
    from protoboard.controller.ports.inputs import Mods


@dataclass(frozen=True, slots=True)
class HoverEvent:
    point: Point
    objects: tuple[uuid.UUID, ...] = field(default_factory=tuple)
    mods: "Mods" = field(default_factory=lambda: _empty_mods())
```

> Implementer: replace `_empty_mods()` with the real empty-`Mods` construction from `inputs.py` (e.g. `Mods()`), imported at runtime (not just TYPE_CHECKING) since it's a default factory. If `Mods` is frozen/hashable, a module-level `_EMPTY_MODS = Mods()` used as `default=` is cleaner than a factory.

In `tool_host.py` `_fire_hover`:

```python
    def _fire_hover(self, parked: tuple[Point, Mods]) -> None:
        tool = self._active_tool
        if tool is None:
            return
        point, mods = parked
        from dataclasses import replace
        ev = replace(tool.empty_action(point, mods), mods=mods)
        self._view.notify_hover(ev)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_hover_event_mods.py -v`
Expected: PASS.

- [ ] **Step 5: Run the controller-import guard**

Run: `uv run pytest tests/unit/test_controller_no_gui_import.py -v`
Expected: PASS — `hover.py` still imports no `gui`.

- [ ] **Step 6: Commit**

```bash
git add src/protoboard/controller/ports/hover.py src/protoboard/gui/tool_host.py tests/unit/test_hover_event_mods.py
git commit -m "feat(hover): HoverEvent carries modifier state for reveal branching"
```

---

## Task 8: Pin-name reveal override on `LayerState`

Add a per-component reveal id so the existing pin-name render path draws for one component while the layer is globally hidden.

**Files:**
- Modify: `src/protoboard/gui/layer_state.py`
- Modify: `src/protoboard/gui/renderers/component_renderer.py`
- Test: `tests/unit/test_layer_state_pin_reveal.py` (create)

**Interfaces:**
- Consumes: `uuid.UUID`, `LayerVisibility`.
- Produces:
  - `LayerState.pin_names_reveal_cid: uuid.UUID | None = None`.
  - `alpha_for_pin_names(side, cid=None) -> float` — returns the side-tool alpha (as if FULL) when the layer is HIDDEN **and** `cid == pin_names_reveal_cid`, else the normal override result.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_layer_state_pin_reveal.py`:

```python
# SPDX-License-Identifier: MIT
"""Per-component pin-name reveal: one component shows names while hidden."""

from __future__ import annotations

import uuid

from protoboard.gui.layer_state import LayerState, LayerOverrides, LayerVisibility


def _hidden_pins_state(reveal=None) -> LayerState:
    return LayerState(
        overrides=LayerOverrides(pin_names=LayerVisibility.HIDDEN),
        pin_names_reveal_cid=reveal,
    )


def test_hidden_layer_zero_alpha_for_others() -> None:
    st = _hidden_pins_state(reveal=uuid.uuid4())
    assert st.alpha_for_pin_names("top", cid=uuid.uuid4()) == 0.0


def test_revealed_component_gets_full_alpha() -> None:
    cid = uuid.uuid4()
    st = _hidden_pins_state(reveal=cid)
    assert st.alpha_for_pin_names("top", cid=cid) > 0.0


def test_no_reveal_still_hidden() -> None:
    cid = uuid.uuid4()
    st = _hidden_pins_state(reveal=None)
    assert st.alpha_for_pin_names("top", cid=cid) == 0.0


def test_visible_layer_unaffected_by_cid() -> None:
    cid = uuid.uuid4()
    st = LayerState()  # pin_names defaults to FULL
    assert st.alpha_for_pin_names("top", cid=cid) > 0.0
    assert st.alpha_for_pin_names("top") > 0.0  # cid optional
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_layer_state_pin_reveal.py -v`
Expected: FAIL — no `pin_names_reveal_cid` / `cid` kwarg.

- [ ] **Step 3: Implement the override**

In `layer_state.py`, add the field to `LayerState` (after `overrides`):

```python
    #: When the pin_names layer is HIDDEN, the one component id whose pin
    #: names are still drawn (hover+Alt reveal).  None = strict hidden.
    pin_names_reveal_cid: uuid.UUID | None = None
```

(Ensure `import uuid` at the top of the file.)

Replace `alpha_for_pin_names`:

```python
    def alpha_for_pin_names(
        self, side: Side, cid: uuid.UUID | None = None,
    ) -> float:
        # Pin names are independent of labels and components.  A per-component
        # reveal (hover+Alt) forces this one component's names on while the
        # layer is globally HIDDEN.
        if (
            self.overrides.pin_names == LayerVisibility.HIDDEN
            and cid is not None
            and cid == self.pin_names_reveal_cid
        ):
            return self._side_tool_alpha(side)
        return self._vis_alpha(self.overrides.pin_names, self._side_tool_alpha(side))
```

- [ ] **Step 4: Thread `cid` at the render call site**

In `component_renderer.py` line ~619, pass the component id (the variable the render loop already has for this instance — grep for `inst.id` / `cid` / `placement` id in `_render_one`):

```python
    alpha_pin_names = layers.alpha_for_pin_names(side, cid=<the component uuid>) * group_alpha
```

> Implementer: use the exact id variable in scope (`inst.id` is likely). Do not fabricate one.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_layer_state_pin_reveal.py -v`
Expected: PASS.

- [ ] **Step 6: Run renderer + layer-state regressions**

Run: `uv run pytest tests/gui/ -k "render or pin_name or layer" -q; uv run pytest tests/unit/ -k layer_state -q`
Expected: PASS — `alpha_for_pin_names` callers without `cid` still work (default None).

- [ ] **Step 7: Commit**

```bash
git add src/protoboard/gui/layer_state.py src/protoboard/gui/renderers/component_renderer.py tests/unit/test_layer_state_pin_reveal.py
git commit -m "feat(layers): per-component pin-name reveal override"
```

---

## Task 9: Hover tooltip widget (2A)

A cursor-anchored widget showing `refdes value`.

**Files:**
- Create: `src/protoboard/gui/hover_tooltip.py`
- Test: `tests/gui/test_hover_reveal.py` (create — shared with Task 10; this task adds the tooltip half)

**Interfaces:**
- Consumes: `QWidget`/`QLabel` or `QGraphicsSimpleTextItem` on the scene; the view's map-scene-to-view.
- Produces:
  - `class HoverTooltip` with `show_at(text: str, view_pos)` and `hide()`.

**Design note:** simplest correct form is a `QLabel` child of the `board_view.viewport()` positioned at the cursor, or `QToolTip.showText`. Prefer a small `QLabel` you own (so hide/show is deterministic and testable) over `QToolTip` (global, timing-fragile). It shows a single string; no scene coordinates needed beyond placing it near the cursor in viewport pixels.

- [ ] **Step 1: Write the failing test**

Create `tests/gui/test_hover_reveal.py` (tooltip portion):

```python
# SPDX-License-Identifier: MIT
"""Hover-reveal: label tooltip (2A) and pin-name reveal (2B)."""

from __future__ import annotations

from protoboard.gui.hover_tooltip import HoverTooltip


def test_tooltip_shows_and_hides(qtbot) -> None:
    from PySide6.QtWidgets import QWidget
    parent = QWidget()
    qtbot.addWidget(parent)
    tip = HoverTooltip(parent)
    tip.show_at("R5 4.7k", (10, 20))
    assert tip.isVisible()
    assert "R5 4.7k" in tip.text()
    tip.hide()
    assert not tip.isVisible()
```

> Implementer: adapt `.text()` to whatever accessor the widget exposes (a `QLabel` has `.text()`).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/gui/test_hover_reveal.py::test_tooltip_shows_and_hides -v`
Expected: FAIL — no `hover_tooltip` module.

- [ ] **Step 3: Implement the widget**

Create `src/protoboard/gui/hover_tooltip.py`:

```python
# SPDX-License-Identifier: MIT
"""Cursor-anchored tooltip for a component's hidden label (hover-reveal 2A)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QWidget


class HoverTooltip(QLabel):
    """A small floating label shown near the cursor while ``labels`` is hidden."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setStyleSheet(
            "background: rgba(30,30,30,220); color: #f0f0f0;"
            " padding: 2px 5px; border-radius: 3px;"
        )
        self.hide()

    def show_at(self, text: str, view_pos: tuple[int, int]) -> None:
        self.setText(text)
        self.adjustSize()
        x, y = view_pos
        self.move(x + 12, y + 12)
        self.show()
        self.raise_()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/gui/test_hover_reveal.py::test_tooltip_shows_and_hides -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/protoboard/gui/hover_tooltip.py tests/gui/test_hover_reveal.py
git commit -m "feat(gui): cursor-anchored hover tooltip widget"
```

---

## Task 10: Dwell timer + hover-reveal controller (2A + 2B wiring)

Tie it together: a ~1 s dwell fires; if `labels` hidden and no Alt → show the tooltip with `refdes value`; if `pin_names` hidden and Alt → set `LayerState.pin_names_reveal_cid` and refresh. Any move/leave/cid-change/Alt-release clears.

**Files:**
- Create: `src/protoboard/gui/hover_reveal.py`
- Modify: `src/protoboard/gui/tool_host.py` (arm the dwell timer from `_fire_hover`) OR `src/protoboard/gui/board_view.py` (subscribe to `hover_published`) — see design note.
- Test: `tests/gui/test_hover_reveal.py` (extend)

**Interfaces:**
- Consumes: `HoverEvent` (with `mods`, Task 7); `LayerState` (`pin_names_reveal_cid`, `alpha_for_pin_names`, Task 8); `HoverTooltip` (Task 9); `TimerWrap` (`gui/throttle.py`); the view's current `LayerState`, refresh trigger, refdes/value lookup, and scene→view mapping.
- Produces:
  - `class HoverReveal` — `on_hover(event: HoverEvent, view_pos)` arms the dwell; `_on_dwell()` branches 2A/2B; `clear()` hides tooltip + clears reveal id + refreshes.

**Design note (where it lives):** `hover_published` currently has no subscriber. Add a `HoverReveal` owned by `board_view` (it needs the viewport, the `LayerState`, and the refresh). Connect `board_view.hover_published` → `HoverReveal.on_hover`. The dwell timer is a `TimerWrap(1000, self._on_dwell, restart_on_arm=True)` re-armed on every `on_hover`; store the latest event + cursor viewport pos to use when it fires. On mouse leave (or cursor-moved with no hit), call `clear()`.

The `refdes value` string: reuse `_compose_overlay_label(refdes, value)` from `component_renderer.py` if importable, else format `f"{refdes} {value}".strip()`. The refdes/value for a hovered `cid` come from the session's instances (`session.schematic` / instance lookup) — grep how `component_renderer` gets `inst.refdes` / `inst.value`.

- [ ] **Step 1: Write the failing test**

Extend `tests/gui/test_hover_reveal.py`:

```python
def test_dwell_labels_hidden_shows_tooltip(qtbot) -> None:
    # Build a HoverReveal with: labels HIDDEN, a component under cursor, no Alt.
    # Fire on_hover, force the dwell (call _on_dwell directly), assert the
    # tooltip is visible with the component's "refdes value".
    ...


def test_dwell_pin_names_hidden_alt_sets_reveal(qtbot) -> None:
    # labels visible, pin_names HIDDEN, component under cursor, Alt held.
    # After the dwell, LayerState.pin_names_reveal_cid == that component id,
    # and a refresh was requested.
    ...


def test_clear_hides_and_resets(qtbot) -> None:
    # After a reveal, clear() hides the tooltip and sets reveal id back to None.
    ...
```

> Implementer: build `HoverReveal` with test doubles for the view surface (a fake exposing `layer_state`, `refresh()`, `viewport()`, and an instance lookup). Keep the fake tiny — this is the seam that proves the branching without a full MainWindow. Call `_on_dwell()` directly rather than waiting on the real 1 s timer.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/gui/test_hover_reveal.py -k dwell -v`
Expected: FAIL — no `hover_reveal` module.

- [ ] **Step 3: Implement `HoverReveal`**

Create `src/protoboard/gui/hover_reveal.py`:

```python
# SPDX-License-Identifier: MIT
"""Hover-reveal controller: dwell → label tooltip (2A) or pin-name reveal (2B).

Subscribes to the board view's hover stream.  When the ``labels`` layer is
hidden, a ~1 s dwell over a component shows its ``refdes value`` in a tooltip
at the cursor.  When the ``pin_names`` layer is hidden and Alt is held, the
dwell instead switches that one component's pin names on via
``LayerState.pin_names_reveal_cid`` — the same render path the layer uses, for
one component only.  Any move / leave / Alt-release clears both.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from protoboard.gui.layer_state import LayerVisibility
from protoboard.gui.throttle import TimerWrap

if TYPE_CHECKING:
    from protoboard.controller.ports.hover import HoverEvent


class HoverReveal:
    def __init__(self, view) -> None:  # view: the board view surface
        self._view = view
        self._tooltip = <HoverTooltip over view.viewport()>
        self._timer = TimerWrap(1000, self._on_dwell, restart_on_arm=True)
        self._pending_cid: uuid.UUID | None = None
        self._pending_view_pos: tuple[int, int] = (0, 0)
        self._pending_alt: bool = False

    def on_hover(self, event: "HoverEvent", view_pos: tuple[int, int]) -> None:
        cid = event.objects[0] if event.objects else None
        # A move that changes the hovered component clears the current reveal.
        if cid != self._pending_cid:
            self.clear()
        self._pending_cid = cid
        self._pending_view_pos = view_pos
        self._pending_alt = bool(event.mods.alt)
        if cid is None:
            self._timer.stop()
            return
        self._timer.arm()

    def _on_dwell(self) -> None:
        cid = self._pending_cid
        if cid is None:
            return
        layers = self._view.layer_state
        if (layers.overrides.labels == LayerVisibility.HIDDEN
                and not self._pending_alt):
            text = self._label_for(cid)
            if text:
                self._tooltip.show_at(text, self._pending_view_pos)
            return
        if (layers.overrides.pin_names == LayerVisibility.HIDDEN
                and self._pending_alt):
            layers.pin_names_reveal_cid = cid
            self._view.refresh()

    def clear(self) -> None:
        self._tooltip.hide()
        layers = self._view.layer_state
        if layers.pin_names_reveal_cid is not None:
            layers.pin_names_reveal_cid = None
            self._view.refresh()

    def _label_for(self, cid: uuid.UUID) -> str | None:
        inst = <lookup instance by cid from view.session>
        if inst is None:
            return None
        return f"{inst.refdes} {inst.value}".strip()
```

> Implementer: replace the `<...>` placeholders with the real view API — `HoverTooltip(view.viewport())`, the view's `layer_state` accessor, its `refresh()` (grep how the scene composer / board_view triggers a re-render; e.g. `view.request_refresh()` or `scene_composer.refresh()`), and the instance-by-id lookup (grep `component_renderer` / `session` for how instances are indexed). Also confirm `Mods.alt` is the real attribute name (Task 7 established it).

- [ ] **Step 4: Wire it into the view**

In `board_view.py` (or wherever `hover_published` is emitted): construct a `HoverReveal(self)` in `__init__`, connect `self.hover_published.connect(lambda ev: self._hover_reveal.on_hover(ev, self._cursor_view_pos()))`, and call `self._hover_reveal.clear()` on leave / cursor-moved-to-empty.

> Implementer: `_cursor_view_pos()` is the current cursor in viewport pixels — grep for an existing cursor-position accessor; `notify_cursor_moved` already flows scene coords, and the raw event pos is available at the `handle_mouse_move` site. Route the viewport pixel pos alongside the hover if needed.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/gui/test_hover_reveal.py -v`
Expected: PASS.

- [ ] **Step 6: Run the hover / board-view regressions + smoke**

Run: `uv run pytest tests/gui/ -k "hover or board_view or tool_host" -q`
Expected: PASS.
Then smoke: `uv run python -m protoboard ...` via the `run-protoboard-designer` skill (headless) — confirm no crash on hover.

- [ ] **Step 7: Commit**

```bash
git add src/protoboard/gui/hover_reveal.py src/protoboard/gui/board_view.py tests/gui/test_hover_reveal.py
git commit -m "feat(gui): hover-reveal — dwell tooltip + per-component pin-name reveal"
```

---

## Task 11: Docs + follow-ups + full suite

**Files:**
- Modify: `docs/gui.md` (hover-reveal affordance), `docs/rendering.md` (body-printed value + ink model)
- Modify: `docs/todo.md` (follow-ups)
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Document the features**

Add to `docs/rendering.md`: body-printed value — `value_text.body_print_text` policy (R/L/C coded, rest raw), `text_contrast.best_ink` luminance model, `body_label.make_body_label`. Add to `docs/gui.md`: hover-reveal — hidden `labels` → dwell tooltip, hidden `pin_names` + Alt → per-component reveal.

- [ ] **Step 2: Record follow-ups in `docs/todo.md`**

```markdown
## Body-printed value / hover-reveal follow-ups (2026-07-14)
- Capacitor colour-code: dropped as YAGNI; revisit only on real need.
- Future `short-spec` / `short-description` instance field: the hover-label
  tooltip (2A) should show that instead of bare `refdes value` once it exists.
- Yellow ink (`#ffd400`) in `LIGHT_INKS`: drop if it fires too eagerly on
  mid-dark bodies (one-line change in `text_contrast.py`).
- `value_codes.py` is pure but lives under `gui/renderers/`; `value_text.py`
  (domain) imports it. Consider relocating `value_codes` to `domain/`.
```

- [ ] **Step 3: Update CHANGELOG**

Add a `feat` entry describing both features under the current unreleased section.

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS except the known pre-existing baseline failures (12 benchmark "missing R/PinHeader_1x04", test_debug_trace ×2, test_hit_test wire-hit, test_library_loader aliases — unchanged count). No new failures in renderers/hover/layers.

- [ ] **Step 5: mypy**

Run: `uv run mypy --strict src/protoboard/domain/components/value_text.py src/protoboard/gui/renderers/text_contrast.py src/protoboard/gui/renderers/packages/body_label.py src/protoboard/gui/hover_reveal.py src/protoboard/gui/hover_tooltip.py`
Expected: clean (fix any `# type: ignore` needed with a comment).

- [ ] **Step 6: Commit**

```bash
git add docs/gui.md docs/rendering.md docs/todo.md CHANGELOG.md
git commit -m "docs: body-printed value + hover-reveal; follow-ups in todo"
```

---

## Self-review notes (for the implementer)

- **Reuse over rebuild:** `code_digits`/`smd_code` already do the coding math; Task 1 only adds a pF parser + one policy branch. `SmdChipRenderer` already had the scale-to-fit — Task 4/5 lift it, they don't reinvent it.
- **Parity guard:** Task 5 keeps the SMD `"223"` assertion green through the refactor (CLAUDE.md "no silent regressions").
- **One signature change for 2B:** `alpha_for_pin_names` gains an optional `cid`; all existing callers keep working (default `None`).
- **Layering:** pure units (`value_text`, `text_contrast` core, capacitor parsing) are headless-testable; only drawing/tooltip/override touch Qt. The controller guard test (Task 7 step 5) confirms `hover.py` stays gui-free.
