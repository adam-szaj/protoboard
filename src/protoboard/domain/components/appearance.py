# SPDX-License-Identifier: MIT
"""Visual appearance override for a component body.

A small value object layered on top of a body's package/theme defaults. It is
**visual only** — it carries the body fill colour and the corner radius, and
affects nothing in geometry, collision masks, DRC, routing or pin positions.

It can be set on a :class:`ComponentDefinition` (a per-type override, e.g. all
``R_0805`` black) and on a :class:`ComponentInstance` (a per-placed-part override).
At render time the effective value falls through **instance → definition →
package → theme default** per field (:meth:`Appearance.resolve` covers the first
two; the package/theme defaults apply later in the renderer where a field is still
``None``).

It is authored as a single CSS-like ``style:`` string, e.g.::

    style: "color: #1a1a1a; corner-radius: 0.4"
    style: "color: #1a1a1a; corner-radius: 2 2 0 0"

Grammar: ``;``-separated declarations, each ``property: value`` (the first ``:``
splits property from value). Property names use the CSS hyphen. Supported
properties are ``color`` (``#rgb`` / ``#rgba`` / ``#rrggbb`` / ``#rrggbbaa`` —
CSS convention, the alpha channel LAST; the renderer reorders it for Qt) and
``corner-radius`` (1–4 bare millimetre numbers, CSS ``border-radius`` order).
Unknown properties are an error; a duplicate property keeps the last
declaration (CSS behaviour). A declared alpha multiplies with the layer alpha
at render time (ghosting/dimming), it does not replace it.

``corner-radius`` follows a single convention: ``0`` = sharp (square) corners,
``> 0`` = rounded with that radius in millimetres (like ``dims``). It is stored
as a tuple of 1–4 values and rendered per corner: the renderer reads
:attr:`Appearance.corner_radius_mm_corners`, the CSS ``border-radius``
expansion to (top-left, top-right, bottom-right, bottom-left).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEX_RE = re.compile(
    r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\Z")


@dataclass(frozen=True, slots=True)
class Appearance:
    """A visual-only override of a body's fill colour and corner radius.

    ``color`` is a ``#rgb`` / ``#rgba`` / ``#rrggbb`` / ``#rrggbbaa`` hex
    string (CSS channel order — alpha last), or ``None`` to inherit.
    ``corner_radius_mm`` is a tuple of 1–4 radii in millimetres (CSS
    ``border-radius`` order), each ``0`` for sharp / ``> 0`` for rounded, or
    ``None`` to inherit the package's edge behaviour. The render path reads
    the per-corner expansion :attr:`corner_radius_mm_corners`.
    """

    color: str | None = None
    corner_radius_mm: tuple[float, ...] | None = None

    def __post_init__(self) -> None:
        radii = self.corner_radius_mm
        if radii is not None:
            if not 1 <= len(radii) <= 4:
                raise ValueError(
                    f"corner-radius takes 1-4 values, got {len(radii)}"
                )
            for value in radii:
                if value < 0:
                    raise ValueError(
                        f"corner-radius must be >= 0, got {value!r}"
                    )

    @property
    def is_empty(self) -> bool:
        """True when neither field is set (nothing to override)."""
        return self.color is None and self.corner_radius_mm is None

    @property
    def corner_radius_mm_scalar(self) -> float | None:
        """First/only corner radius (mm) — a uniform-radius convenience view.

        The render path uses :attr:`corner_radius_mm_corners`; this scalar
        remains for callers that only need a single representative radius.
        """
        return None if self.corner_radius_mm is None else self.corner_radius_mm[0]

    @property
    def corner_radius_mm_corners(
        self,
    ) -> tuple[float, float, float, float] | None:
        """Per-corner radii (mm) as (top-left, top-right, bottom-right,
        bottom-left) — the CSS ``border-radius`` expansion of the stored
        1–4 values, or ``None`` when no radius override is set.
        """
        radii = self.corner_radius_mm
        if radii is None:
            return None
        if len(radii) == 1:
            return (radii[0], radii[0], radii[0], radii[0])
        if len(radii) == 2:
            return (radii[0], radii[1], radii[0], radii[1])
        if len(radii) == 3:
            return (radii[0], radii[1], radii[2], radii[1])
        return (radii[0], radii[1], radii[2], radii[3])

    @staticmethod
    def resolve(*layers: Appearance | None) -> Appearance:
        """Per-field fall-through across *layers*, highest precedence first.

        Call as ``Appearance.resolve(instance.appearance, defn.appearance)``: each
        field independently takes the first non-``None`` value across the layers.
        Package/theme defaults are applied later (in the renderer) for fields that
        are still ``None`` here, so they are intentionally not part of this merge.
        """
        color: str | None = None
        radius: tuple[float, ...] | None = None
        for layer in layers:
            if layer is None:
                continue
            if color is None:
                color = layer.color
            if radius is None:
                radius = layer.corner_radius_mm
        return Appearance(color=color, corner_radius_mm=radius)

    def to_style_string(self) -> str:
        """Serialise back to a canonical ``style:`` string (``""`` when empty).

        ``parse_style(a.to_style_string(), ctx=...) == a`` for any valid ``a``.
        """
        parts: list[str] = []
        if self.color is not None:
            parts.append(f"color: {self.color}")
        if self.corner_radius_mm is not None:
            radii = " ".join(_fmt_mm(v) for v in self.corner_radius_mm)
            parts.append(f"corner-radius: {radii}")
        return "; ".join(parts)


def _fmt_mm(value: float) -> str:
    """Format a millimetre value compactly (``0.4`` not ``0.40000001``)."""
    return f"{value:g}"


def _validate_hex(value: str, ctx: str) -> str:
    if not _HEX_RE.match(value):
        raise ValueError(
            f"{ctx}: color must be a #rgb / #rgba / #rrggbb / #rrggbbaa hex "
            f"string (CSS order, alpha last), got {value!r}"
        )
    return value


def _parse_corner_radius(value: str, ctx: str) -> tuple[float, ...]:
    tokens = value.split()
    if not 1 <= len(tokens) <= 4:
        raise ValueError(f"{ctx}: corner-radius takes 1-4 numbers, got {value!r}")
    try:
        return tuple(float(tok) for tok in tokens)
    except ValueError as exc:
        raise ValueError(
            f"{ctx}: corner-radius must be numbers, got {value!r}"
        ) from exc


def parse_style(raw: object, *, ctx: str) -> Appearance | None:
    """Parse + validate a CSS-like ``style:`` string into an :class:`Appearance`.

    Returns ``None`` for an absent or empty/whitespace-only value. Raises
    ``ValueError`` on a non-string value, a malformed declaration, an unknown
    property, a bad colour or a bad corner-radius; callers translate that into
    their serialization error type. A duplicate property keeps the last
    declaration (CSS behaviour).
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"{ctx}: style must be a string")
    if not raw.strip():
        return None

    color: str | None = None
    radius: tuple[float, ...] | None = None
    for decl in raw.split(";"):
        decl = decl.strip()
        if not decl:
            continue
        if ":" not in decl:
            raise ValueError(
                f"{ctx}: malformed declaration {decl!r} (expected 'property: value')"
            )
        name, _, value = decl.partition(":")
        name = name.strip()
        value = value.strip()
        if name == "color":
            color = _validate_hex(value, ctx)
        elif name == "corner-radius":
            radius = _parse_corner_radius(value, ctx)
        else:
            raise ValueError(f"{ctx}: unknown style property {name!r}")

    appearance = Appearance(color=color, corner_radius_mm=radius)
    return None if appearance.is_empty else appearance
