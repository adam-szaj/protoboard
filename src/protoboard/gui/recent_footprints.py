# SPDX-License-Identifier: MIT
"""Recently-used footprints — persisted to ~/.config/protoboard/recent_footprints.json.

Records which footprint a component was last placed with, so the library picker
can surface a "recently used" group. An entry is a ``(definition_ref, address,
label)`` triple, deduped by ``(definition_ref, address)`` and ordered most-recent
first. GUI/services only — the domain never sees this.
"""

from __future__ import annotations

from dataclasses import dataclass

from protoboard.paths import RECENT_FOOTPRINTS_FILE as _RECENT_FILE
from protoboard.services.persistence.recent_store import RecentStore

_MAX = 20


@dataclass(frozen=True, slots=True)
class RecentFootprint:
    """A previously-chosen footprint for a component."""

    definition_ref: str
    address: str
    label: str


def _decode(raw: object) -> RecentFootprint | None:
    if (
        isinstance(raw, dict)
        and isinstance(raw.get("definition_ref"), str)
        and isinstance(raw.get("address"), str)
        and isinstance(raw.get("label"), str)
    ):
        return RecentFootprint(
            definition_ref=raw["definition_ref"],
            address=raw["address"],
            label=raw["label"],
        )
    return None


def _store() -> RecentStore[RecentFootprint]:
    return RecentStore(
        path=_RECENT_FILE,
        max_entries=_MAX,
        decode=_decode,
        encode=lambda entry: {
            "definition_ref": entry.definition_ref,
            "address": entry.address,
            "label": entry.label,
        },
        key=lambda entry: (entry.definition_ref, entry.address),
    )


def load() -> list[RecentFootprint]:
    """Return the recently-used footprints, most recent first."""
    return _store().load()


def add(definition_ref: str, address: str, label: str) -> None:
    """Prepend a footprint choice to the recent list and persist it.

    Deduped by ``(definition_ref, address)`` and truncated to the most recent
    :data:`_MAX` entries. Silent on I/O failure (a missing list is not fatal).
    """
    _store().add(RecentFootprint(definition_ref, address, label))
