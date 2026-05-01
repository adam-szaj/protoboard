# SPDX-License-Identifier: MIT
"""Visual theme — single source of truth for all colours, sizes and opacities.

Import ``DEFAULT_THEME`` in renderers and export services.  Override individual
fields by loading an optional ``~/.config/protoboard-designer/theme.yaml``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Theme:
    # ── Board ────────────────────────────────────────────────────────────────
    board_bg: str = "#444444"
    board_frame_color: str = "#FFFFFF"        # root box outline
    board_frame_width: float = 0.06
    board_sub_frame_color: str = "#FFFFFF3C"  # non-root box outline (AARRGGBB → alpha 60)
    board_sub_frame_width: float = 0.04

    pad_color: str = "#333333"
    pad_radius: float = 0.35
    pad_outline_color: str = "#000000"
    pad_outline_width: float = 0.04
    hole_color: str = "#1A1A1A"

    strip_color: str = "#C8A030"
    strip_radius: float = 0.30
    strip_track_color: str = "#A07020"
    strip_track_outline: str = "#7A5010"
    strip_track_outline_width: float = 0.03
    isolated_color: str = "#666666"
    isolated_x_color: str = "#FF4040"
    isolated_x_width: float = 0.08

    # ── Wires ────────────────────────────────────────────────────────────────
    net_colors: list[str] = field(default_factory=lambda: [
        "#E03030",  # red
        "#3060E0",  # blue
        "#30A030",  # green
        "#E0A020",  # amber
        "#A030A0",  # purple
        "#20B0B0",  # cyan
        "#E06020",  # orange
        "#B08020",  # olive
    ])
    net_color_unassigned: str = "#A0A0A0"

    bare_width: float = 0.16
    bare_outline_color: str = "#888888"
    bare_outline_width: float = 0.20

    ins_width: float = 0.18
    ins_outline_color: str = "#000000"
    ins_outline_width: float = 0.28

    bottom_opacity: float = 0.45

    ratsnest_color: str = "#FF5050"
    ratsnest_alpha: int = 160           # 0-255
    ratsnest_width: float = 0.08

    # ── Components ───────────────────────────────────────────────────────────
    comp_fill_r: int = 255
    comp_fill_g: int = 255
    comp_fill_b: int = 200
    comp_fill_a: int = 180
    comp_border: str = "#444444"

    pin_color_fallback: str = "#CC2222"  # when no schematic loaded
    pin_radius: float = 0.18

    # Pin colours follow the same net_colors palette (may differ from wire palette)
    pin_net_colors: list[str] = field(default_factory=lambda: [
        "#E63B3B",  # red
        "#3B8AE6",  # blue
        "#2ECC71",  # green
        "#E6A020",  # amber
        "#A020E6",  # purple
        "#20C8E6",  # cyan
        "#E6207A",  # pink
        "#7AE620",  # lime
    ])

    refdes_color: str = "#AAAAAA"
    refdes_font_size: float = 0.55

    # Component lead lines (wire between pins and body)
    lead_color: str = "#888888"
    lead_width: float = 0.06

    # Body shape geometry
    body_corner_radius: float = 0.15
    axial_body_length_ratio: float = 0.50   # body length / pin-to-pin distance
    axial_body_width: float = 0.40          # body "diameter"
    radial_body_radius: float = 0.35        # circular body radius
    to92_body_width: float = 2.4
    to92_body_height: float = 1.4
    dip_notch_radius: float = 0.25
    sil_slot_size: float = 0.40             # per-pin slot square size

    ghost_fill_r: int = 255
    ghost_fill_g: int = 255
    ghost_fill_b: int = 200
    ghost_fill_a: int = 80
    ghost_border_r: int = 255
    ghost_border_g: int = 255
    ghost_border_b: int = 100
    ghost_border_a: int = 160
    ghost_pin_r: int = 200
    ghost_pin_g: int = 80
    ghost_pin_b: int = 80
    ghost_pin_a: int = 160
    ghost_label_r: int = 220
    ghost_label_g: int = 220
    ghost_label_b: int = 100
    ghost_label_a: int = 180

    group_pen_r: int = 100
    group_pen_g: int = 200
    group_pen_b: int = 255
    group_pen_a: int = 160
    group_pen_width: float = 0.08
    group_fill_r: int = 100
    group_fill_g: int = 200
    group_fill_b: int = 255
    group_fill_a: int = 12
    group_name_r: int = 100
    group_name_g: int = 200
    group_name_b: int = 255
    group_name_a: int = 200
    group_margin: float = 0.6

    # ── Routing overlay ──────────────────────────────────────────────────────
    hint_colors: list[tuple[int, int, int, int]] = field(default_factory=lambda: [
        (0, 200, 80, 200),    # green — cheapest path
        (220, 180, 0, 180),   # yellow
        (220, 100, 0, 160),   # orange — most expensive
    ])
    hint_width: float = 0.10

    # ── Export SVG ───────────────────────────────────────────────────────────
    svg_scale: float = 10.0
    svg_bg: str = "#2B6B3A"
    svg_text_color: str = "#111111"


DEFAULT_THEME = Theme()


def load_theme(path: Path | None = None) -> Theme:
    """Load a Theme from a YAML file, falling back to DEFAULT_THEME on any error.

    If *path* is None, looks for ``~/.config/protoboard-designer/theme.yaml``.
    Unknown keys are silently ignored so partial overrides work.
    """
    if path is None:
        path = Path.home() / ".config" / "protoboard-designer" / "theme.yaml"
    if not path.exists():
        return DEFAULT_THEME
    try:
        from ruamel.yaml import YAML  # type: ignore[import-untyped]
        data = YAML().load(path)
        if not isinstance(data, dict):
            return DEFAULT_THEME
        theme = Theme()
        for key, value in data.items():
            if hasattr(theme, key):
                setattr(theme, key, value)
        return theme
    except Exception:  # noqa: BLE001
        return DEFAULT_THEME
