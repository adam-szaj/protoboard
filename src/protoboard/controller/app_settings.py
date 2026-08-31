# SPDX-License-Identifier: MIT
"""Application-level settings persisted across sessions.

Stored in ~/.config/protoboard/app_settings.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Allowed values for drag soft-conflict resolution preferences.  Any
# value outside this set in the YAML falls back to the dataclass
# default — protects against typos cementing into a broken state.
# Single source of truth lives in protoboard.controller.editing.drag_preferences;
# this re-export keeps the legacy private name working.
from protoboard.controller.editing.drag_preferences import (  # noqa: E402
    DRAG_CONFLICT_VALUES as _DRAG_CONFLICT_VALUES,
)
from protoboard.paths import APP_SETTINGS_FILE as _CONFIG_PATH


@dataclass
class AppSettings:
    """Persistent application preferences."""

    default_projects_dir: Path | None = None
    component_library_dirs: list[Path] = field(default_factory=list)
    module_dirs: list[Path] = field(default_factory=list)
    auto_accept_placement: bool = True
    # Selection axes (default: select everything, highlight by logical net).
    selection_filter_tokens: list[str] = field(
        default_factory=lambda: ["components", "wires", "groups"]
    )
    selection_relation: str = "same_logical_net"
    # ---- Render throttles ------------------------------------------------
    # Interval (ms) between status-bar cursor-label updates.  ``cursor_moved``
    # fires on every mouseMoveEvent — without a throttle, each update forces
    # an X11 round-trip per event, which over SSH backlogs hundreds of stale
    # positions after the cursor stops.  Default 33 ms ≈ 30 fps; raise to
    # 100–150 ms over slow remote sessions.
    cursor_label_throttle_ms: int = 33
    # Interval (ms) between rubber-band selection-rect repaints.  Same SSH
    # cost as the cursor label.  33 ms ≈ 30 fps.
    rubber_band_throttle_ms: int = 33
    # Debounce (ms) before the BoardView re-runs its post-zoom refresh
    # (today that is the O(scene items) label-visibility sweep, but the
    # knob is named for the trigger, not the work).  Below this
    # interval, bursts of wheel events collapse to a single refresh.
    # 80 ms lets a 4–8-tick wheel burst settle before the refresh fires.
    zoom_refresh_ms: int = 80
    # Render anti-aliasing for board / scene paint.  On by default; turn
    # off to claw back paint time on very large boards or weak GPUs at
    # the cost of jagged pad / wire edges.
    antialiasing: bool = True
    # Draw a thin dashed bounding box around each selected component (in
    # addition to the bright body outline).  On by default; turn off to keep
    # only the body outline.
    show_selection_bbox: bool = True
    # User-defined cost-model presets.  Built-in presets ("default",
    # "clean_routing") are always available without being stored here.
    # Each entry maps preset_name → {cost_key: weight}.  Editing via
    # PreferencesDialog.  See protoboard.domain.routing.cost_presets.
    cost_presets: dict[str, dict[str, float]] = field(default_factory=dict)
    # Drag-time soft conflict resolution.  Each field is one of
    # "auto-remove" / "ask-remove" / "auto-reroute" / "ask-reroute" /
    # "block" — see docs/archive/bug-drag-skewed-wire-shorts.md.  Defaults
    # picked to be conservative (preserve existing wires unless the
    # user opts in).
    drag_free_wire_conflict: str = "auto-remove"
    drag_stationary_group_wire_conflict: str = "ask-remove"
    drag_cross_group_disconnect: str = "ask-reroute"
    drag_default_lock_internal_wires: bool = False
    # Connections (ratsnest) layer render mode.  "full" draws every pair
    # in a net (K_n graph) so the user sees every missing link; "closest"
    # draws just the nearest-other-pin per pin which keeps the overlay
    # readable on dense designs.  Multi-island nets always use the
    # ``ratsnest_islands_color`` from Theme regardless of mode.
    ratsnest_mode: str = "full"  # "full" | "closest"
    # Defaults for a manually-drawn wire (RouteTool).  Bare on the bottom
    # side is the natural perfboard default — solder-side links with no
    # insulation.  Persisted so the user's last choice survives a restart;
    # the [I]/[T]/[B] keys and toolbar toggles still flip them per-session.
    wire_default_insulated: bool = False
    wire_default_side: str = "bottom"  # "top" | "bottom"


def load_app_settings() -> AppSettings:
    """Load AppSettings from YAML; return defaults on missing file or any error."""
    if not _CONFIG_PATH.exists():
        return AppSettings()
    try:
        from ruamel.yaml import YAML

        data = YAML().load(_CONFIG_PATH)
        if not isinstance(data, dict):
            return AppSettings()
        s = AppSettings()
        if raw := data.get("default_projects_dir"):
            s.default_projects_dir = Path(raw).expanduser()
        if dirs := data.get("component_library_dirs"):
            s.component_library_dirs = [Path(d).expanduser() for d in dirs if d]
        if mdirs := data.get("module_dirs"):
            s.module_dirs = [Path(d).expanduser() for d in mdirs if d]
        if (aap := data.get("auto_accept_placement")) is not None:
            s.auto_accept_placement = bool(aap)
        sel = data.get("selection")
        if isinstance(sel, dict):
            tokens = sel.get("filter")
            if isinstance(tokens, list):
                s.selection_filter_tokens = [str(t) for t in tokens]
            rel = sel.get("relation")
            if isinstance(rel, str):
                s.selection_relation = rel
        perf = data.get("performance")
        if isinstance(perf, dict):
            for attr in (
                "cursor_label_throttle_ms",
                "rubber_band_throttle_ms",
                "zoom_refresh_ms",
            ):
                raw = perf.get(attr)
                if isinstance(raw, (int, float)):
                    # Clamp to a sane range so a typo doesn't lock the UI.
                    setattr(s, attr, max(0, min(2000, int(raw))))
        render = data.get("render")
        if isinstance(render, dict):
            aa = render.get("antialiasing")
            if isinstance(aa, bool):
                s.antialiasing = aa
            sbb = render.get("show_selection_bbox")
            if isinstance(sbb, bool):
                s.show_selection_bbox = sbb
            rmode = render.get("ratsnest_mode")
            if isinstance(rmode, str) and rmode in ("full", "closest"):
                s.ratsnest_mode = rmode
        wire = data.get("wire")
        if isinstance(wire, dict):
            ins = wire.get("default_insulated")
            if isinstance(ins, bool):
                s.wire_default_insulated = ins
            wside = wire.get("default_side")
            if isinstance(wside, str) and wside in ("top", "bottom"):
                s.wire_default_side = wside
        drag = data.get("drag")
        if isinstance(drag, dict):
            for yaml_key, attr_name in (
                ("free_wire_conflict", "drag_free_wire_conflict"),
                (
                    "stationary_group_wire_conflict",
                    "drag_stationary_group_wire_conflict",
                ),
                ("cross_group_disconnect", "drag_cross_group_disconnect"),
            ):
                v = drag.get(yaml_key)
                if isinstance(v, str) and v in _DRAG_CONFLICT_VALUES:
                    setattr(s, attr_name, v)
            lock = drag.get("default_lock_internal_wires")
            if isinstance(lock, bool):
                s.drag_default_lock_internal_wires = lock
        cp = data.get("cost_presets")
        if isinstance(cp, dict):
            from protoboard.domain.routing.cost_presets import builtin_preset_names

            builtins = builtin_preset_names()
            user_presets: dict[str, dict[str, float]] = {}
            for name, weights in cp.items():
                if not isinstance(name, str) or name in builtins:
                    # Reserved name or non-string key — skip, never let the
                    # YAML shadow a built-in preset.
                    continue
                if not isinstance(weights, dict):
                    continue
                cleaned: dict[str, float] = {}
                for k, v in weights.items():
                    if isinstance(k, str) and isinstance(v, (int, float)):
                        cleaned[k] = float(v)
                user_presets[name] = cleaned
            s.cost_presets = user_presets
        return s
    except Exception:  # noqa: BLE001
        return AppSettings()


def save_app_settings(settings: AppSettings) -> None:
    """Persist AppSettings to YAML."""
    try:
        from ruamel.yaml import YAML

        data: dict[str, object] = {}
        if settings.default_projects_dir is not None:
            data["default_projects_dir"] = str(settings.default_projects_dir)
        data["component_library_dirs"] = [
            str(p) for p in settings.component_library_dirs
        ]
        data["module_dirs"] = [str(p) for p in settings.module_dirs]
        data["auto_accept_placement"] = settings.auto_accept_placement
        data["selection"] = {
            "filter": list(settings.selection_filter_tokens),
            "relation": settings.selection_relation,
        }
        data["performance"] = {
            "cursor_label_throttle_ms": settings.cursor_label_throttle_ms,
            "rubber_band_throttle_ms": settings.rubber_band_throttle_ms,
            "zoom_refresh_ms": settings.zoom_refresh_ms,
        }
        data["render"] = {
            "antialiasing": settings.antialiasing,
            "show_selection_bbox": settings.show_selection_bbox,
            "ratsnest_mode": settings.ratsnest_mode,
        }
        data["wire"] = {
            "default_insulated": settings.wire_default_insulated,
            "default_side": settings.wire_default_side,
        }
        data["drag"] = {
            "free_wire_conflict": settings.drag_free_wire_conflict,
            "stationary_group_wire_conflict": settings.drag_stationary_group_wire_conflict,  # noqa: E501
            "cross_group_disconnect": settings.drag_cross_group_disconnect,
            "default_lock_internal_wires": settings.drag_default_lock_internal_wires,
        }
        if settings.cost_presets:
            data["cost_presets"] = {
                name: {k: float(v) for k, v in weights.items()}
                for name, weights in settings.cost_presets.items()
            }

        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        yaml = YAML()
        yaml.default_flow_style = False
        with _CONFIG_PATH.open("w", encoding="utf-8") as fh:
            yaml.dump(data, fh)
    except Exception:  # noqa: BLE001
        pass
