import time
from typing import Dict, Generic, List, Optional, Set, TypeVar

T = TypeVar("T")


class ProcessListCache(Generic[T]):
    def __init__(self, expiry_seconds: int = 300):
        self._cache: Dict[str, tuple[float, List[T]]] = {}
        self._expiry_seconds = expiry_seconds

    def get(self, key: str) -> Optional[List[T]]:
        entry = self._cache.get(key)
        if entry:
            timestamp, value = entry
            if time.time() - timestamp < self._expiry_seconds:
                return value
        return None

    def set(self, key: str, value: List[T]):
        self._cache[key] = (time.time(), value)

    def invalidate(self, key: str) -> None:
        self._cache.pop(key, None)

    def invalidate_stale_keys(self, valid_keys: Set[str]) -> None:
        """Remove cache entries whose keys are no longer in valid_keys."""
        stale = [k for k in self._cache if k not in valid_keys]
        for k in stale:
            del self._cache[k]

    def clear(self) -> None:
        self._cache.clear()


class ProcessCache(Generic[T]):
    """Cache for individual process objects keyed by process id."""

    def __init__(self, expiry_seconds: int = 300):
        self._cache: Dict[str, tuple[float, T]] = {}
        self._expiry_seconds = expiry_seconds

    def get(self, key: str) -> Optional[T]:
        entry = self._cache.get(key)
        if entry:
            timestamp, value = entry
            if time.time() - timestamp < self._expiry_seconds:
                return value
        return None

    def set(self, key: str, value: T):
        self._cache[key] = (time.time(), value)

    def invalidate(self, key: str) -> None:
        self._cache.pop(key, None)

    def clear(self) -> None:
        self._cache.clear()
