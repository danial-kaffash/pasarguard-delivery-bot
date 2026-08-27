"""Tests for the trial selection flow: toggle/cancel/confirm callbacks and /start edge paths."""

from __future__ import annotations

import pytest

from bot import texts
from bot.handlers import trial as trial_handlers
from bot.keyboards import GroupCB
from panel.exceptions import PanelError
from storage import db as store
from tests.helpers import (
    FakeCallback,
    FakeMessage,
    FakePanel,
    FakePanelManager,
    FakeState,
    make_panel_user,
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


async def _setup(db, *, groups=(2,)):
    panel_row = await store.create_panel(
        db,
        name="P",
        base_url="https://p",
        admin_username="a",
        admin_password="b",
    )
    ch = await store.create_channel(db, tg_channel_id=CHANNEL_TG_ID, title="Test")
    for gid in groups:
        await store.upsert_channel_offer_group(
            db,
            channel_id=ch.id,
            panel_id=panel_row.id,
            group_id=gid,
            label=f"group-{gid}",
        )
    return panel_row, ch


def _pm(panel_row, fake_panel):
    pm = FakePanelManager()
    pm.register(panel_row.id, fake_panel)
    return pm


# ── toggle_group ─────────────────────────────────────────────────────────────


async def test_toggle_adds_and_removes_selection(db):
    panel_row, ch = await _setup(db)
    pm = _pm(panel_row, FakePanel(groups=[(2, "NL")]))
    state = FakeState()
    state.data = {"selected": [], "channel_id": ch.id}

    cb = FakeCallback()
    await trial_handlers.toggle_group(
        cb,
        state=state,
        db=db,
        panel_manager=pm,
        callback_data=GroupCB(action="toggle", gid=2),
    )
    assert state.data["selected"] == [2]
    assert "reply_markup" in cb.message.markup_edits[0]  # keyboard re-rendered
    assert cb.answers  # acked

    await trial_handlers.toggle_group(
        cb,
        state=state,
        db=db,
        panel_manager=pm,
        callback_data=GroupCB(action="toggle", gid=2),
    )
    assert state.data["selected"] == []  # toggled off


async def test_toggle_without_channel_renders_empty_keyboard(db):
    panel_row, ch = await _setup(db)
    pm = _pm(panel_row, FakePanel(groups=[(2, "NL")]))
    state = FakeState()  # no channel_id in state (legacy)
    cb = FakeCallback()
    await trial_handlers.toggle_group(
        cb,
        state=state,
        db=db,
        panel_manager=pm,
        callback_data=GroupCB(action="toggle", gid=2),
    )
    assert state.data["selected"] == [2]
    kb = cb.message.markup_edits[0]["reply_markup"]
    assert len(kb.inline_keyboard) == 1  # only confirm/cancel — no group buttons


# ── cancel_selection ─────────────────────────────────────────────────────────


async def test_cancel_clears_state_and_edits_message(db):
    state = FakeState()
    state.data = {"selected": [2], "channel_id": 1}
    cb = FakeCallback()
    await trial_handlers.cancel_selection(cb, state=state)
    assert state.data == {} and state.state is None
    assert cb.message.edits[0][0] == texts.CANCELLED
    assert cb.answers


# ── confirm_selection ────────────────────────────────────────────────────────


async def test_confirm_empty_selection_shows_hint(db):
    state = FakeState()
    state.data = {"selected": [], "channel_id": 1}
    cb = FakeCallback()
    await trial_handlers.confirm_selection(
        cb,
        state=state,
        bot=None,
        db=db,
        panel_manager=FakePanelManager(),
    )
    assert cb.answers[0][0] == texts.SELECT_HINT
    assert cb.answers[0][1].get("show_alert") is True
    assert state.data  # state NOT cleared — user can still pick


async def test_confirm_while_paused_cancels_flow(db):
    _, ch = await _setup(db)
    await store.set_setting(db, "paused", "true")
    state = FakeState()
    state.data = {"selected": [2], "channel_id": ch.id}
    cb = FakeCallback()
    await trial_handlers.confirm_selection(
        cb,
        state=state,
        bot=None,
        db=db,
        panel_manager=FakePanelManager(),
    )
    assert cb.message.edits[0][0] == texts.PAUSED
    assert state.data == {}


async def test_confirm_with_missing_channel(db):
    state = FakeState()
    state.data = {"selected": [2], "channel_id": 999}
    cb = FakeCallback()
    await trial_handlers.confirm_selection(
        cb,
        state=state,
        bot=None,
        db=db,
        panel_manager=FakePanelManager(),
    )
    assert cb.message.edits[0][0] == texts.ERROR_TRY_AGAIN


async def test_confirm_with_stale_group_selection(db):
    # Offer group 2 exists, but user's selection references group 77.
    _, ch = await _setup(db, groups=(2,))
    state = FakeState()
    state.data = {"selected": [77], "channel_id": ch.id}
    cb = FakeCallback()
    await trial_handlers.confirm_selection(
        cb,
        state=state,
        bot=None,
        db=db,
        panel_manager=FakePanelManager(),
    )
    assert cb.message.edits[0][0] == texts.ERROR_TRY_AGAIN


async def test_confirm_with_deleted_panel_row(db):
    panel_row, ch = await _setup(db, groups=(2,))
    await db.execute("DELETE FROM panels WHERE id = ?", (panel_row.id,))
    await db.commit()
    state = FakeState()
    state.data = {"selected": [2], "channel_id": ch.id}
    cb = FakeCallback()
    await trial_handlers.confirm_selection(
        cb,
        state=state,
        bot=None,
        db=db,
        panel_manager=FakePanelManager(),
    )
    assert cb.message.edits[0][0] == texts.ERROR_TRY_AGAIN


async def test_confirm_ineligible_user_gets_friendly_error(db):
    panel_row, ch = await _setup(db)
    await store.record_grant(db, tg_user_id=1, panel_username="t1_x", channel_id=ch.id)
    pm = _pm(panel_row, FakePanel(groups=[(2, "NL")]))
    state = FakeState()
    state.data = {"selected": [2], "channel_id": ch.id}
    cb = FakeCallback(user_id=1)
    await trial_handlers.confirm_selection(
        cb,
        state=state,
        bot=None,
        db=db,
        panel_manager=pm,
    )
    # Same "already granted" message as /start.
    assert texts.ALREADY_GRANTED_NO_URL.split("{")[0] in cb.message.edits[0][0]
    assert state.data == {}


async def test_confirm_panel_error_keeps_grace(db):
    panel_row, ch = await _setup(db)
    pm = _pm(panel_row, FakePanel(groups=[(2, "NL")], create_side_effects=[PanelError("boom")]))
    state = FakeState()
    state.data = {"selected": [2], "channel_id": ch.id}
    cb = FakeCallback(user_id=7)
    await trial_handlers.confirm_selection(
        cb,
        state=state,
        bot=None,
        db=db,
        panel_manager=pm,
    )
    assert cb.message.answers[0][0] == texts.ERROR_TRY_AGAIN
    assert state.data == {}
    assert await store.get_latest_grant(db, 7) is None  # nothing recorded


async def test_confirm_success_delivers_subscription_url(db):
    panel_row, ch = await _setup(db, groups=(2, 5))
    pm = _pm(panel_row, FakePanel(groups=[(2, "NL"), (5, "TR")]))
    state = FakeState()
    state.data = {"selected": [2, 5], "channel_id": ch.id}
    cb = FakeCallback(user_id=7)

    await trial_handlers.confirm_selection(
        cb,
        state=state,
        bot=None,
        db=db,
        panel_manager=pm,
    )

    # "Creating…" edit, then the delivery message.
    assert cb.message.edits[0][0] == texts.CREATING
    delivery = cb.message.answers[0][0]
    assert "https://panel.test/sub/abc/" in delivery
    assert "group-2" in delivery and "group-5" in delivery  # labels listed

    grant = await store.get_latest_grant(db, 7)
    assert grant is not None
    assert grant.panel_user_id == 101
    assert state.data == {}  # flow finished


async def test_confirm_success_without_sub_url_explains_panel_username(db):
    panel_row, ch = await _setup(db)
    pm = _pm(panel_row, FakePanel(create_side_effects=[make_panel_user("ignored", sub_url="")]))
    state = FakeState()
    state.data = {"selected": [2], "channel_id": ch.id}
    cb = FakeCallback(user_id=7)
    await trial_handlers.confirm_selection(
        cb,
        state=state,
        bot=None,
        db=db,
        panel_manager=pm,
    )
    # Panel returned no sub URL — the message explains via the generated panel username.
    text = cb.message.answers[0][0]
    assert "t7_" in text  # generated username for tg user 7
    grant = await store.get_latest_grant(db, 7)
    assert grant is not None  # grant still recorded


# ── /start edge paths ────────────────────────────────────────────────────────


async def test_start_while_globally_paused(db):
    _, ch = await _setup(db)
    await store.set_setting(db, "paused", "true")
    msg = FakeMessage(user_id=7)
    msg.text = "/start join"
    await trial_handlers.on_start(
        msg,
        state=FakeState(),
        bot=None,
        db=db,
        panel_manager=FakePanelManager(),
    )
    assert msg.texts[0] == texts.PAUSED


async def test_start_multiple_channels_requires_deep_link(db):
    panel_row = await store.create_panel(
        db,
        name="P",
        base_url="https://p",
        admin_username="a",
        admin_password="b",
    )
    ch1 = await store.create_channel(db, tg_channel_id=-100111, title="A")
    ch2 = await store.create_channel(db, tg_channel_id=-100222, title="B")
    await store.upsert_channel_offer_group(
        db, channel_id=ch1.id, panel_id=panel_row.id, group_id=2, label="g2"
    )
    await store.upsert_channel_offer_group(
        db, channel_id=ch2.id, panel_id=panel_row.id, group_id=3, label="g3"
    )

    msg = FakeMessage(user_id=7)
    msg.text = "/start join"  # ambiguous: two channels exist
    await trial_handlers.on_start(
        msg,
        state=FakeState(),
        bot=None,
        db=db,
        panel_manager=FakePanelManager(),
    )
    assert "از لینک کانال" in msg.texts[0]


async def test_start_deep_link_resolves_specific_channel(db):
    panel_row = await store.create_panel(
        db,
        name="P",
        base_url="https://p",
        admin_username="a",
        admin_password="b",
    )
    ch1 = await store.create_channel(db, tg_channel_id=-100111, title="A")
    ch2 = await store.create_channel(db, tg_channel_id=-100222, title="B")
    await store.upsert_channel_offer_group(
        db, channel_id=ch1.id, panel_id=panel_row.id, group_id=2, label="g2"
    )
    await store.upsert_channel_offer_group(
        db, channel_id=ch2.id, panel_id=panel_row.id, group_id=3, label="g3"
    )

    state = FakeState()
    msg = FakeMessage(user_id=7)
    msg.text = "/start join_-100222"  # deep-links channel B explicitly
    pm = _pm(panel_row, FakePanel(groups=[(2, "NL"), (3, "TR")]))
    await trial_handlers.on_start(msg, state=state, bot=None, db=db, panel_manager=pm)
    assert state.state is not None  # entered the selection flow for B
    assert state.data["channel_id"] == ch2.id


async def test_start_deep_link_with_garbage_id_falls_back(db):
    panel_row, ch = await _setup(db)
    state = FakeState()
    msg = FakeMessage(user_id=7)
    msg.text = "/start join_not_a_number"  # unparseable id → single-channel fallback
    pm = _pm(panel_row, FakePanel(groups=[(2, "NL")]))
    await trial_handlers.on_start(msg, state=state, bot=None, db=db, panel_manager=pm)
    assert state.state is not None
    assert state.data["channel_id"] == ch.id


async def test_start_new_member_gate_blocks_old_members(db):
    panel_row, ch = await _setup(db)
    await store.update_channel(db, ch.id, trial_max_member_age_days=30)
    pm = _pm(panel_row, FakePanel(groups=[(2, "NL")]))

    msg = FakeMessage(user_id=7)
    msg.text = "/start join"
    state = FakeState()
    await trial_handlers.on_start(msg, state=state, bot=None, db=db, panel_manager=pm)
    # User 7 never joined the channel (no first_join_at) → gate is active → rejected.
    assert "عضو" in msg.texts[0]
    assert state.state is None  # never entered the selection flow
