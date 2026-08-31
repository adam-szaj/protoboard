# SPDX-License-Identifier: MIT
"""Recently-built kind variants — ~/.config/protoboard/recent_kind_variants.json.

Records the *parameter set* a kind was last built with, so the kind-variant
pop-up can offer a one-confirm "Recently used" section.  An entry is a
``(kind, values, label)`` triple, deduped by ``(kind, canonical values)`` and
ordered most-recent first.

The stored thing is the parameters, NOT a catalogue address: a built variant's
family lives only in the session catalogue, so re-offering it in a fresh
session means re-running the generator with the remembered values (the
provider's ``recent_variants`` does that; a rebuild also re-parameterises by
the *current* component's pin count — remembering ``rows: 2`` offers a 2×3
header on a 6-pin part and a 2×5 on a 10-pin one).  GUI/services only — the
domain never sees this.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from protoboard.paths import RECENT_KIND_VARIANTS_FILE as _RECENT_FILE
from protoboard.services.persistence.recent_store import RecentStore

_MAX = 10


@dataclass(frozen=True)
class RecentKindVariant:
    """A previously-built parameter set for a package kind."""

    kind: str
    values: dict[str, object] = field(default_factory=dict)
    label: str = ""


def _canonical(values: Mapping[str, object]) -> str | None:
    """Stable JSON key for dedup; ``None`` when *values* is not JSON-safe."""
    try:
        return json.dumps(dict(values), sort_keys=True)
    except (TypeError, ValueError):
        return None


def _decode(raw: object) -> RecentKindVariant | None:
    if (
        isinstance(raw, dict)
        and isinstance(raw.get("kind"), str)
        and isinstance(raw.get("values"), dict)
        and isinstance(raw.get("label"), str)
    ):
        return RecentKindVariant(
            kind=raw["kind"], values=dict(raw["values"]), label=raw["label"]
        )
    return None


def _store() -> RecentStore[RecentKindVariant]:
    return RecentStore(
        path=_RECENT_FILE,
        max_entries=_MAX,
        decode=_decode,
        encode=lambda entry: {
            "kind": entry.kind,
            "values": entry.values,
            "label": entry.label,
        },
        key=lambda entry: (entry.kind, _canonical(entry.values)),
    )


def load() -> list[RecentKindVariant]:
    """Return the recently-built kind variants, most recent first."""
    return _store().load()


def add(kind: str, values: Mapping[str, object], label: str) -> None:
    """Prepend a built parameter set to the recent list and persist it.

    Deduped by ``(kind, canonical values)`` and truncated to the most recent
    :data:`_MAX` entries.  Silent on I/O failure or non-JSON-safe values (a
    missing history is not fatal).
    """
    key = _canonical(values)
    if key is None:
        return
    _store().add(RecentKindVariant(kind=kind, values=dict(values), label=label))
