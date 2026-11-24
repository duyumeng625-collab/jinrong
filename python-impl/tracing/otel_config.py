"""Tracing is intentionally disabled; this compatibility decorator adds no spans."""

from __future__ import annotations

import functools
from typing import Any, Callable


def trace_agent_call(agent_name: str) -> Callable:
    """Keep existing Agent declarations compatible without collecting call traces."""
    del agent_name

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            return await func(*args, **kwargs)
        return wrapper
    return decorator
