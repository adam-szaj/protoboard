# SPDX-License-Identifier: MIT
"""Minimal git wrapper used by the project-save flow.

The editor stores each project as a folder; on save we want every revision
captured as a git commit so the user can later roll the project back to any
previous save.  We do *not* shell out to a porcelain library — a thin
``subprocess`` wrapper is enough for the three operations we need:

- :func:`is_repo`   — is ``.git`` already present?
- :func:`init`      — initialise an empty repo in the project directory
- :func:`add_and_commit` — stage everything and create one commit

All functions are best-effort: a missing ``git`` binary or a hook failure
raises :class:`GitError` so the caller can surface the message to the user
without breaking the save itself.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import structlog

from protoboard.errors import ProtoboardError

_log = structlog.get_logger(__name__)


class GitError(ProtoboardError):
    """Raised when a git invocation fails or the git binary is missing."""


@dataclass(frozen=True)
class CommitResult:
    """Outcome of :func:`add_and_commit`.

    ``commit_sha`` is None when there was nothing to commit (the working
    tree matched HEAD).  Callers should treat that case as a no-op rather
    than an error.
    """

    commit_sha: str | None
    created_repo: bool


def _run_git(
    args: list[str],
    cwd: Path,
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitError("git binary not found on PATH") from exc
    if check and result.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed (rc={result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def is_repo(path: Path) -> bool:
    """Return True if *path* is a git working tree (``.git`` exists)."""
    return (Path(path) / ".git").exists()


def init(path: Path) -> None:
    """Initialise a git repository in *path* if one isn't already there."""
    path = Path(path)
    if is_repo(path):
        return
    path.mkdir(parents=True, exist_ok=True)
    _run_git(["init", "--quiet"], cwd=path)
    _log.info("git.init", path=str(path))


def add_and_commit(
    path: Path,
    message: str,
    *,
    paths: list[str] | None = None,
    init_if_needed: bool = True,
) -> CommitResult:
    """Stage and commit changes under *path*.

    When *paths* is None (the default) every change is staged with
    ``git add -A``.  When *paths* is provided only those entries are
    staged — useful for splitting a save into multiple logical commits
    (e.g. session log first, then the project files).

    Returns :class:`CommitResult` with the new short SHA, or
    ``commit_sha=None`` when there is nothing to commit.  *init_if_needed*
    creates an empty repo on demand.
    """
    path = Path(path)
    created = False
    if not is_repo(path):
        if not init_if_needed:
            raise GitError(f"{path} is not a git repository")
        init(path)
        created = True

    if paths is None:
        _run_git(["add", "-A"], cwd=path)
    else:
        if not paths:
            return CommitResult(commit_sha=None, created_repo=created)
        _run_git(["add", "--", *paths], cwd=path)

    # ``git diff --cached --quiet`` returns 1 iff there are staged changes.
    diff = _run_git(["diff", "--cached", "--quiet"], cwd=path, check=False)
    if diff.returncode == 0:
        _log.info("git.commit_skipped", path=str(path), reason="no_staged_changes")
        return CommitResult(commit_sha=None, created_repo=created)

    if not message:
        message = "Save"
    _run_git(["commit", "-m", message, "--quiet"], cwd=path)
    sha = _run_git(["rev-parse", "--short", "HEAD"], cwd=path).stdout.strip()
    _log.info("git.commit", path=str(path), sha=sha)
    return CommitResult(commit_sha=sha, created_repo=created)


__all__ = ["CommitResult", "GitError", "add_and_commit", "init", "is_repo"]
