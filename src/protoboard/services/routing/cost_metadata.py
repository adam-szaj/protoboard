# SPDX-License-Identifier: MIT
"""Merge built-in routing cost metadata with discovered router plugins."""

from protoboard.domain.routing.cost_model import (
    COST_KEY_DOCS,
    USER_TUNABLE_COST_KEYS,
)
from protoboard.services.routing.protocol import registry


def merged_cost_key_docs() -> dict[str, str]:
    merged: dict[str, str] = {}
    for name in registry.list_available():
        try:
            extra: object = getattr(registry.get(name), "cost_key_docs", lambda: {})()
            if isinstance(extra, dict):
                merged.update(
                    {
                        str(key): str(value)
                        for key, value in extra.items()
                        if isinstance(key, str)
                    }
                )
        except Exception:  # noqa: BLE001
            continue
    merged.update(COST_KEY_DOCS)
    return merged


def merged_user_tunable_cost_keys() -> frozenset[str]:
    keys = set(USER_TUNABLE_COST_KEYS)
    for name in registry.list_available():
        try:
            extra: object = getattr(
                registry.get(name), "user_tunable_cost_keys", lambda: frozenset()
            )()
            if isinstance(extra, (set, frozenset)):
                keys.update(str(key) for key in extra if isinstance(key, str))
        except Exception:  # noqa: BLE001
            continue
    return frozenset(keys)
