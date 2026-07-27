"""Tests for chat_member join/leave tracking."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from bot.handlers import member_events as me
from storage import db as store

CHANNEL = -1001234567890
SETTINGS = SimpleNamespace(channel_id=CHANNEL)


def make_event(old: str, new: str, chat_id: int = CHANNEL, user_id: int = 7):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        old_chat_member=SimpleNamespace(status=old),
        new_chat_member=SimpleNamespace(status=new, user=SimpleNamespace(id=user_id)),
    )


@pytest.fixture
async def db(tmp_path):
    conn = await store.connect(tmp_path / "test.db")
    try:
        yield conn
    finally:
        await conn.close()


async def test_join_records_event_and_state(db):
    await me.on_chat_member(make_event("left", "member"), db=db, settings=SETTINGS)
    since = datetime.now(UTC) - timedelta(minutes=1)
    assert await store.count_member_events(db, "join", since) == 1
    assert await store.count_member_events(db, "leave", since) == 0
    assert await store.count_chat_members(db, CHANNEL) == 1


async def test_leave_records_event_and_updates_state(db):
    await me.on_chat_member(make_event("member", "left"), db=db, settings=SETTINGS)
    since = datetime.now(UTC) - timedelta(minutes=1)
    assert await store.count_member_events(db, "leave", since) == 1
    assert await store.count_chat_members(db, CHANNEL) == 0  # status 'left' not counted


async def test_role_change_is_not_a_join(db):
    await me.on_chat_member(make_event("member", "administrator"), db=db, settings=SETTINGS)
    since = datetime.now(UTC) - timedelta(minutes=1)
    assert await store.count_member_events(db, "join", since) == 0
    assert await store.count_chat_members(db, CHANNEL) == 1  # admin still counted


async def test_other_chats_ignored(db):
    await me.on_chat_member(make_event("left", "member", chat_id=-999), db=db, settings=SETTINGS)
    assert await store.count_chat_members(db, -999) == 0
    since = datetime.now(UTC) - timedelta(minutes=1)
    assert await store.count_member_events(db, "join", since) == 0
