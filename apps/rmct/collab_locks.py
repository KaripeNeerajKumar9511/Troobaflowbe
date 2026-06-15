"""In-process cell locks when Redis is unavailable (local dev / in-memory channel layer)."""

from __future__ import annotations

import time
from typing import Optional

# key -> (user_id, display_name, expires_at)
_MEM_LOCKS: dict[str, tuple[str, str, float]] = {}


def _purge_expired(now: float | None = None) -> None:
    t = now if now is not None else time.time()
    expired = [k for k, v in _MEM_LOCKS.items() if v[2] <= t]
    for k in expired:
        _MEM_LOCKS.pop(k, None)


def mem_try_lock(*, key: str, user_id: str, name: str, ttl_seconds: int = 45) -> bool:
    """Return True if lock acquired by this user."""
    now = time.time()
    _purge_expired(now)
    current = _MEM_LOCKS.get(key)
    if current and current[2] > now and current[0] != str(user_id):
        return False
    _MEM_LOCKS[key] = (str(user_id), name or "Someone", now + ttl_seconds)
    return True


def mem_get_lock(key: str) -> Optional[tuple[str, str]]:
    """Return (user_id, name) if lock is held, else None."""
    now = time.time()
    _purge_expired(now)
    current = _MEM_LOCKS.get(key)
    if not current or current[2] <= now:
        _MEM_LOCKS.pop(key, None)
        return None
    return current[0], current[1]


def mem_unlock(*, key: str, user_id: str) -> None:
    current = _MEM_LOCKS.get(key)
    if current and current[0] == str(user_id):
        _MEM_LOCKS.pop(key, None)


def mem_release_user_keys(*, user_id: str) -> list[str]:
    """Drop all locks held by user (disconnect). Returns released keys."""
    uid = str(user_id)
    keys = [k for k, v in _MEM_LOCKS.items() if v[0] == uid]
    for k in keys:
        _MEM_LOCKS.pop(k, None)
    return keys
