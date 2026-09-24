from collections.abc import Callable
from dataclasses import dataclass
from threading import Condition
from time import monotonic
from typing import Generic, Hashable, TypeVar


CacheKey = TypeVar("CacheKey", bound=Hashable)
CacheValue = TypeVar("CacheValue")


class CacheBusyError(RuntimeError):
    """Raised when the first load is still running and no cached value exists."""


@dataclass(frozen=True)
class _CacheEntry(Generic[CacheValue]):
    value: CacheValue
    fresh_until: float
    stale_until: float


class SingleFlightTTLCache(Generic[CacheKey, CacheValue]):
    """Small in-process cache that allows only one loader per key at a time."""

    def __init__(
        self,
        *,
        ttl_seconds: float,
        stale_seconds: float,
        wait_seconds: float,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._stale_seconds = stale_seconds
        self._wait_seconds = wait_seconds
        self._condition = Condition()
        self._entries: dict[CacheKey, _CacheEntry[CacheValue]] = {}
        self._refreshing: set[CacheKey] = set()

    def get_or_load(
        self,
        key: CacheKey,
        loader: Callable[[], CacheValue],
    ) -> CacheValue:
        with self._condition:
            now = monotonic()
            entry = self._entries.get(key)
            if entry is not None and entry.fresh_until > now:
                return entry.value

            if key in self._refreshing:
                if entry is not None and entry.stale_until > now:
                    return entry.value

                deadline = now + self._wait_seconds
                while key in self._refreshing:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise CacheBusyError(
                            "Panel refresh is still running and no cached value exists."
                        )
                    self._condition.wait(remaining)

                entry = self._entries.get(key)
                now = monotonic()
                if entry is not None and entry.stale_until > now:
                    return entry.value

            self._refreshing.add(key)
            stale_entry = entry

        try:
            value = loader()
        except Exception:
            with self._condition:
                self._refreshing.discard(key)
                self._condition.notify_all()
                if stale_entry is not None and stale_entry.stale_until > monotonic():
                    return stale_entry.value
            raise

        now = monotonic()
        with self._condition:
            self._entries[key] = _CacheEntry(
                value=value,
                fresh_until=now + self._ttl_seconds,
                stale_until=now + self._stale_seconds,
            )
            self._refreshing.discard(key)
            self._condition.notify_all()
        return value
