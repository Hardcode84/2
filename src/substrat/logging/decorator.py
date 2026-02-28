# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Method-level logging decorator for provider sessions."""

import functools
import inspect
from collections.abc import AsyncGenerator, Callable
from typing import Any, Protocol, TypeVar, runtime_checkable

from substrat.logging.event_log import EventLog


@runtime_checkable
class Loggable(Protocol):
    """Instance with an optional event log. Used by @log_method."""

    _log: EventLog | None


def _build_args_dict(
    fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    """Map positional + keyword args to parameter names, skipping self."""
    sig = inspect.signature(fn)
    # None stands in for self (already stripped from args by the wrapper).
    bound = sig.bind(None, *args, **kwargs)
    bound.arguments.pop("self", None)
    return dict(bound.arguments)


def _get_log(instance: Loggable) -> EventLog | None:
    return instance._log


_F = TypeVar("_F", bound=Callable[..., Any])


def log_method(
    *,
    before: bool = False,
    after: bool = False,
) -> Callable[[_F], _F]:
    """Log method calls to the instance's EventLog.

    Expects the instance to have a `_log: EventLog | None` attribute.
    If _log is None, the method runs without logging.
    Handles both async coroutines and async generators (streaming).
    """

    def decorator(fn: _F) -> _F:
        event_name = fn.__name__

        if inspect.isasyncgenfunction(fn):

            @functools.wraps(fn)
            async def gen_wrapper(
                self: Any, *args: Any, **kwargs: Any
            ) -> AsyncGenerator[Any, None]:
                log = _get_log(self)
                args_dict = _build_args_dict(fn, args, kwargs) if log else {}
                if log and before:
                    log.log(event_name, args_dict)
                chunks: list[str] = []
                try:
                    async for chunk in fn(self, *args, **kwargs):
                        chunks.append(str(chunk))
                        yield chunk
                finally:
                    if log and after:
                        result_data: dict[str, Any] = {**args_dict}
                        result_data["result"] = "".join(chunks)
                        log.log(f"{event_name}.result", result_data)

            return gen_wrapper  # type: ignore[return-value]
        else:

            @functools.wraps(fn)
            async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
                log = _get_log(self)
                args_dict = _build_args_dict(fn, args, kwargs) if log else {}
                if log and before:
                    log.log(event_name, args_dict)
                result = await fn(self, *args, **kwargs)
                if log and after:
                    result_data: dict[str, Any] = {**args_dict}
                    if result is not None:
                        result_data["result"] = _serialize_result(result)
                    log.log(f"{event_name}.result", result_data)
                return result

            return wrapper  # type: ignore[return-value]

    return decorator


def _serialize_result(value: Any) -> Any:
    """Best-effort serialization for log entries."""
    if isinstance(value, bytes):
        import base64

        return base64.b64encode(value).decode()
    if isinstance(value, str | int | float | bool | type(None)):
        return value
    return str(value)
