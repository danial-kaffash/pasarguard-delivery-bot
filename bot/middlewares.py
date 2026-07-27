"""Per-user flood protection for incoming updates (PLAN.md §8)."""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

logger = logging.getLogger(__name__)


class RateLimitMiddleware(BaseMiddleware):
    """Sliding-window limiter: at most `rate_per_minute` updates per user.

    Updates without a `from_user` (e.g. chat_member) always pass through.
    Over-limit updates are dropped (with a log line), not answered.
    """

    def __init__(self, rate_per_minute: int = 30, window_seconds: float = 60.0) -> None:
        self._rate = rate_per_minute
        self._window = window_seconds
        self._buckets: dict[int, deque[float]] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        user_id = getattr(user, "id", None)
        if user_id is not None:
            now = time.monotonic()
            bucket = self._buckets.setdefault(user_id, deque())
            while bucket and bucket[0] <= now - self._window:
                bucket.popleft()
            if len(bucket) >= self._rate:
                logger.warning("Rate limit exceeded for tg_id=%s; dropping update.", user_id)
                return None
            bucket.append(now)
        return await handler(event, data)
