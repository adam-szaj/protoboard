<!-- SPDX-License-Identifier: MIT -->
# Body-printed value — pin to body frame + fixed font — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the body-printed value text pinned to the component body's local frame (inheriting the body's rotation and flip, fixed relative to the body geometry — not the pin row, so it stays put under bendable pins), reading correctly (never mirrored/upside-down), running pin-1→increasing-pins along the body axis; and give it a fixed font size (one larger size for generic bodies, calibrated on TO92 2N2222/2N3904) while the SMD chip keeps its current scale-to-fit look.

**Architecture:** `make_body_label` is extended to take a body-frame `QTransform` basis (the same `_body_placement` the body outline is baked through) plus a size mode: `scale_to_fit` (SMD, unchanged look) OR fixed `point_size` (generic). The label transform composes the body placement with a local centre offset, a mirror-compensation for flipped placements (so glyphs read correctly), and the existing upright-flip. `_maybe_value_label` passes the placement + `box.center` + the new fixed font size; `SmdChipRenderer._code_label` routes through the same helper in `scale_to_fit` mode.

**Tech Stack:** PySide6 (`QTransform`, `QGraphicsSimpleTextItem`, `QFont`), pytest / pytest-qt.

## Global Constraints

- SPDX header on any new file; English docstrings/comments.
- `mypy --strict` must pass on changed files (add no NEW errors).
- Conventional Commits; explicit `git add <paths>` (NEVER `-A`/`.`).
- Tests: `uv run pytest` (use `-n auto` for the full suite; pytest-xdist present).
- Pre-existing baseline: 4 failed + 12 errors (12 benchmark missing-defs, 2 debug_trace, 1 hit_test wire, 1 library_loader aliases — the last is a known `-n auto` shard-order flake that may show 4↔5; re-run once to confirm). NO new failures in renderers.
- Pyright `reportMissingImports` / unused-`qtbot` diagnostics in this worktree are KNOWN false-positives; trust `uv run pytest` + mypy.
- **Parity guard:** the SMD "223" code must keep rendering (same look). `tests/gui/test_value_code_renderers.py` is the guard — keep it green.
- The SMD chip uses a 2-pin **pad-axis** frame (`axis_canvas`); the generic path uses the **body** frame (`_body_placement`). Keep SMD on its current geometry (do not force it through `_body_placement`).

## Context — current code (verified 2026-07-14)

- `make_body_label(text, ink, center, angle_deg, avail_len, avail_w, *, alpha, z)` — `src/protoboard/gui/renderers/packages/body_label.py:18-50`. Builds a `QGraphicsSimpleTextItem`, `scale = min(avail_len/br.width(), avail_w/br.height())`, upright-flips the angle, composes `translate(center)→rotate→scale→translate(-br/2)`. **Geometric scale only; no fixed font; no flip/mirror handling.**
- `PackageRenderer._maybe_value_label(positions)` — `base.py:542-573`. Currently: `placement = self._body_placement(positions)`; `world_pt = placement.map(QPointF(box.center))`; `angle = atan2(placement.m12(), placement.m11())`; calls `make_body_label(..., center=world_pt, angle, avail_len=box.width*0.9, avail_w=box.height*0.75, ...)`. **`atan2(m12,m11)` discards the flip (a reflection can't be a rotation angle), and rebuilding around a world point detaches the text from the body.**
- `SmdChipRenderer._code_label` — `prism_kinds.py:105-128`. Calls `make_body_label(..., center=midpoint(p1,p2), (p2-p1).angle_deg(), avail_len=body_len*0.9-2*cap, avail_w=body_w*0.75, ...)`.
- `_body_placement(positions) -> QTransform` — `base.py:635-659`. With a real placement it is `placement_transform(...)` = `translate ∘ R(rotation) ∘ M_flip(side)` — the full body pose incl. the bottom-side **reflection** (det < 0). Without one (bare `draw` in tests/previews) it is translate + pin1→pin2 rotation, **no flip**.
- Theme font-size fields — `controller/theme.py:94-95`: `refdes_font_size: float = 0.55`, `overlay_font_size: float = 15.0`. **No body-print font field yet.**
- `Point` (`geometry.py`) has `.x`/`.y`; `Vector.angle_deg()` at `geometry.py:131`.
- `bbox_or_none(self._pkg)` returns a `Rect` with `.width`/`.height`/`.center` (a `Point`) in the package-**local** frame (pin 1 at origin).

## File structure

- `src/protoboard/gui/renderers/packages/body_label.py` (**rewrite the function signature + body**) — body-frame transform basis + size mode + mirror compensation.
- `src/protoboard/gui/renderers/packages/base.py` (**modify** `_maybe_value_label`) — pass the placement transform + fixed font size.
- `src/protoboard/gui/renderers/packages/prism_kinds.py` (**modify** `SmdChipRenderer._code_label`) — call the new signature in `scale_to_fit` mode (keep look).
- `src/protoboard/controller/theme.py` (**modify**) — add `body_print_font_size`.
- Tests: `tests/gui/test_body_label.py` (**extend**), `tests/gui/test_generic_body_value.py` (**extend**), `tests/gui/test_value_code_renderers.py` (**parity guard, keep green**).

---

## Task 1: Add `body_print_font_size` to the theme

**Files:**
- Modify: `src/protoboard/controller/theme.py`
- Test: `tests/unit/test_theme.py` (if it exists; else assert via an existing theme test file — grep first)

**Interfaces:**
- Produces: `Theme.body_print_font_size: float` — the fixed point size for generic body-printed value text. Calibrated so a TO92 part number (e.g. `2N2222`) is legible and tidy. Start at `4.0` (raster units, in-scene — see Task 2 note on units) and tune in Task 4 against the screenshot.

- [ ] **Step 1: Write the failing test**

Grep for an existing theme test: `grep -rl "refdes_font_size\|overlay_font_size\|DEFAULT_THEME" tests/`. Add (to the file that already asserts theme defaults, or create `tests/unit/test_theme_body_font.py`):

```python
# SPDX-License-Identifier: MIT
from protoboard.controller.theme import DEFAULT_THEME, Theme


def test_body_print_font_size_default() -> None:
    assert Theme().body_print_font_size == 4.0
    assert DEFAULT_THEME.body_print_font_size == 4.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_theme_body_font.py -v` (or the file you added to)
Expected: FAIL — `AttributeError: ... body_print_font_size`.

- [ ] **Step 3: Add the field**

In `controller/theme.py`, in the `Theme` dataclass next to `refdes_font_size` / `overlay_font_size` (~line 94):

```python
    #: Fixed size (raster units, in-scene) for the value/part-number text
    #: printed on a generic component body (TO92/TO220/DIP/…). The SMD chip
    #: keeps its own scale-to-fit code label. Calibrated on a TO92 part
    #: number (2N2222 / 2N3904) — legible and tidy.
    body_print_font_size: float = 4.0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_theme_body_font.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/protoboard/controller/theme.py tests/unit/test_theme_body_font.py
git commit -m "feat(theme): body_print_font_size for generic body value text"
```

---

## Task 2: Extend `make_body_label` — body-frame basis, size mode, mirror compensation

Rewrite the helper so the text is pinned to a caller-supplied body-frame transform, reads correctly on flipped bodies, and supports either scale-to-fit (SMD) or a fixed point size (generic).

**Files:**
- Modify: `src/protoboard/gui/renderers/packages/body_label.py`
- Test: `tests/gui/test_body_label.py`

**Interfaces:**
- Consumes: `QTransform`, `QGraphicsSimpleTextItem`, `QColor`, `QBrush`, `QFont`; `Point`.
- Produces (NEW signature):
  ```python
  def make_body_label(
      text: str,
      ink: QColor,
      body_transform: QTransform,   # local→world body pose (e.g. _body_placement)
      local_center: Point,          # centre in the body-LOCAL frame (e.g. bbox centre)
      local_angle_deg: float,       # text direction in the LOCAL frame (0 = along local +x)
      *,
      point_size: float | None = None,   # fixed font size (raster units); None → scale-to-fit
      avail_len: float | None = None,     # required when point_size is None (scale-to-fit)
      avail_w: float | None = None,       # required when point_size is None
      alpha: float,
      z: float,
  ) -> QGraphicsSimpleTextItem | None
  ```
  Returns `None` if text empty, or (scale-to-fit) avail dims non-positive, or the text can't measure.

**Design (the transform composition):**
1. Build the `QGraphicsSimpleTextItem`; set the brush (ink × alpha).
2. If `point_size is not None`: set a `QFont` via `font.setPointSizeF(point_size)`, `label.setFont(font)`; the size mode is fixed (no geometric scale). Else compute `scale = min(avail_len/br.width(), avail_w/br.height())` as today.
3. Detect a mirrored body: `mirrored = (body_transform.determinant() < 0)`.
4. Compose the item transform in the LOCAL frame, then apply the body transform:
   `t = QTransform(body_transform)` (copy) → `t.translate(local_center.x, local_center.y)` → `t.rotate(local_angle_deg)` → **mirror comp:** if `mirrored`, `t.scale(-1.0, 1.0)` (counter the reflection so glyphs read left-to-right) → **upright:** compute the *world* angle the text will read at and, if it would be upside-down (outside `(-90, 90]`), `t.rotate(180)` → size (`t.scale(scale, scale)` for scale-to-fit; for fixed font no scale) → `t.translate(-br.width()/2, -br.height()/2)` to centre. Use the `br` measured *after* the font is set (fixed-font path measures the point-size bounding rect).
   - **Upright detail:** the readability angle is the accumulated rotation the glyphs end up at in world = decompose `t` (before the centring translate) via `atan2(t.m12(), t.m11())`; if not `-90 < that <= 90`, add a `t.rotate(180)` at the local stage. Keep it simple: after building `t` up to the size step, read `world_angle = atan2(t.m12(), t.m11())`; if outside range, insert a 180° rotation — easiest is to compute it before applying and branch, mirroring today's upright logic but on the *world* angle rather than the passed angle.
5. `label.setZValue(z)`; return.

> Implementer: the trickiest part is ordering mirror + upright + centre so the text ends up centred AND readable on: (a) 0°, (b) 90°/180°/270° rotations, (c) bottom-side flip, (d) flip+rotation. Write the tests in Step 1 for each of these BEFORE implementing, and let them drive the exact order. The invariant: glyphs always read left-to-right and upright-ish (never mirror-image, never upside-down), while the text's *position* is the body-local `local_center` mapped through `body_transform`.

- [ ] **Step 1: Write the failing tests**

Rewrite/extend `tests/gui/test_body_label.py`. Cover the new signature and the invariants. Use `QTransform` bases you construct directly (no renderer needed):

```python
# SPDX-License-Identifier: MIT
"""make_body_label: body-frame pinned, readable text, fixed or scaled size."""

from __future__ import annotations

import pytest
from PySide6.QtGui import QColor, QTransform

from protoboard.geometry import Point
from protoboard.gui.renderers.packages.body_label import make_body_label


def _identity() -> QTransform:
    return QTransform()


def test_fixed_point_size_item(qtbot) -> None:
    item = make_body_label(
        "2N2222", QColor("#000000"), _identity(), Point(0.0, 0.0), 0.0,
        point_size=4.0, alpha=1.0, z=5.0,
    )
    assert item is not None
    assert item.font().pointSizeF() == pytest.approx(4.0)


def test_scale_to_fit_still_works(qtbot) -> None:
    item = make_body_label(
        "223", QColor("#000000"), _identity(), Point(0.0, 0.0), 0.0,
        avail_len=4.0, avail_w=2.0, alpha=1.0, z=5.0,
    )
    assert item is not None
    br = item.mapRectToScene(item.boundingRect())
    assert br.width() <= 4.0 + 1e-6
    assert br.height() <= 2.0 + 1e-6


def test_position_follows_body_transform(qtbot) -> None:
    # A body translated to (10, 20): the label centre maps there.
    t = QTransform()
    t.translate(10.0, 20.0)
    item = make_body_label(
        "X", QColor("#000000"), t, Point(0.0, 0.0), 0.0,
        point_size=4.0, alpha=1.0, z=0.0,
    )
    assert item is not None
    c = item.mapToScene(item.boundingRect().center())
    assert c.x() == pytest.approx(10.0, abs=0.5)
    assert c.y() == pytest.approx(20.0, abs=0.5)


def test_flipped_body_text_not_mirrored(qtbot) -> None:
    # A reflected body transform (det < 0). Glyphs must NOT be mirrored:
    # the item's final transform must have POSITIVE determinant (the mirror
    # was compensated), so text reads left-to-right.
    flip = QTransform.fromScale(-1.0, 1.0)  # reflection, det = -1
    item = make_body_label(
        "AB", QColor("#000000"), flip, Point(0.0, 0.0), 0.0,
        point_size=4.0, alpha=1.0, z=0.0,
    )
    assert item is not None
    assert item.transform().determinant() > 0.0  # glyphs un-mirrored


def test_upside_down_rotation_flipped_upright(qtbot) -> None:
    # A body rotated 180°: text would read upside-down; it must be flipped
    # upright (final world reading angle within (-90, 90]).
    import math
    rot180 = QTransform()
    rot180.rotate(180.0)
    item = make_body_label(
        "AB", QColor("#000000"), rot180, Point(0.0, 0.0), 0.0,
        point_size=4.0, alpha=1.0, z=0.0,
    )
    assert item is not None
    m = item.transform()
    world_angle = math.degrees(math.atan2(m.m12(), m.m11()))
    assert -90.0 < world_angle <= 90.0


def test_empty_text_none(qtbot) -> None:
    assert make_body_label(
        "", QColor("#000000"), _identity(), Point(0.0, 0.0), 0.0,
        point_size=4.0, alpha=1.0, z=0.0,
    ) is None
```

> Implementer: if `item.transform()` doesn't expose what you need for the mirror/upright assertions (e.g. Qt folds the transform differently), assert via `item.mapToScene` on two glyph-baseline points instead — the INTENT is "not mirrored, not upside-down". Adjust the assertion to honestly test that intent; do not weaken it to pass. If an assertion is genuinely untestable at the item level, ask.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/gui/test_body_label.py -v`
Expected: FAIL — signature mismatch / new behaviour absent.

- [ ] **Step 3: Implement the new `make_body_label`**

Rewrite `body_label.py` per the Design above. Full expected shape:

```python
# SPDX-License-Identifier: MIT
"""The value text printed on a component body, pinned to the body frame.

The label is placed in the body's LOCAL frame (centre + direction) and mapped
through the body's local→world transform, so it inherits the body's rotation
and flip and stays fixed relative to the body geometry — not the pin row
(this matters for bendable pins).  The glyphs are kept readable: a mirrored
(flipped) body is compensated so text never reads backwards, and an
upside-down world orientation is flipped upright.  Size is either a fixed
point size (generic bodies) or geometric scale-to-fit (the SMD chip).
"""

from __future__ import annotations

import math

from PySide6.QtGui import QBrush, QColor, QFont, QTransform
from PySide6.QtWidgets import QGraphicsSimpleTextItem

from protoboard.geometry import Point


def make_body_label(
    text: str,
    ink: QColor,
    body_transform: QTransform,
    local_center: Point,
    local_angle_deg: float,
    *,
    point_size: float | None = None,
    avail_len: float | None = None,
    avail_w: float | None = None,
    alpha: float,
    z: float,
) -> QGraphicsSimpleTextItem | None:
    """A body-frame-pinned, readable value label, or ``None``."""
    if not text:
        return None
    label = QGraphicsSimpleTextItem(text)
    pen_ink = QColor(ink)
    pen_ink.setAlphaF(pen_ink.alphaF() * alpha)
    label.setBrush(QBrush(pen_ink))
    if point_size is not None:
        font = QFont()
        font.setPointSizeF(point_size)
        label.setFont(font)
    br = label.boundingRect()
    if br.width() < 1e-9 or br.height() < 1e-9:
        return None
    if point_size is None:
        if avail_len is None or avail_w is None or avail_len <= 0.0 or avail_w <= 0.0:
            return None
        scale = min(avail_len / br.width(), avail_w / br.height())
    else:
        scale = 1.0

    mirrored = body_transform.determinant() < 0.0

    t = QTransform(body_transform)
    t.translate(local_center.x, local_center.y)
    t.rotate(local_angle_deg)
    if mirrored:
        t.scale(-1.0, 1.0)  # counter the reflection so glyphs read forwards
    # Upright: if the accumulated world rotation would read upside-down,
    # add a half turn (position unaffected; glyphs turn right-way-up).
    world_angle = math.degrees(math.atan2(t.m12(), t.m11()))
    if not -90.0 < world_angle <= 90.0:
        t.rotate(180.0)
    if scale != 1.0:
        t.scale(scale, scale)
    t.translate(-br.width() / 2.0, -br.height() / 2.0)
    label.setTransform(t)
    label.setZValue(z)
    return label
```

> Implementer: verify the mirror/upright ordering against the Step-1 tests. If `determinant()` on a `QTransform` isn't available in this PySide6 (it is on `QTransform` — `determinant()`), compute `m11*m22 - m12*m21` by hand. The `world_angle` check after the mirror is intentional: the mirror flips handedness but the upright check on `atan2(m12,m11)` still correctly detects an upside-down reading.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/gui/test_body_label.py -v`
Expected: PASS (all invariants).

- [ ] **Step 5: Commit**

```bash
git add src/protoboard/gui/renderers/packages/body_label.py tests/gui/test_body_label.py
git commit -m "feat(renderers): make_body_label pins to body frame + fixed-size mode + mirror/upright"
```

---

## Task 3: Repoint `_maybe_value_label` (generic) + `SmdChipRenderer` (SMD) to the new signature

Both call sites move to the new `make_body_label`. Generic path uses the body placement + fixed font; SMD keeps scale-to-fit but also gains the body-frame basis (its pad-axis transform).

**Files:**
- Modify: `src/protoboard/gui/renderers/packages/base.py` (`_maybe_value_label`)
- Modify: `src/protoboard/gui/renderers/packages/prism_kinds.py` (`SmdChipRenderer._code_label`)
- Test: `tests/gui/test_generic_body_value.py` (extend), `tests/gui/test_value_code_renderers.py` (parity — keep green)

**Interfaces:**
- Consumes: the new `make_body_label` (Task 2); `self._body_placement(positions)`; `self.paint.theme.body_print_font_size` (Task 1); `bbox_or_none`.

- [ ] **Step 1: Write/extend the failing test (generic path pins to body)**

Extend `tests/gui/test_generic_body_value.py`. Add a test that a rotated generic body rotates its label with it — i.e. the label item's transform reflects the placement rotation, not a screen-aligned angle. Use the existing fixture idiom in that file to build a non-SMD renderer with `value="2N2222"` and a placement rotated 90°, then assert the produced text item's transform has a non-trivial rotation matching the body (or, more robustly, that the label centre sits at the body centre mapped through the placement). Also assert the font point size equals `theme.body_print_font_size`:

```python
def test_generic_label_uses_fixed_font(qtbot, <build-helpers>) -> None:
    renderer = <build a TO92/generic renderer, instance.value="2N2222">
    items = renderer.draw(<positions>, <fill>, <border>, 1.0, 0.0)
    labels = [it for it in items if isinstance(it, QGraphicsSimpleTextItem)]
    assert labels and labels[0].font().pointSizeF() == pytest.approx(
        DEFAULT_THEME.body_print_font_size
    )
```

> Implementer: reuse the exact renderer-construction idiom already in `test_generic_body_value.py` (Task 6 of the prior feature built it). If a rotated-placement fixture is hard to build, at minimum assert the fixed-font point size + that the label exists; note any rotation assertion you couldn't make.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/gui/test_generic_body_value.py -v`
Expected: FAIL (font size not yet fixed / signature mismatch).

- [ ] **Step 3: Rewrite `_maybe_value_label`**

In `base.py`, replace the world-point extraction with the body-frame call:

```python
    def _maybe_value_label(
        self, positions: list[Point],
    ) -> QGraphicsItem | None:
        """The component value printed on the body, or ``None``.

        Pinned to the body's local frame via ``_body_placement`` so it
        inherits the body's rotation and flip and stays fixed relative to the
        body geometry (not the pin row — matters for bendable pins).  A fixed
        font size (``theme.body_print_font_size``); the SMD chip keeps its own
        scale-to-fit code label.
        """
        if self._instance is None:
            return None
        text = body_print_text(self._component_class, self._instance.value)
        if text is None:
            return None
        box = bbox_or_none(self._pkg)
        if box is None:
            return None
        if box.width <= 0.0 or box.height <= 0.0:
            return None
        return make_body_label(
            text,
            best_ink(self.paint.fill),
            self._body_placement(positions),
            box.center,
            0.0,   # local +x = the body/pin-row axis (pin 1 → increasing pins)
            point_size=self.paint.theme.body_print_font_size,
            alpha=self.paint.alpha,
            z=self.paint.z + 3 * BodyCanvas.Z_STEP,
        )
```

> Implementer: `local_angle_deg=0.0` assumes the package-local +x runs along the pin row (pin 1 → increasing pins). Verify against `bbox_or_none`/`lead_axis_extents`: if the local frame's long/pin-row axis is +y for some packages, pass `90.0` or derive from `lead_axis_extents` (dominant axis). Confirm with one concrete package (a DIP) that the text runs along the row, not across it. If it's wrong, compute `local_angle_deg` from the dominant local pin-cloud axis rather than hardcoding 0.
> Remove the now-unused `QPointF` import and the `math`/`atan2` usage in this method if nothing else needs them (grep base.py first).

- [ ] **Step 4: Repoint `SmdChipRenderer._code_label`**

In `prism_kinds.py`, the SMD chip keeps scale-to-fit but uses the new signature. Its natural body frame is the pad axis (`axis_canvas`-style): build a `QTransform` = translate(midpoint) · rotate((p2-p1).angle_deg()), pass `local_center=Point(0,0)`, `local_angle_deg=0`, scale-to-fit dims as before:

```python
    def _code_label(self, p1, p2, body_len, body_w, cap):
        """The printed value code, scaled into the body between the caps."""
        if self._instance is None:
            return None
        text = body_print_text(self._component_class, self._instance.value)
        if text is None:
            return None
        axis = QTransform()
        axis.translate((p1.x + p2.x) / 2.0, (p1.y + p2.y) / 2.0)
        axis.rotate((p2 - p1).angle_deg())
        return make_body_label(
            text,
            best_ink(self.paint.fill),
            axis,
            Point(0.0, 0.0),
            0.0,
            avail_len=body_len * 0.9 - 2.0 * cap,
            avail_w=body_w * 0.75,
            alpha=self.paint.alpha,
            z=self.paint.z + 2 * BodyCanvas.Z_STEP,
        )
```

Add `from PySide6.QtGui import QTransform` to prism_kinds.py if not present.

> Implementer: this must keep the SMD "223" look identical (same centre = pad midpoint, same angle = p1→p2, same avail box). The upright-flip inside make_body_label reproduces the old `(p2-p1)` upright handling. Verify against the parity test.

- [ ] **Step 5: Run tests — generic + SMD parity**

Run: `uv run pytest tests/gui/test_generic_body_value.py tests/gui/test_value_code_renderers.py tests/unit/test_smd_mount_mechanic.py -v`
Expected: PASS — generic uses fixed font + pins to body; SMD "223" still renders identically.

- [ ] **Step 6: Renderer regression sweep**

Run: `uv run pytest tests/gui/ -k "render or body or value" -n auto -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/protoboard/gui/renderers/packages/base.py src/protoboard/gui/renderers/packages/prism_kinds.py tests/gui/test_generic_body_value.py
git commit -m "feat(renderers): pin body value to body frame; fixed font for generic bodies"
```

---

## Task 4: Visual verification + font-size calibration + docs

Confirm the change looks right on a real TO92, tune the font size, update docs.

**Files:**
- Possibly modify: `src/protoboard/controller/theme.py` (font-size value, if calibration needs it)
- Modify: `docs/rendering.md` (update the body-printed-value section to the body-frame + fixed-font description)

- [ ] **Step 1: Build a visual driver check**

The smoke driver fixture (`tests/fixtures/tangled`) has no solid-body part with a value. Add a tiny throwaway check: run the driver, OR write a short script that places a TO92 (2N2222) + a DIP + an SMD resistor, renders, and screenshots. Simplest: extend the driver with one step that loads a fixture containing a TO92 with `value="2N2222"` and a DIP, screenshot it. If no such fixture exists, create a minimal one under `tests/fixtures/` OR add components to a scratch project the driver can load. Read the screenshot: the value must sit ON the body, rotate/flip WITH the body, read upright, run along the pin row, and be legible.

> Implementer: keep this verification lightweight and DELETE any scratch fixture/driver-step you added once calibration is done (it's a check, not a shipped artifact) — unless it's genuinely reusable, in which case note it. Report the screenshot path so the controller can look.

- [ ] **Step 2: Calibrate `body_print_font_size`**

From the screenshot, judge the TO92 `2N2222`/`2N3904` legibility. If too small/large, adjust `body_print_font_size` in `theme.py` and re-render. Target: legible and tidy on a TO92, not overflowing the body. Update the Task 1 test's expected value if you change the default.

- [ ] **Step 3: Update docs**

In `docs/rendering.md`, update the body-printed-value section: the text is now pinned to the body's local frame via `_body_placement` (inherits rotation + flip, fixed relative to body geometry not the pin row — bendable-pin safe), reads upright and un-mirrored (mirror compensation on flipped bodies), runs pin-1→increasing along the body axis, and uses a fixed `theme.body_print_font_size` for generic bodies while the SMD chip keeps scale-to-fit.

- [ ] **Step 4: Full suite + mypy**

Run: `uv run pytest -n auto -q` (expect baseline 4 failed + 12 errors, no new).
Run: `uv run mypy --strict src/protoboard/gui/renderers/packages/body_label.py src/protoboard/gui/renderers/packages/base.py src/protoboard/gui/renderers/packages/prism_kinds.py src/protoboard/controller/theme.py`
Expected: clean (no new errors).

- [ ] **Step 5: Commit**

```bash
git add docs/rendering.md src/protoboard/controller/theme.py
git commit -m "docs(rendering): body value pinned to body frame; calibrate font size"
```

---

## Self-review notes (for the implementer)

- **The core insight:** the old code did `atan2(m12,m11)` which *discards the flip* and rebuilt the label around a world point (detaching it). The fix passes the whole `_body_placement` `QTransform` as the basis so the label inherits rotation+flip and is pinned to the body — then compensates the mirror on the glyphs only (readability) and flips upside-down text upright.
- **Bendable pins:** `_body_placement` comes from `placement_transform` (depends on the component's pose, NOT the live/bent pin positions), so a bent pin does not move the label — exactly the requested behaviour. No extra code needed; it falls out of using the placement.
- **SMD parity is the guard:** keep `test_value_code_renderers.py` green through Task 3. SMD stays on its pad-axis frame + scale-to-fit — only the call signature changes.
- **Two sizes = SMD scale-to-fit + generic fixed.** No per-body threshold; the split is by render path (SMD renderer vs generic), matching the user's "mała tylko dla SMD-chip".
- **Local angle:** verify `local_angle_deg=0.0` runs the text along the pin row for a DIP; if the local long axis is +y, derive from `lead_axis_extents`.
