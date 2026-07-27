"""Tests for bot.promo — publishing logic and the scheduler loop.

The Telegram Bot is replaced by a FakeBot that records calls.
"""

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


SETTINGS = SimpleNamespace(
    channel_id=-1001234567890,
    promo_interval_hours=6.0,
    promo_pin=True,
    promo_silent=True,
)


async def test_publish_first_time(db):
    bot = FakeBot()
    message_id = await promo.publish_promo(
        bot, db, channel_id=SETTINGS.channel_id, pin=True, silent=True
    )

    assert bot.deleted == []  # nothing to delete on first run
    assert len(bot.sent) == 1
    sent = bot.sent[0]
    assert sent["chat_id"] == SETTINGS.channel_id
    assert sent["disable_notification"] is True
    assert "تست رایگان" in sent["text"]  # default Persian seed text

    button = sent["reply_markup"].inline_keyboard[0][0]
    assert button.url == f"https://t.me/{bot.username}?start={promo.START_PAYLOAD}"

    assert bot.pinned == [
        {"chat_id": SETTINGS.channel_id, "message_id": message_id, "disable_notification": True}
    ]


async def test_publish_replaces_previous_post(db):
    bot = FakeBot()
    await store.set_promo_state(db, SETTINGS.channel_id, message_id=777, next_run_at=0.0)

    await promo.publish_promo(bot, db, channel_id=SETTINGS.channel_id, pin=True, silent=True)

    assert bot.deleted == [(SETTINGS.channel_id, 777)]
    assert len(bot.sent) == 1


async def test_promo_text_override_beats_file(db):
    bot = FakeBot()
    await store.set_setting(db, promo.PROMO_TEXT_KEY, "متن دلخواه صاحب ربات")
    await promo.publish_promo(bot, db, channel_id=SETTINGS.channel_id, pin=False, silent=True)
    assert bot.sent[0]["text"] == "متن دلخواه صاحب ربات"
    assert bot.pinned == []  # pin disabled


async def test_interval_override_and_fallbacks(db):
    assert await promo.get_interval_hours(db, 6.0) == 6.0
    await store.set_setting(db, promo.PROMO_INTERVAL_KEY, "2.5")
    assert await promo.get_interval_hours(db, 6.0) == 2.5
    await store.set_setting(db, promo.PROMO_INTERVAL_KEY, "not-a-number")
    assert await promo.get_interval_hours(db, 6.0) == 6.0
    await store.set_setting(db, promo.PROMO_INTERVAL_KEY, "-3")
    assert await promo.get_interval_hours(db, 6.0) == 6.0


async def test_scheduler_posts_and_persists_next_run(db, monkeypatch):
    monkeypatch.setattr(promo, "STARTUP_GRACE_SECONDS", 0.0)
    bot = FakeBot()

    task = asyncio.create_task(promo.run_scheduler(bot, db, SETTINGS))
    # wait for the first publish
    for _ in range(100):
        if bot.sent:
            break
        await asyncio.sleep(0.01)
    assert bot.sent, "scheduler did not publish in time"

    # state must be persisted with a future next_run_at (~ now + 6h)
    state = None
    for _ in range(100):
        state = await store.get_promo_state(db)
        if state and state.next_run_at > time.time():
            break
        await asyncio.sleep(0.01)
    assert state is not None
    assert state.message_id > 1000  # the FakeBot's generated message id
    expected = time.time() + 6 * 3600
    assert abs(state.next_run_at - expected) < 120  # generous bound

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
