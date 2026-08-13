"""In-process TTL cache for hot-path data.

A tiny dict-based cache with monotonic-clock expiry — no locks needed for
CPython dict ops, no serialization overhead. Used to keep repeated panel
refreshes from re-hitting networks or recomputing analytics.
"""

from __future__ import annotations

import functools
import time
from typing import Any, Callable, TypeVar

F = TypeVar("F", bound=Callable[..., Any])

_SENTINEL = object()


class TTLCache:
    def __init__(self, ttl: float, max_items: int = 512) -> None:
        self.ttl = ttl
        self.max_items = max_items
        self._data: dict[Any, tuple[float, Any]] = {}

    def get(self, key: Any) -> Any:
        entry = self._data.get(key)
        if entry is None:
            return _SENTINEL
        expires, value = entry
        if time.monotonic() > expires:
            self._data.pop(key, None)
            return _SENTINEL
        return value

    def put(self, key: Any, value: Any) -> None:
        if len(self._data) >= self.max_items:
            # Drop the oldest ~25% — O(n) but rare and n is small.
            for k in sorted(self._data, key=lambda k: self._data[k][0])[
                : self.max_items // 4
            ]:
                self._data.pop(k, None)
        self._data[key] = (time.monotonic() + self.ttl, value)

    def clear(self) -> None:
        self._data.clear()


def ttl_cache(ttl: float, max_items: int = 256) -> Callable[[F], F]:
    """Memoize a function with per-arguments TTL expiry."""

    def decorator(fn: F) -> F:
        cache = TTLCache(ttl, max_items)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            hit = cache.get(key)
            if hit is not _SENTINEL:
                return hit
            value = fn(*args, **kwargs)
            cache.put(key, value)
            return value

        wrapper.cache = cache  # type: ignore[attr-defined]
        return wrapper  # type: ignore[return-value]

    return decorator
