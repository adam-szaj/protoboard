# SPDX-License-Identifier: MIT
"""Tests for the @traced debug decorator."""

import structlog

from protoboard.services.debug_trace import (
    is_enabled,
    set_enabled,
    traced,
    tracing_enabled,
)


@traced()
def _add(a: int, b: int = 1) -> int:
    return a + b


@traced()
def _boom() -> None:
    raise ValueError("nope")


class _Obj:
    @traced()
    def double(self, x: int) -> int:
        return x * 2


def test_disabled_is_passthrough() -> None:
    set_enabled(False)
    assert is_enabled() is False
    assert _add(2, 3) == 5  # still works, no logging


def test_logs_args_and_return(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    events: list[dict] = []

    def _capture(logger, method_name, event_dict):  # type: ignore[no-untyped-def]
        events.append(dict(event_dict))
        raise structlog.DropEvent

    structlog.configure(processors=[_capture])
    try:
        with tracing_enabled():
            assert _add(2, b=4) == 6
    finally:
        structlog.reset_defaults()

    call = next(e for e in events if e.get("event") == "trace.call[_add]")
    ret = next(e for e in events if e.get("event") == "trace.return[_add]")
    assert call["args"] == ["2"]
    assert call["kwargs"] == {"b": "4"}
    assert ret["result"] == "6"


def test_exception_is_logged_and_reraised() -> None:
    events: list[dict] = []

    def _capture(logger, method_name, event_dict):  # type: ignore[no-untyped-def]
        events.append(dict(event_dict))
        raise structlog.DropEvent

    structlog.configure(processors=[_capture])
    try:
        with tracing_enabled():
            try:
                _boom()
            except ValueError:
                pass
    finally:
        structlog.reset_defaults()

    assert any(e.get("event") == "trace.raise[_boom]" for e in events)


def test_method_binding() -> None:
    set_enabled(False)
    assert _Obj().double(21) == 42  # __get__ binds self correctly


def test_context_manager_restores_state() -> None:
    set_enabled(False)
    with tracing_enabled():
        assert is_enabled() is True
    assert is_enabled() is False
