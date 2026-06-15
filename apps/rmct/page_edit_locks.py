"""Org-wide page / product-scoped edit locks (one editor at a time per scope)."""

from __future__ import annotations

import time
from typing import Optional

# key -> (user_id, display_name, expires_at)
_MEM_PAGE_EDIT: dict[str, tuple[str, str, float]] = {}

PAGE_EDIT_TTL_SECONDS = 4 * 60 * 60


def page_edit_lock_key(
    *, org_id: str, model_id: str, page: str, product_id: str | None = None
) -> str:
    base = f"pageedit:{org_id}:{model_id}:{page}"
    if product_id:
        return f"{base}:{product_id}"
    return base


def _purge_expired(now: float | None = None) -> None:
    t = now if now is not None else time.time()
    expired = [k for k, v in _MEM_PAGE_EDIT.items() if v[2] <= t]
    for k in expired:
        _MEM_PAGE_EDIT.pop(k, None)


def mem_try_page_edit(
    *, key: str, user_id: str, name: str, ttl_seconds: int = PAGE_EDIT_TTL_SECONDS
) -> bool:
    now = time.time()
    _purge_expired(now)
    current = _MEM_PAGE_EDIT.get(key)
    if current and current[2] > now and current[0] != str(user_id):
        return False
    _MEM_PAGE_EDIT[key] = (str(user_id), name or "Someone", now + ttl_seconds)
    return True


def mem_get_page_edit(key: str) -> Optional[tuple[str, str]]:
    now = time.time()
    _purge_expired(now)
    current = _MEM_PAGE_EDIT.get(key)
    if not current or current[2] <= now:
        _MEM_PAGE_EDIT.pop(key, None)
        return None
    return current[0], current[1]


def mem_release_page_edit(*, key: str, user_id: str) -> bool:
    current = _MEM_PAGE_EDIT.get(key)
    if current and current[0] == str(user_id):
        _MEM_PAGE_EDIT.pop(key, None)
        return True
    return False


def mem_release_user_page_edits(*, user_id: str) -> list[str]:
    uid = str(user_id)
    keys = [k for k, v in _MEM_PAGE_EDIT.items() if v[0] == uid]
    for k in keys:
        _MEM_PAGE_EDIT.pop(k, None)
    return keys
