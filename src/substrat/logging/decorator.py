"""Method-level logging decorator for provider sessions."""

import functools
import inspect
from collections.abc import AsyncGenerator, Callable
from typing import Any

from substrat.logging.event_log import EventLog


def _build_args_dict(
    fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    """Map positional + keyword args to parameter names, skipping self."""
    sig = inspect.signature(fn)
    # None stands in for self (already stripped from args by the wrapper).
    bound = sig.bind(None, *args, **kwargs)
    bound.arguments.pop("self", None)
    return dict(bound.arguments)


def _get_log(instance: Any) -> EventLog | None:
    log: EventLog | None = getattr(instance, "_log", None)
    return log


def log_method(
    *,
    before: bool = False,
    after: bool = False,
) -> Callable[[Any], Any]:
    """Log method calls to the instance's EventLog.

    Expects the instance to have a `_log: EventLog | None` attribute.
    If _log is None, the method runs without logging.
    Handles both async coroutines and async generators (streaming).
    """

    def decorator(fn: Any) -> Any:
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
                        chunks.append(chunk)
                        yield chunk
                finally:
                    if log and after:
                        log.log(f"{event_name}.result", {"text": "".join(chunks)})

            return gen_wrapper
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

            return wrapper

    return decorator


def _serialize_result(value: Any) -> Any:
    """Best-effort serialization for log entries."""
    if isinstance(value, bytes):
        import base64

        return base64.b64encode(value).decode()
    return value
