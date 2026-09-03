# SPDX-License-Identifier: MIT
"""Synchronous executor used when routing parallelism is disabled."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from typing import ParamSpec, TypeVar

_P = ParamSpec("_P")
_T = TypeVar("_T")


class InlineExecutor:
    """Small ``Executor``-compatible adapter that runs work immediately."""

    def submit(
        self, fn: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs
    ) -> Future[_T]:
        future: Future[_T] = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001
            future.set_exception(exc)
        return future

    def shutdown(self, *args: object, **kwargs: object) -> None:
        pass


__all__ = ["InlineExecutor"]
