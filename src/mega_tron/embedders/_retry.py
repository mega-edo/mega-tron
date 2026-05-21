"""Tiny retry helper — exponential backoff for transient HTTP/SDK errors.

Kept inline (no tenacity dep) because the call sites are 2 files × 1 loop.
"""
from __future__ import annotations

import random
import time
from typing import Callable, TypeVar

T = TypeVar("T")


def retry(
    fn: Callable[[], T],
    *,
    attempts: int = 4,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
) -> T:
    """Run `fn` with exponential backoff + jitter on any Exception."""
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — caller knows the SDK's exceptions
            last_exc = e
            if i == attempts - 1:
                break
            delay = min(max_delay, base_delay * (2**i)) * (0.5 + random.random())
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc
