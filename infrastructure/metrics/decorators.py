from __future__ import annotations

from functools import wraps
from inspect import iscoroutinefunction
from time import perf_counter
from typing import Any, Callable

from infrastructure.metrics.main import DURATION_SECONDS, ERRORS


def _get_labels(
    func: Callable[..., Any],
    target: str | Callable[..., Any] | None,
    operation: str | None,
) -> tuple[str, str]:
    if isinstance(target, str):
        return target, operation or func.__qualname__.split('.')[-1]

    names = func.__qualname__.split('.')
    if len(names) > 1:
        return names[-2], operation or names[-1]
    return 'global', operation or names[-1]


def metrics(target: str | Callable[..., Any] | None = None, operation: str | None = None) -> Callable[..., Any]:
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        target_name, operation_name = _get_labels(func, target, operation)

        @wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            start = perf_counter()
            try:
                return await func(*args, **kwargs)
            except Exception:
                ERRORS.labels(target=target_name, operation=operation_name).inc()
                raise
            finally:
                DURATION_SECONDS.labels(target=target_name, operation=operation_name).observe(
                    perf_counter() - start
                )

        @wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            start = perf_counter()
            try:
                return func(*args, **kwargs)
            except Exception:
                ERRORS.labels(target=target_name, operation=operation_name).inc()
                raise
            finally:
                DURATION_SECONDS.labels(target=target_name, operation=operation_name).observe(
                    perf_counter() - start
                )

        return async_wrapper if iscoroutinefunction(func) else sync_wrapper

    return decorator(target) if callable(target) else decorator
