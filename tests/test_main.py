"""Tests for bot.main — dispatcher wiring, startup/shutdown lifecycle, error handler."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot import main as bot_main
from bot.main import _refresh_channel_titles, build_dispatcher, main, on_error
from storage import db as store
from tests.helpers import FakeBot, FakeFileBot, make_settings


@pytest.fixture
def fake_telegram(monkeypatch):
    """Keep build_dispatcher's real Bot from hitting the Telegram API in tests."""

    async def fake_get_me(self):
        return SimpleNamespace(username="TestBot")

    async def fake_get_chat(self, chat_id: int):
        return SimpleNamespace(title="Fake Chat")

    monkeypatch.setattr("aiogram.client.bot.Bot.get_me", fake_get_me)
    monkeypatch.setattr("aiogram.client.bot.Bot.get_chat", fake_get_chat)


def _main_settings(tmp_path, **overrides) -> SimpleNamespace:
    overrides.setdefault("panel_base_url", "")
    s = make_settings(**overrides)
    s.telegram_bot_token = "123456:TEST-TOKEN"
    s.rate_limit_per_minute = 20
    s.db_path = str(tmp_path / "bot.db")
    s.offer_groups_file = tmp_path / "offer_groups.json"
    s.panel_admin_username = ""
    s.panel_admin_password = ""
    s.panel_verify_ssl = True
    s.panel_timeout_seconds = 15.0
    s.trial_protocols = "vless"
    s.auto_delete_days = 11
    s.channel_id = 0
    s.log_level = "INFO"
    return s


# ── build_dispatcher + startup/shutdown lifecycle ────────────────────────────
#
# NOTE: the handler routers are module-level singletons and can only be attached
# to one Dispatcher per process — build_dispatcher must run exactly once here,
# so wiring, startup and shutdown are exercised in a single lifecycle test.


async def test_dispatcher_lifecycle_wiring_startup_shutdown(tmp_path, fake_telegram):
    settings = _main_settings(
        tmp_path,
        panel_base_url="https://panel.test:8000",  # exercises the .env fallback
    )
    settings.offer_groups_file.write_text('[{"id": 3, "label": "NL"}]', encoding="utf-8")

    bot, dp = build_dispatcher(settings)
    try:
        # ── wiring ──
        assert dp["settings"] is settings
        # Six handler routers: admin, backup, panel, trial, join_request, member_events.
        assert len(dp.sub_routers) >= 6
        # Rate limiter is installed on both update types.
        assert len(dp.message.middleware) >= 1
        assert len(dp.callback_query.middleware) >= 1
        # Startup/shutdown/error hooks registered.
        assert dp.startup.handlers
        assert dp.shutdown.handlers
        assert dp.errors.handlers

        # ── startup ──
        await dp.emit_startup(bot=FakeBot())

        db = dp["db"]
        assert db is not None
        rows = await db.execute_fetchall("SELECT COUNT(*) AS c FROM offer_groups")
        assert rows[0]["c"] == 1  # seeded from the JSON file

        # No panels in DB + PANEL_BASE_URL set → "Default" panel created from .env.
        panels = await store.list_panels(db, active_only=True)
        assert len(panels) == 1
        assert panels[0].name == "Default"
        assert panels[0].base_url == "https://panel.test:8000"

        assert dp["panel_manager"] is not None
        task = dp.get("scheduler_task")
        assert task is not None and not task.done()

        # ── shutdown ──
        await dp.emit_shutdown(bot=FakeBot())
        assert task.cancelled()
    finally:
        await bot.session.close()


# ── on_error ─────────────────────────────────────────────────────────────────


def _message_update(chat_id: int = 42):
    from datetime import UTC, datetime

    from aiogram.types import Chat, Message, Update, User

    return Update(
        update_id=1,
        message=Message(
            message_id=10,
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type="private"),
            from_user=User(id=7, is_bot=False, first_name="Tester"),
            text="hello",
        ),
    )


def _callback_update(chat_id: int = 43):
    from datetime import UTC, datetime

    from aiogram.types import CallbackQuery, Chat, Message, Update, User

    return Update(
        update_id=2,
        callback_query=CallbackQuery(
            id="cb1",
            from_user=User(id=7, is_bot=False, first_name="Tester"),
            chat_instance="ci",
            message=Message(
                message_id=11,
                date=datetime.now(UTC),
                chat=Chat(id=chat_id, type="private"),
                text="menu",
            ),
            data="x",
        ),
    )


async def test_on_error_notifies_message_chat():
    from aiogram.types import ErrorEvent

    event = ErrorEvent(update=_message_update(42), exception=RuntimeError("boom"))
    bot = FakeBot()
    result = await on_error(event, bot=bot)
    assert result is True
    assert len(bot.sent) == 1
    assert bot.sent[0]["chat_id"] == 42


async def test_on_error_notifies_callback_chat():
    from aiogram.types import ErrorEvent

    event = ErrorEvent(update=_callback_update(43), exception=RuntimeError("boom"))
    bot = FakeBot()
    result = await on_error(event, bot=bot)
    assert result is True
    assert bot.sent[0]["chat_id"] == 43


async def test_on_error_without_chat_stays_silent():
    from aiogram.types import ErrorEvent, Update

    event = ErrorEvent(update=Update(update_id=9), exception=RuntimeError("boom"))
    bot = FakeBot()
    result = await on_error(event, bot=bot)
    assert result is True
    assert bot.sent == []


# ── _refresh_channel_titles ──────────────────────────────────────────────────


async def test_refresh_channel_titles_fills_empty_titles(tmp_path):
    db = await store.connect(tmp_path / "test.db")
    try:
        await store.create_channel(db, tg_channel_id=-100111, title="")
        await store.create_channel(db, tg_channel_id=-100222, title="Kept")
        bot = FakeFileBot(chat_titles={-100111: "Fetched Title"})

        await _refresh_channel_titles(bot, db)

        ch1 = await store.get_channel_by_tg_id(db, -100111)
        assert ch1.title == "Fetched Title"
        ch2 = await store.get_channel_by_tg_id(db, -100222)
        assert ch2.title == "Kept"  # already titled — never re-fetched
        assert bot.got_chats == [-100111]
    finally:
        await db.close()


async def test_refresh_channel_titles_tolerates_telegram_errors(tmp_path):
    db = await store.connect(tmp_path / "test.db")
    try:
        await store.create_channel(db, tg_channel_id=-100333, title="")
        bot = FakeFileBot()  # get_chat raises — every fetch fails

        await _refresh_channel_titles(bot, db)

        ch = await store.get_channel_by_tg_id(db, -100333)
        assert ch.title == ""  # unchanged, error swallowed
    finally:
        await db.close()


# ── main() ───────────────────────────────────────────────────────────────────


def test_main_exits_without_bot_token(monkeypatch, tmp_path):
    settings = _main_settings(tmp_path)
    settings.telegram_bot_token = ""
    monkeypatch.setattr(bot_main, "get_settings", lambda: settings)
    with pytest.raises(SystemExit):
        main()
