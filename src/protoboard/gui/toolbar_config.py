# SPDX-License-Identifier: MIT
"""Toolbar layout persistence — full-state YAML in ``~/.config/protoboard/toolbars.yaml``.

The shipped default lives at ``BUILTIN_DEFAULT_TOOLBARS_FILE`` and is
copied to the user's config dir on first start.  From then on the user
file is the single source of truth — the Customize Toolbars dialog
edits it in place.

Format (see ``default_toolbars.yaml`` in the package root for the live
example)::

    <toolbar_group>:
      - <action_id>
      - <action_id>

Unknown action ids are dropped at load time with a warning; the loader
is robust against malformed YAML — any error falls back to defaults
and logs the cause.

The seven supported toolbar groups are defined as
``ToolbarGroup`` in :mod:`protoboard.gui.actions`.  Adding a new
toolbar means:

1. Add the literal to ``ToolbarGroup`` in ``actions.py``.
2. Add an entry (possibly empty) to ``default_toolbars.yaml``.
3. The builder picks it up automatically.
"""  # noqa: E501

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast, get_args

import structlog

from protoboard.gui.actions import ToolbarGroup
from protoboard.paths import (
    BUILTIN_DEFAULT_TOOLBARS_FILE,
    CONFIG_DIR,
    TOOLBARS_FILE,
)

_log = structlog.get_logger(__name__)


#: Authoritative list of known toolbar groups, derived from the
#: ``ToolbarGroup`` Literal so adding one in ``actions.py`` automatically
#: surfaces here too.
KNOWN_GROUPS: frozenset[str] = frozenset(get_args(ToolbarGroup))


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------


def load_toolbar_config(
    *,
    user_path: Path | None = None,
    default_path: Path | None = None,
) -> dict[ToolbarGroup, list[str]]:
    """Return the toolbar layout: ``{group: [action_id, ...]}``.

    Resolution order:

    1. If ``user_path`` exists, load it.  Malformed → fall back to defaults.
    2. Otherwise load ``default_path`` (shipped with the package).
    3. If neither is loadable, return an empty layout so the builder
       creates no toolbars — the application still launches.

    The function never raises; failures are logged and substituted.
    """
    if user_path is None:
        user_path = TOOLBARS_FILE
    if default_path is None:
        default_path = BUILTIN_DEFAULT_TOOLBARS_FILE
    raw: dict[str, list[str]] | None = None
    source: str = "<empty>"

    if user_path.exists():
        loaded = _read_yaml(user_path)
        if loaded is not None:
            raw = loaded
            source = str(user_path)
    if raw is None and default_path.exists():
        loaded = _read_yaml(default_path)
        if loaded is not None:
            raw = loaded
            source = str(default_path)
    if raw is None:
        _log.warning(
            "toolbar_config.no_source", user=str(user_path), default=str(default_path)
        )
        return {}

    cleaned: dict[ToolbarGroup, list[str]] = {}
    for group_str, items in raw.items():
        if group_str not in KNOWN_GROUPS:
            _log.warning(
                "toolbar_config.unknown_group",
                group=group_str,
                source=source,
            )
            continue
        if not isinstance(items, list):
            _log.warning(
                "toolbar_config.non_list_value",
                group=group_str,
                source=source,
            )
            continue
        action_ids: list[str] = []
        for entry in items:
            if isinstance(entry, str):
                action_ids.append(entry)
            else:
                _log.warning(
                    "toolbar_config.non_string_action",
                    group=group_str,
                    entry=repr(entry),
                    source=source,
                )
        cleaned[cast(ToolbarGroup, group_str)] = action_ids
    return cleaned


def _read_yaml(path: Path) -> dict[str, list[str]] | None:
    """Defensive YAML reader.  Returns ``None`` on any failure."""
    try:
        from ruamel.yaml import YAML  # type: ignore[import-untyped,unused-ignore]

        yaml = YAML(typ="safe")
        data = yaml.load(path)
        if not isinstance(data, dict):
            _log.warning("toolbar_config.not_a_mapping", path=str(path))
            return None
        # ruamel may return CommentedMap — coerce to plain dict for type stability.
        return dict(data)
    except Exception as exc:  # malformed YAML / IO error
        _log.warning("toolbar_config.read_failed", path=str(path), error=repr(exc))
        return None


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------


def save_toolbar_config(
    config: Mapping[ToolbarGroup, list[str]],
    *,
    user_path: Path | None = None,
) -> None:
    """Persist ``config`` to the user's toolbars file.

    Writes every known group, including empty ones, so the file
    documents the full toolbar surface.  Comments from the shipped
    defaults are NOT preserved — the dialog writes its own header.
    """
    if user_path is None:
        user_path = TOOLBARS_FILE
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    from ruamel.yaml import YAML  # type: ignore[import-untyped,unused-ignore]

    yaml = YAML()
    yaml.default_flow_style = False

    payload: dict[str, list[str]] = {}
    for group in get_args(ToolbarGroup):
        payload[group] = list(config.get(group, []))

    with user_path.open("w", encoding="utf-8") as fh:
        fh.write("# Protoboard Designer — toolbar layout\n")
        fh.write("# Edited by View → Customize Toolbars…  (or by hand).\n")
        fh.write("# See packaged default_toolbars.yaml for the format spec.\n\n")
        yaml.dump(payload, fh)


def ensure_user_toolbars_file_exists(
    *,
    user_path: Path | None = None,
    default_path: Path | None = None,
) -> None:
    """Copy the shipped default toolbar layout into the user's config
    dir on first start.  Subsequent starts find the file in place and
    leave it untouched.

    Safe to call repeatedly — no-op when the user file already exists.
    """
    if user_path is None:
        user_path = TOOLBARS_FILE
    if default_path is None:
        default_path = BUILTIN_DEFAULT_TOOLBARS_FILE
    if user_path.exists():
        return
    if not default_path.exists():
        _log.warning(
            "toolbar_config.bootstrap_no_default",
            default=str(default_path),
        )
        return
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    user_path.write_text(default_path.read_text(encoding="utf-8"), encoding="utf-8")
    _log.info("toolbar_config.bootstrapped", path=str(user_path))


# ---------------------------------------------------------------------------
# Defaults snapshot — used by the Customize Toolbars dialog "Reset" button
# ---------------------------------------------------------------------------


def load_default_toolbar_config() -> dict[ToolbarGroup, list[str]]:
    """Reload the shipped default layout, bypassing the user file.
    Used by the dialog's reset-to-defaults action."""
    return load_toolbar_config(
        user_path=Path("/dev/null"),  # force fallback to defaults
        default_path=BUILTIN_DEFAULT_TOOLBARS_FILE,
    )


__all__ = [
    "KNOWN_GROUPS",
    "load_toolbar_config",
    "save_toolbar_config",
    "ensure_user_toolbars_file_exists",
    "load_default_toolbar_config",
]
