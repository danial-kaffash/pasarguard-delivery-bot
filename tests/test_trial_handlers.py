"""Handler-level tests for the /start flow — multi-tenant version."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bot.handlers import trial as trial_handlers
from storage import db as store
from tests.helpers import (
    FakeMessage,
    FakePanel,
    FakePanelManager,
    FakeState,
    make_settings,
)

SETTINGS = make_settings()
CHANNEL_TG_ID = -1001234567890


@pytest.fixture
async def db(tmp_path):
    conn = await store.connect(tmp_path / "test.db")
    try:
        yield conn
    finally:
        await conn.close()


async def _setup_channel(db, *, panel_groups=(2,)):
    """Create a panel, channel, and offer groups for testing."""
    panel = await store.create_panel(
        db,
        name="TestPanel",
        base_url="https://panel.test",
        admin_username="admin",
        admin_password="pw",
    )
    ch = await store.create_channel(
        db,
        tg_channel_id=CHANNEL_TG_ID,
        title="Test",
    )
    for gid in panel_groups:
        await store.upsert_channel_offer_group(
            db,
            channel_id=ch.id,
            panel_id=panel.id,
            group_id=gid,
            label=f"group-{gid}",
        )
    return panel, ch


def _make_panel_manager(panel_row, fake_panel):
    pm = FakePanelManager()
    pm.register(panel_row.id, fake_panel)
    return pm


async def test_start_escapes_user_name(db):
    panel_row, ch = await _setup_channel(db)
    fake_panel = FakePanel(groups=[(2, "NL")])
    pm = _make_panel_manager(panel_row, fake_panel)

    msg = FakeMessage(user_id=7)
    msg.from_user.first_name = "<b>bad</b> & 'name'"
    msg.text = "/start join"  # plain start → auto-resolves single channel
    state = FakeState()

    await trial_handlers.on_start(
        msg,
        state=state,
        bot=None,
        db=db,
        panel_manager=pm,
    )

    reply = msg.texts[0]
    assert "&lt;b&gt;bad&lt;/b&gt;" in reply
    assert "<b>bad</b>" not in reply  # raw markup must never reach the message
    assert state.state is not None  # entered the selection flow


async def test_start_no_offers_pauses_trials(db):
    """A channel with no offer groups shows 'not available'."""
    panel_row = await store.create_panel(
        db,
        name="P",
        base_url="https://p",
        admin_username="a",
        admin_password="b",
    )
    await store.create_channel(db, tg_channel_id=CHANNEL_TG_ID, title="Empty")
    pm = _make_panel_manager(panel_row, FakePanel(groups=[(2, "NL")]))
    msg = FakeMessage(user_id=7)
    msg.text = "/start join"

    await trial_handlers.on_start(
        msg,
        state=FakeState(),
        bot=None,
        db=db,
        panel_manager=pm,
    )
    assert "موجود نیست" in msg.texts[0]


async def test_start_active_grant_resends_subscription_url(db):
    panel_row, ch = await _setup_channel(db)
    await store.record_grant(
        db,
        tg_user_id=7,
        panel_username="t7_x",
        channel_id=ch.id,
    )
    fake_panel = FakePanel(groups=[(2, "NL")])
    pm = _make_panel_manager(panel_row, fake_panel)
    msg = FakeMessage(user_id=7)
    msg.text = "/start join"

    await trial_handlers.on_start(
        msg,
        state=FakeState(),
        bot=None,
        db=db,
        panel_manager=pm,
    )

    assert "https://panel.test/sub/abc/" in msg.texts[0]


async def test_start_cooldown_message(db):
    panel_row, ch = await _setup_channel(db)
    await store.record_grant(
        db,
        tg_user_id=7,
        panel_username="t7_x",
        channel_id=ch.id,
    )
    # Age the grant past its lifetime (10 days) but inside the 30-day cooldown.
    old = (datetime.now(UTC) - timedelta(days=15)).isoformat()
    await db.execute("UPDATE trial_grants SET created_at = ? WHERE tg_user_id = 7", (old,))
    await db.commit()

    fake_panel = FakePanel(groups=[(2, "NL")])
    pm = _make_panel_manager(panel_row, fake_panel)
    msg = FakeMessage(user_id=7)
    msg.text = "/start join"

    await trial_handlers.on_start(
        msg,
        state=FakeState(),
        bot=None,
        db=db,
        panel_manager=pm,
    )
    assert "روز" in msg.texts[0]  # "N days until a new test"


async def test_start_ignores_non_private_chats(db):
    msg = FakeMessage(user_id=7)
    msg.chat.type = "supergroup"
    msg.text = "/start"

    await trial_handlers.on_start(
        msg,
        state=FakeState(),
        bot=None,
        db=db,
        panel_manager=FakePanelManager(),
    )
    assert "پی‌وی" in msg.texts[0]


async def test_start_no_channel_shows_error(db):
    """When there are no channels configured, shows a friendly error."""
    msg = FakeMessage(user_id=7)
    msg.text = "/start"

    await trial_handlers.on_start(
        msg,
        state=FakeState(),
        bot=None,
        db=db,
        panel_manager=FakePanelManager(),
    )
    assert "موجود نیست" in msg.texts[0] or "لینک" in msg.texts[0]
