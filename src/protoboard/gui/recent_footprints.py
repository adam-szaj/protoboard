# SPDX-License-Identifier: MIT
"""Recently-used footprints — persisted to ~/.config/protoboard/recent_footprints.json.

Records which footprint a component was last placed with, so the library picker
can surface a "recently used" group. An entry is a ``(definition_ref, address,
label)`` triple, deduped by ``(definition_ref, address)`` and ordered most-recent
first. GUI/services only — the domain never sees this.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from protoboard.paths import CONFIG_DIR as _CONFIG_DIR
from protoboard.paths import RECENT_FOOTPRINTS_FILE as _RECENT_FILE

_MAX = 20


@dataclass(frozen=True, slots=True)
class RecentFootprint:
    """A previously-chosen footprint for a component."""

    definition_ref: str
    address: str
    label: str


def load() -> list[RecentFootprint]:
    """Return the recently-used footprints, most recent first."""
    try:
        data = json.loads(_RECENT_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    out: list[RecentFootprint] = []
    for item in data if isinstance(data, list) else []:
        if (
            isinstance(item, dict)
            and isinstance(item.get("definition_ref"), str)
            and isinstance(item.get("address"), str)
            and isinstance(item.get("label"), str)
        ):
            out.append(
                RecentFootprint(
                    definition_ref=item["definition_ref"],
                    address=item["address"],
                    label=item["label"],
                )
            )
    return out


def add(definition_ref: str, address: str, label: str) -> None:
    """Prepend a footprint choice to the recent list and persist it.

    Deduped by ``(definition_ref, address)`` and truncated to the most recent
    :data:`_MAX` entries. Silent on I/O failure (a missing list is not fatal).
    """
    entry = RecentFootprint(definition_ref, address, label)
    existing = [
        e for e in load() if (e.definition_ref, e.address) != (definition_ref, address)
    ]
    updated = ([entry] + existing)[:_MAX]
    try:
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _RECENT_FILE.write_text(
            json.dumps(
                [
                    {
                        "definition_ref": e.definition_ref,
                        "address": e.address,
                        "label": e.label,
                    }
                    for e in updated
                ],
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass
