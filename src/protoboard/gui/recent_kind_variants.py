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

from protoboard.paths import CONFIG_DIR as _CONFIG_DIR
from protoboard.paths import RECENT_KIND_VARIANTS_FILE as _RECENT_FILE

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


def load() -> list[RecentKindVariant]:
    """Return the recently-built kind variants, most recent first."""
    try:
        data = json.loads(_RECENT_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    out: list[RecentKindVariant] = []
    for item in data if isinstance(data, list) else []:
        if (
            isinstance(item, dict)
            and isinstance(item.get("kind"), str)
            and isinstance(item.get("values"), dict)
            and isinstance(item.get("label"), str)
        ):
            out.append(
                RecentKindVariant(
                    kind=item["kind"],
                    values=dict(item["values"]),
                    label=item["label"],
                )
            )
    return out


def add(kind: str, values: Mapping[str, object], label: str) -> None:
    """Prepend a built parameter set to the recent list and persist it.

    Deduped by ``(kind, canonical values)`` and truncated to the most recent
    :data:`_MAX` entries.  Silent on I/O failure or non-JSON-safe values (a
    missing history is not fatal).
    """
    key = _canonical(values)
    if key is None:
        return
    existing = [e for e in load() if (e.kind, _canonical(e.values)) != (kind, key)]
    entry = RecentKindVariant(kind=kind, values=dict(values), label=label)
    updated = ([entry] + existing)[:_MAX]
    try:
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _RECENT_FILE.write_text(
            json.dumps(
                [
                    {"kind": e.kind, "values": e.values, "label": e.label}
                    for e in updated
                ],
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass
