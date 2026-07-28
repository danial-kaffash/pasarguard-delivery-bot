"""Tests for the channel join-request handler — multi-tenant version."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from bot.handlers import join_request as jr
from bot.handlers.admin import cmd_pausejoins, cmd_resumejoins, cmd_setjoindelay, cmd_joinstats
from bot.pause import is_joins_paused, is_channel_joins_paused, set_joins_paused, set_channel_joins_paused
from storage import db as store
from tests.helpers import FakeBotWithDM, FakeChatJoinRequest, FakeMessage, FakePanel, FakePanelManager, make_settings

CHANNEL_TG_ID = -1001234567890
SETTINGS = make_settings()


@pytest.fixture
async def db(tmp_path):
    conn = await store.connect(tmp_path / "test.db")
    try:
        yield conn
    finally:
        await conn.close()


async def _setup_channel(db, *, tg_id=CHANNEL_TG_ID, delay=10, panel_groups=(2,)):
    """Create a panel, channel, and offer groups for testing."""
    panel = await store.create_panel(
        db, name="TestPanel", base_url="https://panel.test",
        admin_username="admin", admin_password="pw",
    )
    ch = await store.create_channel(
        db, tg_channel_id=tg_id, title="Test",
        join_approval_delay_seconds=delay,
    )
    for gid in panel_groups:
        await store.upsert_channel_offer_group(
            db, channel_id=ch.id, panel_id=panel.id, group_id=gid, label=f"group-{gid}",
        )
    return panel, ch


def _make_panel_manager(panel_row, fake_panel):
    """Create a FakePanelManager wired to a specific FakePanel."""
    pm = FakePanelManager()
    pm.register(panel_row.id, fake_panel)
    return pm


# ── Join delay getter ────────────────────────────────────────────────────────


async def test_get_join_delay_default(db):
    assert await jr.get_join_delay(db, 10) == 10


async def test_get_join_delay_override(db):
    await store.set_setting(db, jr.JOIN_DELAY_KEY, "30")
    assert await jr.get_join_delay(db, 10) == 30


async def test_get_join_delay_channel_scoped(db):
    await store.set_setting(db, f"channel:5:{jr.JOIN_DELAY_KEY_SUFFIX}", "45")
    assert await jr.get_join_delay(db, 10, channel_db_id=5) == 45
    # Different channel falls back to default.
    assert await jr.get_join_delay(db, 10, channel_db_id=99) == 10


async def test_get_join_delay_invalid_override(db):
    await store.set_setting(db, jr.JOIN_DELAY_KEY, "abc")
    assert await jr.get_join_delay(db, 10) == 10


# ── Pause joins toggle ───────────────────────────────────────────────────────


async def test_joins_paused_default(db):
    assert await is_joins_paused(db) is False


async def test_set_joins_paused(db):
    await set_joins_paused(db, True)
    assert await is_joins_paused(db) is True
    await set_joins_paused(db, False)
    assert await is_joins_paused(db) is False


# ── on_join_request: wrong channel ignored ───────────────────────────────────


async def test_join_request_wrong_channel_ignored(db):
    event = FakeChatJoinRequest(chat_id=-999, user_id=42)
    bot = FakeBotWithDM()
    pm = FakePanelManager()

    await jr.on_join_request(event, bot=bot, db=db, panel_manager=pm)

    assert not event.approved
    assert len(bot.dms) == 0


# ── on_join_request: joins paused → approve immediately, no trial ────────────


async def test_join_request_joins_paused_approves_immediately(db):
    panel_row, ch = await _setup_channel(db)
    await set_joins_paused(db, True)
    event = FakeChatJoinRequest(chat_id=CHANNEL_TG_ID, user_id=42)
    bot = FakeBotWithDM()
    pm = _make_panel_manager(panel_row, FakePanel(groups=[(2, "NL")]))

    await jr.on_join_request(event, bot=bot, db=db, panel_manager=pm)

    assert event.approved
    assert len(bot.dms) == 0  # no DMs when paused


# ── on_join_request: eligible user → create trial + DM + approve ─────────────


async def test_join_request_creates_trial_and_approves(db):
    panel_row, ch = await _setup_channel(db, delay=5)
    fake_panel = FakePanel(groups=[(2, "NL")])
    pm = _make_panel_manager(panel_row, fake_panel)
    event = FakeChatJoinRequest(chat_id=CHANNEL_TG_ID, user_id=42, first_name="Ali")
    bot = FakeBotWithDM()

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await jr.on_join_request(event, bot=bot, db=db, panel_manager=pm)

    # Should have created a trial.
    assert len(fake_panel.create_calls) == 1
    assert fake_panel.create_calls[0].username.startswith("t42_")

    # Should have recorded the grant.
    grant = await store.get_latest_grant(db, 42)
    assert grant is not None
    assert grant.source == "join_request"
    assert grant.channel_id == ch.id

    # Should have DM'd the user (trial config + approval confirmation).
    assert len(bot.dms) == 2
    assert "sub_url" in bot.dms[0][1] or "panel.test" in bot.dms[0][1]
    assert "تأیید" in bot.dms[1][1]

    # Should have slept for the configured delay.
    mock_sleep.assert_awaited_once_with(5)

    # Should have approved.
    assert event.approved


async def test_join_request_delay_zero_approves_immediately(db):
    panel_row, ch = await _setup_channel(db, delay=0)
    fake_panel = FakePanel(groups=[(2, "NL")])
    pm = _make_panel_manager(panel_row, fake_panel)
    event = FakeChatJoinRequest(chat_id=CHANNEL_TG_ID, user_id=99, first_name="Sara")
    bot = FakeBotWithDM()

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await jr.on_join_request(event, bot=bot, db=db, panel_manager=pm)

    mock_sleep.assert_not_awaited()
    assert event.approved


# ── on_join_request: active grant → approve immediately ──────────────────────


async def test_join_request_active_grant_approves_and_resends_sub(db):
    panel_row, ch = await _setup_channel(db)
    # Record a grant on this channel's panel.
    await store.record_grant(
        db, tg_user_id=42, panel_username="t42_abc", source="start", channel_id=ch.id,
    )
    fake_panel = FakePanel(groups=[(2, "NL")])
    pm = _make_panel_manager(panel_row, fake_panel)
    event = FakeChatJoinRequest(chat_id=CHANNEL_TG_ID, user_id=42, first_name="Ali")
    bot = FakeBotWithDM()

    await jr.on_join_request(event, bot=bot, db=db, panel_manager=pm)

    assert event.approved
    assert len(bot.dms) == 1
    assert "panel.test" in bot.dms[0][1]


# ── on_join_request: cooldown → approve immediately ──────────────────────────


async def test_join_request_cooldown_approves_and_mentions_cooldown(db):
    panel_row, ch = await _setup_channel(db)
    await store.record_grant(
        db, tg_user_id=42, panel_username="t42_abc", source="start", channel_id=ch.id,
    )
    # Age the grant past its lifetime but within cooldown.
    old = (datetime.now(UTC) - timedelta(days=15)).isoformat()
    await db.execute("UPDATE trial_grants SET created_at = ? WHERE tg_user_id = 42", (old,))
    await db.commit()

    fake_panel = FakePanel(groups=[(2, "NL")])
    pm = _make_panel_manager(panel_row, fake_panel)
    event = FakeChatJoinRequest(chat_id=CHANNEL_TG_ID, user_id=42, first_name="Ali")
    bot = FakeBotWithDM()

    await jr.on_join_request(event, bot=bot, db=db, panel_manager=pm)

    assert event.approved
    assert len(bot.dms) == 1
    assert "روز" in bot.dms[0][1]  # mentions N days


# ── on_join_request: no offer groups → approve without trial ─────────────────


async def test_join_request_no_offers_approves_without_trial(db):
    # Create channel without offer groups.
    panel_row = await store.create_panel(
        db, name="P", base_url="https://p", admin_username="a", admin_password="b",
    )
    await store.create_channel(db, tg_channel_id=CHANNEL_TG_ID, title="Empty")

    event = FakeChatJoinRequest(chat_id=CHANNEL_TG_ID, user_id=42)
    bot = FakeBotWithDM()
    pm = _make_panel_manager(panel_row, FakePanel(groups=[(2, "NL")]))

    await jr.on_join_request(event, bot=bot, db=db, panel_manager=pm)

    assert event.approved
    # No DMs — no offer groups means no trial, just approve silently.
    assert len(bot.dms) == 0


# ── on_join_request: panel error → still approve ─────────────────────────────


async def test_join_request_panel_error_still_approves(db):
    from panel.exceptions import PanelTransportError

    panel_row, ch = await _setup_channel(db)
    fake_panel = FakePanel(
        groups=[(2, "NL")],
        create_side_effects=[PanelTransportError("panel down")],
    )
    pm = _make_panel_manager(panel_row, fake_panel)
    event = FakeChatJoinRequest(chat_id=CHANNEL_TG_ID, user_id=42)
    bot = FakeBotWithDM()

    await jr.on_join_request(event, bot=bot, db=db, panel_manager=pm)

    assert event.approved
    assert len(bot.dms) == 1
    assert "مشکلی" in bot.dms[0][1]


# ── on_join_request: join event is recorded ──────────────────────────────────


async def test_join_request_records_event(db):
    panel_row, ch = await _setup_channel(db)
    event = FakeChatJoinRequest(chat_id=CHANNEL_TG_ID, user_id=42)
    bot = FakeBotWithDM()
    pm = _make_panel_manager(panel_row, FakePanel(groups=[(2, "NL")]))

    await jr.on_join_request(event, bot=bot, db=db, panel_manager=pm)

    epoch = datetime.min.replace(tzinfo=UTC)
    count = await store.count_member_events(db, "join_request", epoch)
    assert count == 1


# ── Admin commands ───────────────────────────────────────────────────────────


async def test_cmd_pausejoins(db):
    panel_row, ch = await _setup_channel(db)
    await store.upsert_user(db, tg_user_id=1, role="superadmin")
    msg = FakeMessage(user_id=1)
    command = SimpleNamespace(args=str(ch.tg_channel_id))
    await cmd_pausejoins(message=msg, command=command, db=db, settings=SETTINGS)
    assert await is_channel_joins_paused(db, ch.id) is True


async def test_cmd_resumejoins(db):
    panel_row, ch = await _setup_channel(db)
    await set_channel_joins_paused(db, ch.id, True)
    await store.upsert_user(db, tg_user_id=1, role="superadmin")
    msg = FakeMessage(user_id=1)
    command = SimpleNamespace(args=str(ch.tg_channel_id))
    await cmd_resumejoins(message=msg, command=command, db=db, settings=SETTINGS)
    assert await is_channel_joins_paused(db, ch.id) is False


async def test_cmd_setjoindelay(db):
    panel_row, ch = await _setup_channel(db)
    await store.upsert_user(db, tg_user_id=1, role="superadmin")
    msg = FakeMessage(user_id=1)
    command = SimpleNamespace(args=f"{ch.tg_channel_id} 30")
    await cmd_setjoindelay(message=msg, command=command, db=db, settings=SETTINGS)
    updated = await store.get_channel(db, ch.id)
    assert updated.join_approval_delay_seconds == 30


async def test_cmd_setjoindelay_invalid(db):
    panel_row, ch = await _setup_channel(db)
    await store.upsert_user(db, tg_user_id=1, role="superadmin")
    msg = FakeMessage(user_id=1)
    command = SimpleNamespace(args=f"{ch.tg_channel_id} abc")
    await cmd_setjoindelay(message=msg, command=command, db=db, settings=SETTINGS)
    assert "کاربرد" in msg.texts[0]


async def test_cmd_setjoindelay_zero(db):
    panel_row, ch = await _setup_channel(db)
    await store.upsert_user(db, tg_user_id=1, role="superadmin")
    msg = FakeMessage(user_id=1)
    command = SimpleNamespace(args=f"{ch.tg_channel_id} 0")
    await cmd_setjoindelay(message=msg, command=command, db=db, settings=SETTINGS)
    updated = await store.get_channel(db, ch.id)
    assert updated.join_approval_delay_seconds == 0


async def test_cmd_joinstats(db):
    panel_row, ch = await _setup_channel(db)
    await store.upsert_user(db, tg_user_id=1, role="superadmin")
    await store.record_grant(db, tg_user_id=10, panel_username="t10_a", source="start")
    await store.record_grant(db, tg_user_id=20, panel_username="t20_a", source="join_request")
    await store.record_member_event(db, CHANNEL_TG_ID, 30, "join_request")

    msg = FakeMessage(user_id=1)
    command = SimpleNamespace(args=str(ch.tg_channel_id))
    await cmd_joinstats(message=msg, command=command, db=db, settings=SETTINGS)
    assert "آمار درخواست عضویت" in msg.texts[0]


# ── DB migration ─────────────────────────────────────────────────────────────


async def test_migration_adds_source_column(tmp_path):
    import aiosqlite

    db_path = tmp_path / "old.db"
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.executescript("""
        CREATE TABLE trial_grants (
            tg_user_id     INTEGER PRIMARY KEY,
            tg_username    TEXT,
            panel_username TEXT NOT NULL,
            panel_user_id  INTEGER,
            group_ids      TEXT,
            data_limit     INTEGER,
            expire_at      TEXT,
            created_at     TEXT NOT NULL,
            source_chat_id INTEGER,
            revoked        INTEGER NOT NULL DEFAULT 0
        );
    """)
    await db.commit()
    await db.close()

    db = await store.connect(db_path)
    try:
        await store.record_grant(db, tg_user_id=1, panel_username="t1_x", source="join_request")
        grant = await store.get_latest_grant(db, 1)
        assert grant is not None
        assert grant.source == "join_request"
        assert grant.channel_id is None  # legacy grants have no channel
    finally:
        await db.close()
