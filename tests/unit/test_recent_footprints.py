# SPDX-License-Identifier: MIT
"""Tests for the recently-used footprints store."""

from __future__ import annotations

from pathlib import Path

import pytest

from protoboard.gui import recent_footprints


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Redirect the persisted file into a tmp dir so tests don't touch ~/.config.
    monkeypatch.setattr(
        recent_footprints, "_RECENT_FILE", tmp_path / "recent_footprints.json"
    )


def test_add_then_load_roundtrip() -> None:
    recent_footprints.add("R", "r-tht/DIN/0207", "R_DIN-0207")
    loaded = recent_footprints.load()
    assert len(loaded) == 1
    assert loaded[0].definition_ref == "R"
    assert loaded[0].address == "r-tht/DIN/0207"
    assert loaded[0].label == "R_DIN-0207"


def test_most_recent_first_and_dedupe() -> None:
    recent_footprints.add("R", "a/typ", "R_a")
    recent_footprints.add("R", "b/typ", "R_b")
    recent_footprints.add("R", "a/typ", "R_a")  # re-pick a → moves to front
    addrs = [e.address for e in recent_footprints.load()]
    assert addrs == ["a/typ", "b/typ"]  # deduped, a first


def test_dedupe_is_per_definition_and_address() -> None:
    recent_footprints.add("R", "a/typ", "R_a")
    recent_footprints.add("C", "a/typ", "C_a")  # same address, different comp
    loaded = recent_footprints.load()
    assert len(loaded) == 2


def test_truncates_at_max() -> None:
    for i in range(recent_footprints._MAX + 5):
        recent_footprints.add("R", f"fam{i}/typ", f"R_{i}")
    assert len(recent_footprints.load()) == recent_footprints._MAX


def test_missing_file_is_empty() -> None:
    assert recent_footprints.load() == []
