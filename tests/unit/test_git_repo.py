# SPDX-License-Identifier: MIT
"""Tests for the thin ``git`` wrapper used by the save flow."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from protoboard.services.persistence import git_repo

_GIT_AVAILABLE = shutil.which("git") is not None
pytestmark = pytest.mark.skipif(not _GIT_AVAILABLE, reason="git binary not available")


@pytest.fixture(autouse=True)
def _isolate_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force git to use a deterministic identity without touching ~/.gitconfig."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.invalid")
    # Pin the initial branch to keep tests stable across git versions.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    monkeypatch.setenv("GIT_INIT_DEFAULT_BRANCH", "main")


def _log_count(path: Path) -> int:
    return int(
        subprocess.check_output(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=str(path),
            text=True,
        ).strip()
    )


def _staged_paths(path: Path) -> list[str]:
    out = subprocess.check_output(
        ["git", "status", "--porcelain"],
        cwd=str(path),
        text=True,
    )
    return [line[3:] for line in out.splitlines()]


class TestInit:
    def test_init_creates_repo(self, tmp_path: Path) -> None:
        assert not git_repo.is_repo(tmp_path)
        git_repo.init(tmp_path)
        assert git_repo.is_repo(tmp_path)
        assert (tmp_path / ".git").is_dir()

    def test_init_idempotent(self, tmp_path: Path) -> None:
        git_repo.init(tmp_path)
        # Should not raise nor reset the repo.
        git_repo.init(tmp_path)
        assert git_repo.is_repo(tmp_path)


class TestAddAndCommit:
    def test_first_save_creates_repo_and_commits(self, tmp_path: Path) -> None:
        (tmp_path / "board.yaml").write_text("dummy: 1\n")
        result = git_repo.add_and_commit(tmp_path, "initial")
        assert result.created_repo is True
        assert result.commit_sha is not None
        assert _log_count(tmp_path) == 1
        assert _staged_paths(tmp_path) == []

    def test_subsequent_save_appends_commit(self, tmp_path: Path) -> None:
        (tmp_path / "board.yaml").write_text("dummy: 1\n")
        first = git_repo.add_and_commit(tmp_path, "initial")
        (tmp_path / "board.yaml").write_text("dummy: 2\n")
        second = git_repo.add_and_commit(tmp_path, "second")
        assert second.created_repo is False
        assert second.commit_sha != first.commit_sha
        assert _log_count(tmp_path) == 2

    def test_noop_save_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "board.yaml").write_text("dummy: 1\n")
        git_repo.add_and_commit(tmp_path, "initial")
        # Nothing changed.
        result = git_repo.add_and_commit(tmp_path, "noop")
        assert result.commit_sha is None
        assert _log_count(tmp_path) == 1

    def test_specific_paths_only(self, tmp_path: Path) -> None:
        (tmp_path / "a.yaml").write_text("a\n")
        (tmp_path / "b.yaml").write_text("b\n")
        # Commit only a.yaml; b.yaml stays unstaged.
        result = git_repo.add_and_commit(tmp_path, "only-a", paths=["a.yaml"])
        assert result.commit_sha is not None
        assert _staged_paths(tmp_path) == ["b.yaml"]

    def test_empty_paths_is_noop(self, tmp_path: Path) -> None:
        (tmp_path / "a.yaml").write_text("a\n")
        result = git_repo.add_and_commit(tmp_path, "nothing", paths=[])
        assert result.commit_sha is None
        assert result.created_repo is True

    def test_missing_git_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Strip git from PATH for this test only.
        monkeypatch.setenv("PATH", "")
        with pytest.raises(git_repo.GitError):
            git_repo.add_and_commit(tmp_path, "fails")

    def test_init_if_needed_false(self, tmp_path: Path) -> None:
        with pytest.raises(git_repo.GitError):
            git_repo.add_and_commit(tmp_path, "fails", init_if_needed=False)
