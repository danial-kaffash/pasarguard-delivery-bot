"""Tests for bot.promo — multi-channel publishing and scheduler."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from bot import promo
from storage import db as store


class FakeBot:
    def __init__(self, username: str = "TestBot"):
        self.username = username
        self.deleted: list[tuple[int, int]] = []
        self.sent: list[dict] = []
        self.pinned: list[dict] = []
        self._next_id = 1000

    async def get_me(self):
        return SimpleNamespace(username=self.username)

    async def delete_message(self, chat_id: int, message_id: int):
        self.deleted.append((chat_id, message_id))

    async def send_message(
        self, chat_id: int, text: str, reply_markup=None, disable_notification=None
    ):
        self._next_id += 1
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_markup": reply_markup,
                "disable_notification": disable_notification,
            }
        )
        return SimpleNamespace(message_id=self._next_id)

    async def pin_chat_message(self, chat_id: int, message_id: int, disable_notification=None):
        self.pinned.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "disable_notification": disable_notification,
            }
        )


@pytest.fixture
async def db(tmp_path):
    conn = await store.connect(tmp_path / "test.db")
    try:
        yield conn
    finally:
        await conn.close()


TG_CHANNEL_ID = -1001234567890


async def _create_channel(db, *, tg_id=TG_CHANNEL_ID):
    return await store.create_channel(db, tg_channel_id=tg_id, title="Test")


# ── publish_promo ────────────────────────────────────────────────────────────


async def test_publish_first_time(db):
    ch = await _create_channel(db)
    bot = FakeBot()
    message_id = await promo.publish_promo(
        bot, db, channel_id=TG_CHANNEL_ID, pin=True, silent=True, channel_db_id=ch.id,
    )

    assert bot.deleted == []
    assert len(bot.sent) == 1
    sent = bot.sent[0]
    assert sent["chat_id"] == TG_CHANNEL_ID
    assert sent["disable_notification"] is True
    assert "تست رایگان" in sent["text"]

    button = sent["reply_markup"].inline_keyboard[0][0]
    assert f"start=join_{TG_CHANNEL_ID}" in button.url

    assert bot.pinned == [
        {"chat_id": TG_CHANNEL_ID, "message_id": message_id, "disable_notification": True}
    ]


async def test_publish_replaces_previous_post(db):
    ch = await _create_channel(db)
    await promo.set_channel_promo_state(db, ch.id, message_id=777, next_run_at=0.0)
    bot = FakeBot()

    await promo.publish_promo(
        bot, db, channel_id=TG_CHANNEL_ID, pin=True, silent=True, channel_db_id=ch.id,
    )

    assert bot.deleted == [(TG_CHANNEL_ID, 777)]
    assert len(bot.sent) == 1


async def test_promo_text_channel_override(db):
    ch = await _create_channel(db)
    key = f"channel:{ch.id}:promo_text"
    await store.set_setting(db, key, "متن اختصاصی کانال")
    bot = FakeBot()

    await promo.publish_promo(
        bot, db, channel_id=TG_CHANNEL_ID, pin=False, silent=True, channel_db_id=ch.id,
    )
    assert bot.sent[0]["text"] == "متن اختصاصی کانال"
    assert bot.pinned == []


async def test_promo_text_global_fallback(db):
    """When no channel-specific text, uses global."""
    await store.set_setting(db, promo.PROMO_TEXT_KEY, "متن سراسری")
    ch = await _create_channel(db)
    bot = FakeBot()

    await promo.publish_promo(
        bot, db, channel_id=TG_CHANNEL_ID, pin=False, silent=True, channel_db_id=ch.id,
    )
    assert bot.sent[0]["text"] == "متن سراسری"


async def test_interval_override_and_fallbacks(db):
    assert await promo.get_interval_hours(db, 6.0) == 6.0
    await store.set_setting(db, promo.PROMO_INTERVAL_KEY, "2.5")
    assert await promo.get_interval_hours(db, 6.0) == 2.5
    await store.set_setting(db, promo.PROMO_INTERVAL_KEY, "not-a-number")
    assert await promo.get_interval_hours(db, 6.0) == 6.0


async def test_channel_interval(db):
    ch = await _create_channel(db)
    assert await promo.get_channel_interval(db, ch.id, 6.0) == 6.0  # default
    key = f"channel:{ch.id}:promo_interval_hours"
    await store.set_setting(db, key, "3.5")
    assert await promo.get_channel_interval(db, ch.id, 6.0) == 3.5


# ── per-channel promo state ──────────────────────────────────────────────────


async def test_channel_promo_state_crud(db):
    ch = await _create_channel(db)
    assert await promo.get_channel_promo_state(db, ch.id) is None

    await promo.set_channel_promo_state(db, ch.id, message_id=123, next_run_at=999.0)
    state = await promo.get_channel_promo_state(db, ch.id)
    assert state is not None
    assert state.message_id == 123
    assert state.next_run_at == 999.0

    # Update.
    await promo.set_channel_promo_state(db, ch.id, message_id=456, next_run_at=1999.0)
    state = await promo.get_channel_promo_state(db, ch.id)
    assert state.message_id == 456


async def test_channel_promo_state_independent(db):
    """Different channels have independent promo states."""
    ch1 = await store.create_channel(db, tg_channel_id=-1, title="A")
    ch2 = await store.create_channel(db, tg_channel_id=-2, title="B")
    await promo.set_channel_promo_state(db, ch1.id, message_id=100, next_run_at=100.0)
    await promo.set_channel_promo_state(db, ch2.id, message_id=200, next_run_at=200.0)

    s1 = await promo.get_channel_promo_state(db, ch1.id)
    s2 = await promo.get_channel_promo_state(db, ch2.id)
    assert s1.message_id == 100
    assert s2.message_id == 200


# ── scheduler ────────────────────────────────────────────────────────────────


async def test_scheduler_posts_to_channel(db, monkeypatch):
    monkeypatch.setattr(promo, "STARTUP_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(promo, "CHANNEL_SCAN_INTERVAL", 0.1)
    ch = await _create_channel(db)
    bot = FakeBot()
    settings = SimpleNamespace()

    task = asyncio.create_task(promo.run_scheduler(bot, db, settings))
    for _ in range(200):
        if bot.sent:
            break
        await asyncio.sleep(0.02)

    assert bot.sent, "scheduler did not publish in time"
    assert bot.sent[0]["chat_id"] == TG_CHANNEL_ID

    # State must be persisted.
    state = None
    for _ in range(100):
        state = await promo.get_channel_promo_state(db, ch.id)
        if state and state.next_run_at > time.time():
            break
        await asyncio.sleep(0.01)
    assert state is not None
    assert state.message_id > 1000

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_scheduler_skips_paused_channel(db, monkeypatch):
    monkeypatch.setattr(promo, "STARTUP_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(promo, "CHANNEL_SCAN_INTERVAL", 0.1)
    monkeypatch.setattr(promo, "PAUSE_POLL_SECONDS", 0.1)
    ch = await _create_channel(db)
    from bot.pause import set_channel_paused
    await set_channel_paused(db, ch.id, True)

    bot = FakeBot()
    settings = SimpleNamespace()

    task = asyncio.create_task(promo.run_scheduler(bot, db, settings))
    await asyncio.sleep(0.5)

    assert not bot.sent, "should not post to paused channel"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
