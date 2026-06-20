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

``corner_radius_mm`` follows a single convention: ``0`` = sharp (square) corners,
``> 0`` = rounded with that radius in millimetres (like ``dims``). There is no
separate edge enum in the override.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})\Z")


@dataclass(frozen=True, slots=True)
class Appearance:
    """A visual-only override of a body's fill colour and corner radius.

    ``color`` is a ``#rgb`` / ``#rrggbb`` hex string, or ``None`` to inherit.
    ``corner_radius_mm`` is ``0`` for sharp, ``> 0`` for rounded (mm), or ``None``
    to inherit the package's edge behaviour.
    """

    color: str | None = None
    corner_radius_mm: float | None = None

    def __post_init__(self) -> None:
        if self.corner_radius_mm is not None and self.corner_radius_mm < 0:
            raise ValueError(
                f"corner_radius must be >= 0, got {self.corner_radius_mm!r}"
            )

    @property
    def is_empty(self) -> bool:
        """True when neither field is set (nothing to override)."""
        return self.color is None and self.corner_radius_mm is None

    @staticmethod
    def resolve(*layers: Appearance | None) -> Appearance:
        """Per-field fall-through across *layers*, highest precedence first.

        Call as ``Appearance.resolve(instance.appearance, defn.appearance)``: each
        field independently takes the first non-``None`` value across the layers.
        Package/theme defaults are applied later (in the renderer) for fields that
        are still ``None`` here, so they are intentionally not part of this merge.
        """
        color: str | None = None
        radius: float | None = None
        for layer in layers:
            if layer is None:
                continue
            if color is None:
                color = layer.color
            if radius is None:
                radius = layer.corner_radius_mm
        return Appearance(color=color, corner_radius_mm=radius)


def _validate_hex(value: str, ctx: str) -> str:
    if not _HEX_RE.match(value):
        raise ValueError(
            f"{ctx}: color must be a #rgb or #rrggbb hex string, got {value!r}"
        )
    return value


def parse_appearance(raw: object, *, ctx: str) -> Appearance | None:
    """Parse + validate an ``appearance:`` YAML block.

    Returns ``None`` for an absent or empty block. Raises ``ValueError`` on a
    malformed colour, a non-numeric / negative radius, or a non-mapping value;
    callers translate that into their serialization error type.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"{ctx}: appearance must be a mapping")
    color_raw = raw.get("color")
    color = None if color_raw is None else _validate_hex(str(color_raw), ctx)
    radius_raw = raw.get("corner_radius")
    if radius_raw is None:
        radius = None
    else:
        try:
            radius = float(radius_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{ctx}: corner_radius must be a number") from exc
    appearance = Appearance(color=color, corner_radius_mm=radius)
    return None if appearance.is_empty else appearance
