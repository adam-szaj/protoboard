"""Generic, fault-tolerant persistence for recently used values."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class RecentStore(Generic[_T]):
    """Store a bounded, deduplicated list of JSON-backed values."""

    path: Path
    max_entries: int
    decode: Callable[[object], _T | None]
    encode: Callable[[_T], object]
    key: Callable[[_T], Hashable]

    def load(self) -> list[_T]:
        """Load valid entries, preserving file order and removing duplicates."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(raw, list):
            return []

        result: list[_T] = []
        seen: set[Hashable] = set()
        for item in raw:
            try:
                value = self.decode(item)
                if value is None:
                    continue
                value_key = self.key(value)
            except Exception:  # noqa: BLE001
                continue
            if value_key in seen:
                continue
            seen.add(value_key)
            result.append(value)
            if len(result) >= self.max_entries:
                break
        return result

    def add(self, value: _T) -> None:
        """Prepend ``value`` and atomically persist the bounded history."""
        try:
            value_key = self.key(value)
            existing = [item for item in self.load() if self.key(item) != value_key]
            updated = [value, *existing][: self.max_entries]
            encoded = [self.encode(item) for item in updated]
            payload = json.dumps(encoded, indent=2)
        except Exception:  # noqa: BLE001
            return

        temp_name: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temp_name = temporary.name
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temp_name, str(self.path))
            temp_name = None
        except Exception:  # noqa: BLE001
            pass
        finally:
            if temp_name is not None:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
