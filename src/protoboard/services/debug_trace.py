# SPDX-License-Identifier: MIT
"""Lightweight call tracing — log arguments and return values.

A parameterised ``@traced("label")`` decorator logs each call's arguments and
its return value (or raised exception). Disabled by default; flip on with the
``PROTOBOARD_TRACE=1`` environment variable at import time, or the
:func:`tracing_enabled` context manager at runtime.

Companion to :mod:`protoboard.services.call_counters` (same env-gated,
zero-overhead-when-off, method-binding style) but a different purpose: counters
answer "how many times", tracing answers "with what and returning what".

When disabled the wrapper still exists but its ``active`` flag is False — one
attribute load + branch per call. ::

    from protoboard.services.debug_trace import traced, tracing_enabled

    @traced("solve")
    def solve(x, y=2):
        return x + y

    with tracing_enabled():
        solve(3)
        # → traced solve args=(3,) kwargs={} → 5

Output goes through structlog at DEBUG (event ``"trace.call"`` /
``"trace.return"`` / ``"trace.raise"``). Long values are truncated to keep the
log readable; override the cap with ``PROTOBOARD_TRACE_MAXLEN``.
"""

from __future__ import annotations

import functools
import os
import reprlib
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, Callable, TypeVar

import structlog

_log = structlog.get_logger("trace")

_ENABLED: bool = os.environ.get("PROTOBOARD_TRACE", "0") == "1"

# Registry of every wrapper so ``tracing_enabled`` can flip the active bit
# without re-decorating hosts.
_REGISTRY: list["_TracedWrapper"] = []

_MAXLEN: int = int(os.environ.get("PROTOBOARD_TRACE_MAXLEN", "200"))

F = TypeVar("F", bound=Callable[..., Any])


def _abbr(value: object) -> str:
    """Compact, length-capped repr safe for logging arbitrary objects."""
    r = reprlib.Repr()
    r.maxstring = _MAXLEN
    r.maxother = _MAXLEN
    r.maxlist = 10
    r.maxdict = 10
    r.maxtuple = 10
    try:
        return r.repr(value)
    except Exception:  # never let logging break the traced call
        return f"<unreprable {type(value).__name__}>"


class _TracedWrapper:
    """Callable wrapper that logs args/return then forwards.

    Stored separately from a plain function so ``active`` can be flipped at
    runtime. No ``__slots__`` — ``functools.update_wrapper`` writes attributes
    onto the instance.
    """

    def __init__(self, fn: Callable[..., Any], label: str) -> None:
        functools.update_wrapper(self, fn)
        self.fn = fn
        self.label = label
        self.active = _ENABLED

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if not self.active:
            return self.fn(*args, **kwargs)
        _log.debug(
            f"trace.call[{self.label}]",
            args=[_abbr(a) for a in args],
            kwargs={k: _abbr(v) for k, v in kwargs.items()},
        )
        try:
            result = self.fn(*args, **kwargs)
        except BaseException as exc:
            _log.debug(f"trace.raise[{self.label}]", exc=f"{type(exc).__name__}: {exc}")
            raise
        _log.debug(f"trace.return[{self.label}]", result=_abbr(result))
        return result

    def __get__(self, obj: Any, objtype: Any = None) -> Any:
        if obj is None:
            return self
        return functools.partial(self.__call__, obj)


def traced(label: str | None = None) -> Callable[[F], F]:
    """Parameterised decorator: log args + return value on every call.

    Active only while tracing is enabled (env var at import, or
    :func:`tracing_enabled`); otherwise one branch per call.
    """

    def decorator(fn: F) -> F:
        _label = label if label else fn.__qualname__
        wrapper = _TracedWrapper(fn, _label)
        _REGISTRY.append(wrapper)
        return wrapper  # type: ignore[return-value]

    return decorator


def is_enabled() -> bool:
    """Process-wide enabled flag."""
    return _ENABLED


def set_enabled(enabled: bool) -> None:
    """Flip tracing on/off for every registered wrapper at runtime."""
    global _ENABLED
    _ENABLED = enabled
    for w in _REGISTRY:
        w.active = enabled


@contextmanager
def tracing_enabled() -> Generator[None]:
    """Context manager that enables tracing for its duration."""
    prev = _ENABLED
    set_enabled(True)
    try:
        yield
    finally:
        set_enabled(prev)


__all__ = [
    "is_enabled",
    "set_enabled",
    "traced",
    "tracing_enabled",
]
