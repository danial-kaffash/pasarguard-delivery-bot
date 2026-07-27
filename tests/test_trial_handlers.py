"""Handler-level tests for the /start flow (bot.handlers.trial)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bot.handlers import trial as trial_handlers
from storage import db as store
from tests.helpers import FakeMessage, FakePanel, FakeState, make_settings

SETTINGS = make_settings()


@pytest.fixture
async def db(tmp_path):
    conn = await store.connect(tmp_path / "test.db")
    try:
        yield conn
    finally:
        await conn.close()


async def _seed_offers(db, ids=(2,)):
    for gid in ids:
        await store.upsert_offer_group(db, gid, f"group-{gid}")


async def test_start_escapes_user_name(db):
    await _seed_offers(db)
    msg = FakeMessage(user_id=7)
    msg.from_user.first_name = "<b>bad</b> & 'name'"
    state = FakeState()

    await trial_handlers.on_start(
        msg, state=state, bot=None, db=db, settings=SETTINGS, panel=FakePanel(groups=[(2, "NL")])
    )

    reply = msg.texts[0]
    assert "&lt;b&gt;bad&lt;/b&gt;" in reply
    assert "<b>bad</b>" not in reply  # raw markup must never reach the message
    assert state.state is not None  # entered the selection flow


async def test_start_no_offers_pauses_trials(db):
    msg = FakeMessage(user_id=7)
    await trial_handlers.on_start(
        msg,
        state=FakeState(),
        bot=None,
        db=db,
        settings=SETTINGS,
        panel=FakePanel(groups=[(2, "NL")]),
    )
    assert "موجود نیست" in msg.texts[0]


async def test_start_active_grant_resends_subscription_url(db):
    await store.record_grant(db, tg_user_id=7, panel_username="t7_x")
    msg = FakeMessage(user_id=7)

    await trial_handlers.on_start(
        msg, state=FakeState(), bot=None, db=db, settings=SETTINGS, panel=FakePanel()
    )

    assert "https://panel.test/sub/abc/" in msg.texts[0]


async def test_start_cooldown_message(db):
    await store.record_grant(db, tg_user_id=7, panel_username="t7_x")
    # age the grant past its lifetime (10 days) but inside the 30-day cooldown
    old = (datetime.now(UTC) - timedelta(days=15)).isoformat()
    await db.execute("UPDATE trial_grants SET created_at = ? WHERE tg_user_id = 7", (old,))
    await db.commit()

    msg = FakeMessage(user_id=7)
    await trial_handlers.on_start(
        msg, state=FakeState(), bot=None, db=db, settings=SETTINGS, panel=FakePanel()
    )
    assert "روز" in msg.texts[0]  # "N days until a new test"


async def test_start_ignores_non_private_chats(db):
    msg = FakeMessage(user_id=7)
    msg.chat.type = "supergroup"
    await trial_handlers.on_start(
        msg, state=FakeState(), bot=None, db=db, settings=SETTINGS, panel=FakePanel()
    )
    assert "پی‌وی" in msg.texts[0]
