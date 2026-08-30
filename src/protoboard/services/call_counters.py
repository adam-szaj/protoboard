# SPDX-License-Identifier: MIT
"""Lightweight call-count instrumentation.

A parameterised decorator collects per-label call counts into a
process-global registry, **scoped by the call stack of other
decorated functions**.  Each entered ``@counted("X")`` pushes ``X``
onto a context-local label stack; the count is bucketed under the
joined-by-dots stack path.  So if ``compute()`` calls ``refresh()``
which calls ``walk()`` and all three are decorated, the registered
counts are::

    compute                     1
    compute.refresh             1
    compute.refresh.walk        17

This makes it trivial to attribute a single inner-loop function's
call count to the higher-level user action that triggered it —
the metric you usually want when reasoning about algorithmic
complexity ("during one drag, walk runs O(N²) under refresh but
only O(N) under hit-test").

When counting is disabled (the default), the wrapper still exists
but its ``active`` flag is False — one attribute load + branch per
call.  Set ``PROTOBOARD_CALL_COUNTERS=1`` at import time or use the
:func:`counters_enabled` context manager to switch every wrapper
on at once.

Example::

    from protoboard.services.call_counters import (
        counted, counters_enabled, snapshot,
    )

    @counted("walk")
    def walk():
        return 42

    @counted("refresh")
    def refresh():
        for _ in range(3):
            walk()

    with counters_enabled():
        refresh()
        print(snapshot())
        # {"refresh": 1, "refresh.walk": 3}

The label stack uses :class:`contextvars.ContextVar` so async tasks
and threads each see their own stack.  No cross-thread leakage.
"""

from __future__ import annotations

import functools
import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TypeVar

# ---------------------------------------------------------------------------
# Public state
# ---------------------------------------------------------------------------

_ENABLED: bool = os.environ.get("PROTOBOARD_CALL_COUNTERS", "0") == "1"
_COUNTS: dict[str, int] = {}
# Registry of every @counted wrapper so ``counters_enabled`` can flip
# the per-wrapper "active" bit without rebuilding decorated functions.
_REGISTRY: list[_CountedWrapper] = []
# Per-context label stack — context-local so threads / async tasks
# don't collide.  Stored as a tuple so the snapshot is immutable per
# stack frame (we replace the var on push/pop instead of mutating).
_STACK: ContextVar[tuple[str, ...]] = ContextVar(
    "_protoboard_counter_stack", default=()
)

# Optional cap on how deep the recorded stack may grow.  Calls beyond
# this depth pass through as if the decorator weren't there — no count,
# no stack push, no effect on downstream counted paths.  ``None`` means
# unlimited.
_MAX_DEPTH: int | None = None

# Optional path filter: a list of compiled regexes derived from a
# semicolon-separated glob string like ``"a.b.c.*;g.h.c.*"``.  ``None``
# means accept everything.  Glob syntax: ``*`` matches one segment,
# ``**`` matches one-or-more segments.  Compared against the **full
# path** (current stack + this label) — if no pattern matches, the
# call bypasses just like an over-depth call.
_PATH_FILTER: list[re.Pattern[str]] | None = None
_PATH_FILTER_SOURCE: str | None = None


F = TypeVar("F", bound=Callable[..., Any])


# ---------------------------------------------------------------------------
# Glob compilation for path filters
# ---------------------------------------------------------------------------


def _compile_glob(pattern: str) -> re.Pattern[str]:
    """Compile a single dotted-path glob into an anchored regex.

    Syntax:
      * ``*``  matches exactly one segment (no dots).
      * ``**`` matches one or more segments (with dots).
      * literal label characters are taken verbatim.

    ``a.b.c.*`` matches ``a.b.c.foo`` but not ``a.b.c`` itself nor
    ``a.b.c.foo.bar``.  Use ``a.b.c.**`` for the "anything under" form
    that also accepts deeper descendants.  ``a.b.c`` alone matches only
    the exact path.
    """
    parts: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                # ``**`` — one or more segments.  We require at least
                # one character so it doesn't collapse to empty.
                parts.append(r"[^.]+(?:\.[^.]+)*")
                i += 2
            else:
                # ``*`` — exactly one segment.
                parts.append(r"[^.]+")
                i += 1
        elif c == ".":
            parts.append(r"\.")
            i += 1
        else:
            parts.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(parts) + "$")


def _compile_filter(spec: str | None) -> list[re.Pattern[str]] | None:
    """Compile a semicolon-separated glob spec into a regex list.

    Empty / None / whitespace-only spec disables filtering.
    """
    if spec is None:
        return None
    chunks = [s.strip() for s in spec.split(";") if s.strip()]
    if not chunks:
        return None
    return [_compile_glob(c) for c in chunks]


def _path_passes_filter(path: str) -> bool:
    """True iff *path* matches at least one pattern (or no filter set)."""
    if _PATH_FILTER is None:
        return True
    for pat in _PATH_FILTER:
        if pat.match(path):
            return True
    return False


class _CountedWrapper:
    """Callable wrapper that increments a counter then forwards.

    Stored separately from a plain function so we can flip ``active``
    at runtime without re-decorating the host.  No ``__slots__`` —
    :func:`functools.update_wrapper` writes ``__module__`` /
    ``__qualname__`` / ``__doc__`` etc. directly on the instance.
    """

    def __init__(self, fn: Callable[..., Any], label: str) -> None:
        # Run update_wrapper FIRST so any __dict__ copy from ``fn``
        # (which itself may be a _CountedWrapper carrying ``fn`` /
        # ``label`` / ``active`` of an inner layer) doesn't clobber
        # the attributes we set below.  The order matters when
        # chaining counted decorators — see the test fixtures that
        # build a@b@c chain.
        functools.update_wrapper(self, fn)
        self.fn = fn
        self.label = label
        self.active = _ENABLED

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if not self.active:
            return self.fn(*args, **kwargs)
        cur = _STACK.get()
        # Depth gate: if we'd exceed the recording cap, behave as a
        # bare function — no count, no push.  This keeps the cost of
        # deep call trees flat once you've narrowed the scope.
        if _MAX_DEPTH is not None and len(cur) >= _MAX_DEPTH:
            return self.fn(*args, **kwargs)
        new_stack = cur + (self.label,)
        path = ".".join(new_stack)
        # Path filter gates *only the count* — the stack still grows
        # so any nested counted call sees the correct full context.
        # Otherwise a filter like ``"a.b.c.*"`` couldn't ever match,
        # because the intermediate parents wouldn't have pushed their
        # labels and the nested calls would see a truncated prefix.
        if _path_passes_filter(path):
            _COUNTS[path] = _COUNTS.get(path, 0) + 1
        token = _STACK.set(new_stack)
        try:
            return self.fn(*args, **kwargs)
        finally:
            _STACK.reset(token)

    # Method-binding support: when @counted decorates an instance method,
    # accessing it through an instance should still bind self correctly.
    def __get__(self, obj: Any, objtype: Any = None) -> Any:
        if obj is None:
            return self
        return functools.partial(self.__call__, obj)


def counted(label: str) -> Callable[[F], F]:
    """Parameterised decorator: increment ``_COUNTS[label]`` on every call.

    When ``PROTOBOARD_CALL_COUNTERS`` is unset at import time AND the
    runtime flag remains off, the wrapper still exists but its
    ``active`` bit is False — one branch per call.  If you need true
    zero overhead for profiling-disabled production paths, gate the
    decorator behind ``if _ENABLED`` at the callsite (rare).
    """

    def decorator(fn: F) -> F:
        wrapper = _CountedWrapper(fn, label)
        _REGISTRY.append(wrapper)
        return wrapper  # type: ignore[return-value]

    return decorator


# ---------------------------------------------------------------------------
# Runtime control
# ---------------------------------------------------------------------------


def is_enabled() -> bool:
    """Process-wide enabled flag."""
    return _ENABLED


def enable() -> None:
    """Turn counting ON for every registered wrapper.

    Idempotent.  Does NOT reset the existing counts — call
    :func:`reset` first if you want a clean slate.
    """
    global _ENABLED
    _ENABLED = True
    for w in _REGISTRY:
        w.active = True


def disable() -> None:
    """Turn counting OFF for every registered wrapper."""
    global _ENABLED
    _ENABLED = False
    for w in _REGISTRY:
        w.active = False


def set_max_depth(max_depth: int | None) -> None:
    """Cap how deep the recorded label stack may grow.

    ``None`` removes the cap.  ``1`` means only top-level calls count;
    nested @counted calls below them pass through.  Useful when you
    want to attribute totals to top-level user actions without paying
    for every leaf.
    """
    global _MAX_DEPTH
    if max_depth is not None and max_depth < 0:
        raise ValueError("max_depth must be >= 0 or None")
    _MAX_DEPTH = max_depth


def get_max_depth() -> int | None:
    return _MAX_DEPTH


def set_path_filter(spec: str | None) -> None:
    """Restrict counting to paths matching the given glob spec.

    *spec* is a semicolon-separated list of globs over dotted paths.
    Glob syntax:

      * ``*``  — exactly one segment (no dots).
      * ``**`` — one or more segments.
      * Anything else is literal.

    A call whose full path doesn't match any pattern is bypassed
    entirely (no count, no stack push); children under that bypassed
    parent see only the parent's parent as their context.  ``None``
    or empty string disables filtering.

    Examples::

        set_path_filter("refresh.**")             # only refresh and below
        set_path_filter("a.b.c.*;g.h.c.*")        # children of two prefixes
        set_path_filter("*.find_owned_wires.**")  # any top-level → find_owned…
    """
    global _PATH_FILTER, _PATH_FILTER_SOURCE
    _PATH_FILTER = _compile_filter(spec)
    _PATH_FILTER_SOURCE = spec


def get_path_filter() -> str | None:
    """Return the spec string most recently passed to :func:`set_path_filter`."""
    return _PATH_FILTER_SOURCE


def reset() -> None:
    """Drop all accumulated counts.  Does NOT touch the enabled flag."""
    _COUNTS.clear()


def snapshot() -> dict[str, int]:
    """Return a copy of the current counter map.

    Keys are dotted paths reflecting the call stack at the time each
    counted function ran — e.g. ``refresh.find_owned_wires.pin_world_pos``
    if all three are decorated and called transitively.
    """
    return dict(_COUNTS)


def current_path() -> tuple[str, ...]:
    """Return the active label stack — useful for tests that want to
    assert the *position* of a counted call within a hierarchy."""
    return _STACK.get()


def rollup(counts: dict[str, int] | None = None) -> dict[str, int]:
    """Aggregate a per-path counter map up the dotted hierarchy.

    For each path ``a.b.c`` in *counts*, the returned map carries
    counts under ``a``, ``a.b`` and ``a.b.c`` — every ancestor sees
    the leaf hit.  Useful when one leaf is reachable via several
    parents and you want a totals view ("how often did walk run, in
    aggregate, regardless of which top-level action triggered it").

    Pass *counts* explicitly when you've taken a snapshot earlier;
    otherwise rolls up the live ``_COUNTS``.
    """
    src = counts if counts is not None else _COUNTS
    out: dict[str, int] = {}
    for path, n in src.items():
        parts = path.split(".")
        for i in range(1, len(parts) + 1):
            prefix = ".".join(parts[:i])
            out[prefix] = out.get(prefix, 0) + n
    return out


def delta(baseline: dict[str, int]) -> dict[str, int]:
    """Return ``snapshot() - baseline`` (per-key)."""
    cur = snapshot()
    out: dict[str, int] = {}
    for k, v in cur.items():
        b = baseline.get(k, 0)
        if v != b:
            out[k] = v - b
    return out


@contextmanager
def counters_enabled(
    *,
    max_depth: int | None = None,
    path_filter: str | None = None,
) -> Iterator[None]:
    """Context manager: turn counting on for the block, restore on exit.

    *max_depth* and *path_filter* are applied for the duration of the
    block and rolled back on exit (the previous values are restored
    — they don't have to have been None).  Pass them as keyword args
    to scope a measurement to just the contexts you care about.

    The previous enabled flag is preserved, so nesting works.  Note
    that accumulated counts persist across enter/exit boundaries —
    call :func:`reset` explicitly if you need a clean window.
    """
    prev_enabled = is_enabled()
    prev_depth = _MAX_DEPTH
    prev_filter = _PATH_FILTER_SOURCE
    enable()
    if max_depth is not None:
        set_max_depth(max_depth)
    if path_filter is not None:
        set_path_filter(path_filter)
    try:
        yield
    finally:
        # Always restore the previous state, even when the caller
        # didn't pass max_depth / path_filter — otherwise nested
        # blocks would leak each other's settings.
        set_max_depth(prev_depth)
        set_path_filter(prev_filter)
        if not prev_enabled:
            disable()


@contextmanager
def fresh_counters(
    *,
    max_depth: int | None = None,
    path_filter: str | None = None,
) -> Iterator[dict[str, int]]:
    """Context manager: reset + enable, yield a live counter dict, restore.

    Convenience for benchmarks.  Accepts the same *max_depth* /
    *path_filter* knobs as :func:`counters_enabled`.  The yielded
    dict is the same object as the module-internal ``_COUNTS``, so
    reads at any point in the block reflect counts so far.  Caller
    usually snapshots at the end.
    """
    prev_enabled = is_enabled()
    prev_depth = _MAX_DEPTH
    prev_filter = _PATH_FILTER_SOURCE
    reset()
    enable()
    if max_depth is not None:
        set_max_depth(max_depth)
    if path_filter is not None:
        set_path_filter(path_filter)
    try:
        yield _COUNTS
    finally:
        set_max_depth(prev_depth)
        set_path_filter(prev_filter)
        if not prev_enabled:
            disable()
