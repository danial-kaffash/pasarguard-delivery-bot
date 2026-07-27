"""Tests for the per-user rate-limit middleware."""

from __future__ import annotations

from types import SimpleNamespace

from bot.middlewares import RateLimitMiddleware


async def _ok_handler(event, data):
    return "ok"


def _event(user_id: int | None):
    user = SimpleNamespace(id=user_id) if user_id is not None else None
    return SimpleNamespace(from_user=user)


async def test_allows_up_to_limit_then_drops():
    mw = RateLimitMiddleware(rate_per_minute=2)
    event = _event(5)
    assert await mw(_ok_handler, event, {}) == "ok"
    assert await mw(_ok_handler, event, {}) == "ok"
    assert await mw(_ok_handler, event, {}) is None  # over the limit → dropped


async def test_limit_is_per_user():
    mw = RateLimitMiddleware(rate_per_minute=1)
    assert await mw(_ok_handler, _event(1), {}) == "ok"
    assert await mw(_ok_handler, _event(1), {}) is None
    assert await mw(_ok_handler, _event(2), {}) == "ok"  # other user unaffected


async def test_events_without_user_always_pass():
    mw = RateLimitMiddleware(rate_per_minute=0)  # even a zero budget
    for _ in range(3):
        assert await mw(_ok_handler, _event(None), {}) == "ok"
