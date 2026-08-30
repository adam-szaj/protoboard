# SPDX-License-Identifier: MIT
"""Visual theme — single source of truth for all colours, sizes and opacities.

Import ``DEFAULT_THEME`` in renderers and export services.  Override individual
fields by loading an optional ``~/.config/protoboard-designer/theme.yaml``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from protoboard.domain.components.package import PackageFamily


@dataclass
class Theme:
    # ── Board ────────────────────────────────────────────────────────────────
    board_bg: str = "#FF5E5C64"
    board_frame_color: str = "#FFFFFF"  # root box outline
    board_frame_width: float = 0.06
    board_sub_frame_color: str = (
        "#FFFFFF80"  # non-root box outline (AARRGGBB → alpha 60)
    )
    board_sub_frame_width: float = 0.04

    pad_color: str = "#70000000"
    pad_radius: float = 0.35
    pad_outline_color: str = "#222222"
    pad_outline_width: float = 0.04
    hole_color: str = "#FFFFFFFF"

    strip_color: str = "#FFC8A030"
    strip_radius: float = 0.30
    strip_track_color: str = "#A07020"
    strip_track_outline: str = "#7A5010"
    strip_track_outline_width: float = 0.03
    isolated_color: str = "#666666"
    isolated_x_color: str = "#FFFF4040"
    isolated_x_width: float = 0.08

    # ── Wires ────────────────────────────────────────────────────────────────
    net_colors: list[str] = field(
        default_factory=lambda: [
            "#FFE03030",  # red
            "#FF3060E0",  # blue
            "#FF30A030",  # green
            "#FFE0A020",  # amber
            "#FFA030A0",  # purple
            "#FF20B0B0",  # cyan
            "#FFE06020",  # orange
            "#FFB08020",  # olive
        ]
    )
    net_color_unassigned: str = "#A0A0A0"

    bare_width: float = 0.2
    bare_outline_color: str = "#FFFFFFFF"
    bare_outline_width: float = 0.3

    ins_width: float = 0.17
    ins_outline_color: str = "#FF241F31"
    ins_outline_width: float = 0.3

    ratsnest_color: str = "#FFF6D32D"  # #AARRGGBB
    ratsnest_width: float = 0.08
    # Ratsnest line colour used when a net's pins are all connected
    # internally but the net is split across multiple physically
    # disconnected electrical islands (e.g. two routed sub-nets that
    # were never joined).  Different colour highlights the layout
    # error vs. plain "yet to be routed".
    ratsnest_islands_color: str = "#FFA347BA"  # purple — "attention"

    # ── Components ───────────────────────────────────────────────────────────
    comp_fill_color: str = "#FF8FF0A4"  # #AARRGGBB
    comp_border: str = "#FF222222"

    pin_color_fallback: str = "#CC2222"  # when no schematic loaded
    pin_radius: float = 0.18

    # Pin colours follow the same net_colors palette (may differ from wire palette)
    pin_net_colors: list[str] = field(
        default_factory=lambda: [
            "#E63B3B",  # red
            "#3B8AE6",  # blue
            "#2ECC71",  # green
            "#E6A020",  # amber
            "#A020E6",  # purple
            "#20C8E6",  # cyan
            "#E6207A",  # pink
            "#7AE620",  # lime
        ]
    )

    refdes_color: str = "#FF1C71D8"
    refdes_font_size: float = 0.55
    overlay_font_size: float = 15.0
    #: Maximum rendered height (raster units, in-scene) for the
    #: value/part-number text printed on a generic component body
    #: (TO92/TO220/DIP/…). The text scales to fit the body, clamped to this
    #: cap so short strings on big bodies don't balloon. The SMD chip keeps
    #: its own uncapped scale-to-fit code label.
    body_print_max_height: float = 0.5
    label_bg_color: str = "#B0FFFFFF"

    # Alpha of the second (mid) blit of the board pixmap in the layered
    # background sandwich.  Slot 0 paints the board full opacity on the
    # bottom; slot 5 paints it again at this alpha so front-side items
    # visually sit on the pads while back-side items remain readable.
    # 0 disables the mid slot entirely.
    board_mid_alpha: float = 0.30

    # Half-transparent black wash drawn between back-side items and the
    # mid board blit.  Visually separates back-side wires/components from
    # front-side ones independently of camera-side dimming.  Active when
    # the board layer is FULL or GHOST; suppressed when HIDDEN.
    back_filter_color: str = "#000000"
    back_filter_alpha: float = 0.30

    # Axis labels (column/row indices rendered around the board).  They sit
    # over the surrounding canvas, not over the board itself, so they reuse
    # ``board_bg`` for their background pill rather than ``label_bg_color``.
    axis_label_color: str = "#FFFFFFFF"

    # Component lead lines (wire between pins and body)
    lead_color: str = "#FF000000"
    lead_width: float = 0.06

    # Raster calibration — used to convert SVG mm dimensions to raster units
    raster_pitch_mm: float = 2.54

    # Body shape geometry
    body_corner_radius: float = 0.15
    radial_body_radius: float = 0.35  # circular body radius
    to220_tab_thickness_mm: float = 1.3  # metal mounting tab extension
    dip_notch_radius: float = 0.25
    sil_slot_size: float = 0.40  # per-pin slot square size

    # Package-family catalogue loaded at startup; keyed by family id.
    package_families: dict[str, PackageFamily] = field(default_factory=dict)

    ghost_fill_r: int = 255
    ghost_fill_g: int = 255
    ghost_fill_b: int = 200
    ghost_fill_a: int = 225
    ghost_border_r: int = 255
    ghost_border_g: int = 255
    ghost_border_b: int = 100
    ghost_border_a: int = 225
    ghost_pin_r: int = 200
    ghost_pin_g: int = 80
    ghost_pin_b: int = 80
    ghost_pin_a: int = 225
    ghost_label_r: int = 220
    ghost_label_g: int = 220
    ghost_label_b: int = 100
    ghost_label_a: int = 128

    group_pen_color: str = "#FFC0BFBC"  # #AARRGGBB
    group_pen_width: float = 0.0
    group_fill_color: str = "#1C0020BB"  # #AARRGGBB
    group_name_color: str = "#FF241F31"  # #AARRGGBB
    group_margin: float = 0.6
    group_active_color: str = "#C8DC3232"  # active group outline (red)
    group_active_fill_color: str = "#35FF4242"  # active group fill
    group_dim_pen_alpha: int = 60  # alpha for dimmed child group outline
    group_dim_fill_alpha: int = 20  # alpha for dimmed child group fill
    group_dim_name_alpha: int = 60  # alpha for dimmed child group label

    select_color: str = "#FF00FFFF"  # single selection outline
    select_width: float = 0.07
    multi_select_color: str = "#FF00FFFF"  # multi selection outline
    multi_select_width: float = 0.05
    # Thin dashed bounding box drawn around a selected component (in addition to
    # the bright body outline) — dimmed cyan, much thinner than the outline.
    selection_bbox_color: str = "#8000FFFF"
    selection_bbox_width: float = 0.03

    # ── Tool feedback ────────────────────────────────────────────────────────
    placement_reach_color: str = "#7800c850"  # reachable pad highlight
    placement_reach_line_color: str = "#b400c850"  # pin1→cursor line
    routing_preview_color: str = "#b4ff6400"  # route wire preview (basic [W])
    routing_net_aware_color: str = (
        "#b400c853"  # route wire preview (net-aware [Shift+W])
    )

    # ── Component pins & markers ─────────────────────────────────────────────
    pin_width_complete: float = 0.08  # pin with assigned net (solid)
    pin_width_incomplete: float = 0.08  # pin without net (dashed)
    pin_label_fallback_color: str = "#ff777777"
    pin1_marker_color: str = "#ff777777"
    component_origin_cross_color: str = "#dcc86400"
    component_origin_cross_width: float = 0.07

    # ── Exported pins ────────────────────────────────────────────────────────
    exported_pin_color: str = "#c800c850"
    exported_pin_pen_color: str = "#FFED333B"
    exported_pin_pen_width: float = 0.13

    # ── Component body details ────────────────────────────────────────────────
    body_led_stripe_color: str = "#FF241F31"
    body_diode_cathode_color: str = "#ff202028"
    body_diode_anode_color: str = "#ff808088"
    body_slot_opacity: float = 0.63
    body_tab_opacity: float = 0.6
    body_slot_pen_width: float = 0.04
    body_tab_pen_width: float = 0.04

    # ── Material palette ─────────────────────────────────────────────────────
    # Keyed by ``Material`` enum (as a string).  Each entry is a
    # ``(fill_hex, border_hex, fill_alpha)`` triplet.  The renderer uses
    # the spec's ``material`` to pick the default body fill / border;
    # specific class-flavour decorations (cathode stripe, polarity
    # marker) overlay on top of this base.
    material_palette: dict[str, tuple[str, str, float]] = field(
        default_factory=lambda: {
            # Glass body (DO-35 signal diodes, glass fuses): translucent
            # off-white shell with a dark rim.
            "glass": ("#f4ece0", "#403428", 0.55),
            # Plastic body (DO-41/DO-15 rectifier diodes, plastic
            # resistors): matte mid-grey-blue.
            "plastic": ("#404858", "#1c2030", 0.90),
            # Ceramic body (axial resistors, ceramic disc caps,
            # ceramic bricks): warm beige.
            "ceramic": ("#d6c08c", "#5c4a30", 0.92),
            # Metal body (axial / radial electrolytic cans, HC-49):
            # cool silver-grey with darker rim.
            "metal": ("#b8b8c0", "#48484c", 0.92),
            # LED lens (5 mm / 3 mm clear or coloured): bright base
            # that the class-flavour layer tints by colour.
            "led_lens": ("#e8e8e8", "#3c3c3c", 0.80),
        }
    )

    # ── Routing overlay ──────────────────────────────────────────────────────
    hint_colors: list[tuple[int, int, int, int]] = field(
        default_factory=lambda: [
            (0, 200, 80, 200),  # green — cheapest path
            (220, 180, 0, 180),  # yellow
            (220, 100, 0, 160),  # orange — most expensive
        ]
    )
    hint_width: float = 0.10

    # ── Layer automatic alphas ───────────────────────────────────────────────
    back_alpha: float = 0.9  # non-camera-side layers
    # Back-side WIRE fragments only — deliberately far below back_alpha so a
    # fragment flipped to the other side reads as clearly recessed (the
    # generic 0.9 was indistinguishable from the front at a glance).
    wire_back_alpha: float = 0.45
    board_ghost_alpha: float = 0.51  # board while placing/routing
    ghost_comp_alpha: float = 0.89  # components while placing/routing

    # ── Net / group highlight dim ────────────────────────────────────────────
    highlight_dim_components: float = 0.4  # alpha for dimmed components
    highlight_dim_wires: float = 0.4  # alpha for dimmed wires
    # ── Net highlight: bold (tier-1) ─────────────────────────────────────────
    # Tier-1 net highlight (routing in-progress, Net Inspector selection):
    # - pins of the highlighted net grow to nearly the size of the pad and
    #   become fully opaque so wires can't bleed through
    # - wires get a thicker core (in the net colour) and thicker outline
    #   (in its theme colour — outline distinguishes bare/insulated, not nets)
    # Colours stay unchanged; only widths and dot radius scale.
    bold_pin_width_factor: float = 2.5  # multiplier on pin_width_complete/_incomplete
    bold_pin_dot_radius_factor: float = (
        0.85  # multiplier on pad_radius for the bold dot
    )
    bold_wire_core_width_factor: float = 1.6  # multiplier on ins_width / bare_width
    bold_wire_outline_width_factor: float = (
        1.8  # multiplier on ins_outline_width / bare_outline_width
    )
    # ── Routing highlight glow (tier-2: wire selection only) ─────────────────
    route_glow_wire_width: float = (
        0.55  # extra glow halo width around highlighted wires
    )
    route_glow_wire_alpha: float = 0.35  # alpha of the glow halo
    route_glow_pin_radius: float = 0.28  # radius of glow ring around highlighted pins
    route_glow_pin_alpha: float = 0.55  # alpha of the glow ring

    # ── Pin-pads layer ───────────────────────────────────────────────────────
    # Schematic-aware overlay above the board layer.  Every pad that
    # carries an electrical net is repainted in the net colour here,
    # with a modifier shape (square / diamond / star) cut out of the
    # centre to mark pin 1 / exported / both.  See
    # ``domain/layout/pin_pad_classification.py``.
    #
    # Filled pin-pad geometry inherits ``pad_radius`` so the colour
    # disc lines up with the bare-pad ring underneath.  Modifier shape
    # is drawn smaller so the outer ring stays visible.
    pin_pad_modifier_radius_factor: float = 0.7  # × pad_radius
    pin_pad_contact_only_width: float = 0.04  # outline ring stroke for contact-only
    # FREE pin (terminal present, no net): neutral high-contrast outline so
    # an unconnected pin is distinct from an empty hole without implying a
    # net colour.  Deliberately not red (accessibility) and heavier stroke
    # than contact-only so it reads as "a pin is here, unassigned".
    pin_free_color: str = "#9098A0"
    pin_free_width: float = 0.11
    # Routing-target neon — overrides net colour when session.routing_net_id
    # matches the pad's / wire's net.  Bright yellow chosen to read against
    # both dark and light board backgrounds; alpha=FF for full neon.
    routing_target_color: str = "#FFFFEE00"
    # ── Via markers (wire side transitions) ──────────────────────────────────
    # Drawn where a wire fragment crosses to the other board side.  Light
    # disc + dark ring: a shape/contrast cue (not colour-only) that stays
    # readable regardless of the net colour underneath.
    via_marker_color: str = "#E8E8E8"
    via_marker_ring_color: str = "#202020"
    via_marker_radius: float = 0.28

    # ── Export SVG ───────────────────────────────────────────────────────────
    svg_scale: float = 10.0
    svg_bg: str = "#2B6B3A"
    svg_text_color: str = "#111111"


DEFAULT_THEME = Theme()


def save_theme(theme: Theme, path: Path | None = None) -> None:
    """Persist *theme* to YAML, writing only fields that differ from defaults."""
    from protoboard.paths import THEME_FILE

    if path is None:
        path = THEME_FILE
    try:
        from ruamel.yaml import YAML

        defaults = Theme()
        data: dict[str, object] = {}
        for f in theme.__dataclass_fields__:
            if f == "package_families":
                # Loaded from assets at startup; not user-configurable.
                continue
            val = getattr(theme, f)
            if val != getattr(defaults, f):
                data[f] = val

        path.parent.mkdir(parents=True, exist_ok=True)
        yaml = YAML()
        yaml.default_flow_style = False
        with path.open("w", encoding="utf-8") as fh:
            yaml.dump(data, fh)
    except Exception:  # noqa: BLE001
        pass


def load_theme(path: Path | None = None) -> Theme:
    """Load a Theme from a YAML file, falling back to DEFAULT_THEME on any error.

    If *path* is None, looks for ``~/.config/protoboard/theme.yaml``.
    Unknown keys are silently ignored so partial overrides work.
    """
    from protoboard.paths import THEME_FILE

    if path is None:
        path = THEME_FILE
    if not path.exists():
        return DEFAULT_THEME
    try:
        from ruamel.yaml import YAML

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
