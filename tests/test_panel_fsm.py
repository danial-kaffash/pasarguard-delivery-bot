"""Tests for the inline management panel FSM text input flows."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.handlers.panel import (
    PanelCB,
    PanelInput,
    input_data_limit,
    input_grace_days,
    input_join_delay,
    input_max_age,
    input_promo_int,
    input_promo_text,
    input_regrant_days,
    input_reset_user,
    input_trial_days,
    on_edit,
)
from storage import db as store
from tests.helpers import FakeMessage, make_settings

SETTINGS = make_settings()


@pytest.fixture
async def db(tmp_path):
    conn = await store.connect(tmp_path / "test.db")
    try:
        yield conn
    finally:
        await conn.close()


async def _setup(db):
    panel = await store.create_panel(
        db,
        name="P",
        base_url="https://p",
        admin_username="a",
        admin_password="b",
    )
    ch = await store.create_channel(db, tg_channel_id=-100123, title="Test")
    await store.upsert_user(db, tg_user_id=1, role="superadmin")
    return panel, ch


class FakeFSMContext:
    def __init__(self):
        self.state = None
        self.data = {}

    async def set_state(self, state):
        self.state = state

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def get_data(self):
        return self.data

    async def clear(self):
        self.state = None
        self.data = {}


# ── Edit action enters FSM state ─────────────────────────────────────────────


async def test_edit_promo_text_enters_fsm():
    callback = SimpleNamespace(
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    state = FakeFSMContext()
    cb_data = PanelCB(action="edit", target="promo_text", tid=42)
    await on_edit(callback, callback_data=cb_data, state=state)
    assert state.state == PanelInput.waiting_promo_text
    assert state.data["channel_id"] == 42


async def test_edit_trial_dl_enters_fsm():
    callback = SimpleNamespace(
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    state = FakeFSMContext()
    cb_data = PanelCB(action="edit", target="trial_dl", tid=42)
    await on_edit(callback, callback_data=cb_data, state=state)
    assert state.state == PanelInput.waiting_data_limit


# ── Promo text input ─────────────────────────────────────────────────────────


async def test_input_promo_text_saves(db):
    _, ch = await _setup(db)
    msg = FakeMessage(user_id=1)
    msg.text = "🎁 متن جدید"
    state = FakeFSMContext()
    state.data = {"channel_id": ch.id}

    await input_promo_text(msg, state=state, db=db)
    key = f"channel:{ch.id}:promo_text"
    assert await store.get_setting(db, key) == "🎁 متن جدید"
    assert state.state is None  # cleared
    assert "✅" in msg.texts[0]


# ── Promo interval input ─────────────────────────────────────────────────────


async def test_input_promo_int_valid(db):
    _, ch = await _setup(db)
    msg = FakeMessage(user_id=1)
    msg.text = "12"
    state = FakeFSMContext()
    state.data = {"channel_id": ch.id}

    await input_promo_int(msg, state=state, db=db)
    updated = await store.get_channel(db, ch.id)
    assert updated.promo_interval_hours == 12.0


async def test_input_promo_int_invalid(db):
    _, ch = await _setup(db)
    msg = FakeMessage(user_id=1)
    msg.text = "abc"
    state = FakeFSMContext()
    state.state = PanelInput.waiting_promo_int
    state.data = {"channel_id": ch.id}

    await input_promo_int(msg, state=state, db=db)
    assert "❌" in msg.texts[0]
    # State is NOT cleared on error — user can retry.
    assert state.state == PanelInput.waiting_promo_int


# ── Data limit input ─────────────────────────────────────────────────────────


async def test_input_data_limit_valid(db):
    _, ch = await _setup(db)
    msg = FakeMessage(user_id=1)
    msg.text = "20"
    state = FakeFSMContext()
    state.data = {"channel_id": ch.id}

    await input_data_limit(msg, state=state, db=db)
    updated = await store.get_channel(db, ch.id)
    assert updated.trial_data_limit_gb == 20.0


async def test_input_data_limit_invalid(db):
    _, ch = await _setup(db)
    msg = FakeMessage(user_id=1)
    msg.text = "-5"
    state = FakeFSMContext()
    state.data = {"channel_id": ch.id}

    await input_data_limit(msg, state=state, db=db)
    assert "❌" in msg.texts[0]


# ── Trial days input ─────────────────────────────────────────────────────────


async def test_input_trial_days_valid(db):
    _, ch = await _setup(db)
    msg = FakeMessage(user_id=1)
    msg.text = "14"
    state = FakeFSMContext()
    state.data = {"channel_id": ch.id}

    await input_trial_days(msg, state=state, db=db)
    updated = await store.get_channel(db, ch.id)
    assert updated.trial_days == 14


async def test_input_trial_days_invalid(db):
    _, ch = await _setup(db)
    msg = FakeMessage(user_id=1)
    msg.text = "abc"
    state = FakeFSMContext()
    state.data = {"channel_id": ch.id}

    await input_trial_days(msg, state=state, db=db)
    assert "❌" in msg.texts[0]


# ── Grace days input ─────────────────────────────────────────────────────────


async def test_input_grace_days_valid(db):
    _, ch = await _setup(db)
    msg = FakeMessage(user_id=1)
    msg.text = "14"
    state = FakeFSMContext()
    state.data = {"channel_id": ch.id}

    await input_grace_days(msg, state=state, db=db)
    updated = await store.get_channel(db, ch.id)
    assert updated.on_hold_grace_days == 14


# ── Regrant days input ───────────────────────────────────────────────────────


async def test_input_regrant_days_valid(db):
    _, ch = await _setup(db)
    msg = FakeMessage(user_id=1)
    msg.text = "60"
    state = FakeFSMContext()
    state.data = {"channel_id": ch.id}

    await input_regrant_days(msg, state=state, db=db)
    updated = await store.get_channel(db, ch.id)
    assert updated.allow_regrant_after_days == 60


# ── Max age input ────────────────────────────────────────────────────────────


async def test_input_max_age_valid(db):
    _, ch = await _setup(db)
    msg = FakeMessage(user_id=1)
    msg.text = "7"
    state = FakeFSMContext()
    state.data = {"channel_id": ch.id}

    await input_max_age(msg, state=state, db=db)
    updated = await store.get_channel(db, ch.id)
    assert updated.trial_max_member_age_days == 7.0


async def test_input_max_age_zero(db):
    _, ch = await _setup(db)
    msg = FakeMessage(user_id=1)
    msg.text = "0"
    state = FakeFSMContext()
    state.data = {"channel_id": ch.id}

    await input_max_age(msg, state=state, db=db)
    updated = await store.get_channel(db, ch.id)
    assert updated.trial_max_member_age_days == 0.0


# ── Join delay input ─────────────────────────────────────────────────────────


async def test_input_join_delay_valid(db):
    _, ch = await _setup(db)
    msg = FakeMessage(user_id=1)
    msg.text = "30"
    state = FakeFSMContext()
    state.data = {"channel_id": ch.id}

    await input_join_delay(msg, state=state, db=db)
    updated = await store.get_channel(db, ch.id)
    assert updated.join_approval_delay_seconds == 30


async def test_input_join_delay_zero(db):
    _, ch = await _setup(db)
    msg = FakeMessage(user_id=1)
    msg.text = "0"
    state = FakeFSMContext()
    state.data = {"channel_id": ch.id}

    await input_join_delay(msg, state=state, db=db)
    updated = await store.get_channel(db, ch.id)
    assert updated.join_approval_delay_seconds == 0


# ── Reset user input ─────────────────────────────────────────────────────────


async def test_input_reset_user_valid(db):
    _, ch = await _setup(db)
    await store.record_grant(db, tg_user_id=42, panel_username="t42", channel_id=ch.id)
    msg = FakeMessage(user_id=1)
    msg.text = "42"
    state = FakeFSMContext()
    state.data = {"channel_id": ch.id}

    await input_reset_user(msg, state=state, db=db)
    grant = await store.get_latest_grant(db, 42)
    assert grant.revoked is True


async def test_input_reset_user_not_found(db):
    _, ch = await _setup(db)
    msg = FakeMessage(user_id=1)
    msg.text = "999"
    state = FakeFSMContext()
    state.data = {"channel_id": ch.id}

    await input_reset_user(msg, state=state, db=db)
    assert "یافت نشد" in msg.texts[0]
