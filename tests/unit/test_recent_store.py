"""Tests for the generic recent-value persistence store."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from protoboard.services.persistence.recent_store import RecentStore


@dataclass(frozen=True)
class _Entry:
    key: str
    label: str


def _store(path: Path, max_entries: int = 3) -> RecentStore[_Entry]:
    return RecentStore(
        path=path,
        max_entries=max_entries,
        decode=lambda raw: (
            _Entry(raw["key"], raw["label"])
            if isinstance(raw, dict)
            and isinstance(raw.get("key"), str)
            and isinstance(raw.get("label"), str)
            else None
        ),
        encode=lambda entry: {"key": entry.key, "label": entry.label},
        key=lambda entry: entry.key,
    )


def test_load_skips_malformed_root_and_items(tmp_path: Path) -> None:
    path = tmp_path / "recent.json"
    path.write_text(
        json.dumps([{"key": "ok", "label": "OK"}, {"key": 3}, "bad"]),
        encoding="utf-8",
    )

    assert _store(path).load() == [_Entry("ok", "OK")]

    path.write_text("not json", encoding="utf-8")
    assert _store(path).load() == []


def test_add_deduplicates_moves_to_front_and_truncates(tmp_path: Path) -> None:
    store = _store(tmp_path / "recent.json", max_entries=2)
    store.add(_Entry("a", "A"))
    store.add(_Entry("b", "B"))
    store.add(_Entry("a", "A2"))
    store.add(_Entry("c", "C"))

    assert store.load() == [_Entry("c", "C"), _Entry("a", "A2")]


def test_non_serializable_value_leaves_history_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "recent.json"
    store = RecentStore(
        path=path,
        max_entries=3,
        decode=lambda raw: raw if isinstance(raw, str) else None,
        encode=lambda value: value,
        key=lambda value: value,
    )
    store.add("old")
    bad_store = RecentStore(
        path=path,
        max_entries=3,
        decode=lambda raw: raw if isinstance(raw, str) else None,
        encode=lambda _value: object(),
        key=lambda value: value,
    )

    bad_store.add("new")

    assert store.load() == ["old"]


def test_atomic_replace_failure_preserves_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "recent.json"
    store = _store(path)
    store.add(_Entry("old", "Old"))

    def fail_replace(_source: str, _target: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(
        "protoboard.services.persistence.recent_store.os.replace", fail_replace
    )
    store.add(_Entry("new", "New"))

    assert store.load() == [_Entry("old", "Old")]
