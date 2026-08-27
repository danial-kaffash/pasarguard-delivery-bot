"""Tests for channel-scoped pause functions in bot/pause.py."""

from __future__ import annotations

import pytest

from bot.pause import (
    is_channel_joins_paused,
    is_channel_paused,
    is_joins_paused,
    is_paused,
    set_channel_joins_paused,
    set_channel_paused,
    set_joins_paused,
    set_paused,
)
from storage import db as store


@pytest.fixture
async def db(tmp_path):
    conn = await store.connect(tmp_path / "test.db")
    try:
        yield conn
    finally:
        await conn.close()


# ── Global pause (backward compat) ──────────────────────────────────────────


async def test_global_pause_default(db):
    assert await is_paused(db) is False


async def test_global_pause_set(db):
    await set_paused(db, True)
    assert await is_paused(db) is True
    await set_paused(db, False)
    assert await is_paused(db) is False


async def test_global_joins_pause_default(db):
    assert await is_joins_paused(db) is False


async def test_global_joins_pause_set(db):
    await set_joins_paused(db, True)
    assert await is_joins_paused(db) is True


# ── Channel-scoped pause ────────────────────────────────────────────────────


async def test_channel_pause_default(db):
    assert await is_channel_paused(db, 42) is False


async def test_channel_pause_set(db):
    await set_channel_paused(db, 42, True)
    assert await is_channel_paused(db, 42) is True
    await set_channel_paused(db, 42, False)
    assert await is_channel_paused(db, 42) is False


async def test_channel_pause_independent(db):
    """Pausing one channel doesn't affect another."""
    await set_channel_paused(db, 1, True)
    assert await is_channel_paused(db, 1) is True
    assert await is_channel_paused(db, 2) is False


async def test_channel_joins_pause_default(db):
    assert await is_channel_joins_paused(db, 42) is False


async def test_channel_joins_pause_set(db):
    await set_channel_joins_paused(db, 42, True)
    assert await is_channel_joins_paused(db, 42) is True
    await set_channel_joins_paused(db, 42, False)
    assert await is_channel_joins_paused(db, 42) is False


async def test_channel_joins_pause_independent(db):
    await set_channel_joins_paused(db, 1, True)
    assert await is_channel_joins_paused(db, 1) is True
    assert await is_channel_joins_paused(db, 2) is False


# ── Global flag propagates to channel check ─────────────────────────────────


async def test_global_pause_affects_channel_check(db):
    """When the global pause is on, is_channel_paused returns True for any channel."""
    await set_paused(db, True)
    assert await is_channel_paused(db, 42) is True
    assert await is_channel_paused(db, 99) is True


async def test_global_joins_pause_affects_channel_check(db):
    await set_joins_paused(db, True)
    assert await is_channel_joins_paused(db, 42) is True


async def test_global_off_channel_on(db):
    """Channel-specific pause works independently when global is off."""
    await set_paused(db, False)
    await set_channel_paused(db, 42, True)
    assert await is_channel_paused(db, 42) is True
    assert await is_channel_paused(db, 99) is False
