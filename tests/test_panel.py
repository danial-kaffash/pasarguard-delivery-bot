"""Tests for the inline management panel (bot/handlers/panel.py)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.handlers import panel
from bot.handlers.panel import PanelCB, PanelInput, cmd_panel, on_view, on_toggle
from bot.pause import is_channel_joins_paused, is_channel_paused, set_channel_paused
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


async def _setup(db, *, tg_id=-1001234567890, superadmin=True):
    panel_row = await store.create_panel(
        db, name="P", base_url="https://p", admin_username="a", admin_password="b",
    )
    ch = await store.create_channel(db, tg_channel_id=tg_id, title="Test")
    if superadmin:
        await store.upsert_user(db, tg_user_id=1, role="superadmin")
    return panel_row, ch


# ── Callback data encoding ───────────────────────────────────────────────────


def test_panel_cb_roundtrip():
    cb = PanelCB(action="view", target="ch", tid=42, extra="foo")
    packed = cb.pack()
    unpacked = PanelCB.unpack(packed)
    assert unpacked.action == "view"
    assert unpacked.target == "ch"
    assert unpacked.tid == 42
    assert unpacked.extra == "foo"


def test_panel_cb_defaults():
    cb = PanelCB(action="back", target="main")
    assert cb.tid == 0
    assert cb.extra == ""


# ── Keyboard builders ────────────────────────────────────────────────────────


def test_build_channel_list_keyboard():
    ch1 = SimpleNamespace(id=1, title="Channel A", tg_channel_id=-1)
    ch2 = SimpleNamespace(id=2, title="Channel B", tg_channel_id=-2)
    kb = panel.build_channel_list_keyboard([ch1, ch2])
    assert len(kb.inline_keyboard) == 2
    assert "Channel A" in kb.inline_keyboard[0][0].text
    assert "Channel B" in kb.inline_keyboard[1][0].text


def test_build_channel_menu():
    ch = SimpleNamespace(
        id=1, tg_channel_id=-1, promo_pin=True, promo_silent=False,
        title="Test", trial_data_limit_gb=5.0, trial_days=3,
        on_hold_grace_days=7, allow_regrant_after_days=30,
        trial_max_member_age_days=0, join_approval_delay_seconds=10,
        promo_interval_hours=6.0,
    )
    kb = panel.build_channel_menu(ch, paused=False, joins_paused=False)
    # Should have pause, promo/trial row, join/offer row, stats
    assert len(kb.inline_keyboard) >= 4
    # First button should be pause (not resumed).
    assert "توقف" in kb.inline_keyboard[0][0].text


def test_build_channel_menu_paused():
    ch = SimpleNamespace(id=1, tg_channel_id=-1, promo_pin=True, promo_silent=True,
                         title="", trial_data_limit_gb=5.0, trial_days=3,
                         on_hold_grace_days=7, allow_regrant_after_days=30,
                         trial_max_member_age_days=0, join_approval_delay_seconds=10,
                         promo_interval_hours=6.0)
    kb = panel.build_channel_menu(ch, paused=True, joins_paused=True)
    assert "فعال" in kb.inline_keyboard[0][0].text  # "Enable" when paused


def test_build_promo_menu():
    ch = SimpleNamespace(
        id=1, tg_channel_id=-1, promo_pin=True, promo_silent=False,
        promo_interval_hours=6.0,
    )
    kb = panel.build_promo_menu(ch)
    assert any("Pin" in btn.text for row in kb.inline_keyboard for btn in row)
    assert any("Silent" in btn.text for row in kb.inline_keyboard for btn in row)


def test_build_trial_menu():
    ch = SimpleNamespace(
        id=1, trial_data_limit_gb=5.0, trial_days=3,
        on_hold_grace_days=7, allow_regrant_after_days=30,
        trial_max_member_age_days=0.0,
    )
    kb = panel.build_trial_menu(ch)
    texts = [btn.text for row in kb.inline_keyboard for btn in row]
    assert any("5" in t for t in texts)  # data limit
    assert any("3" in t for t in texts)  # days


def test_build_offer_menu_empty():
    ch = SimpleNamespace(id=1)
    kb = panel.build_offer_menu(ch, [])
    assert len(kb.inline_keyboard) >= 2  # add + back


def test_build_offer_menu_with_groups():
    ch = SimpleNamespace(id=1)
    offers = [
        SimpleNamespace(channel_id=1, panel_id=1, group_id=2, label="NL", sort_order=0),
        SimpleNamespace(channel_id=1, panel_id=1, group_id=5, label="TR", sort_order=1),
    ]
    kb = panel.build_offer_menu(ch, offers)
    texts = [btn.text for row in kb.inline_keyboard for btn in row]
    assert any("NL" in t for t in texts)
    assert any("TR" in t for t in texts)
    assert any("حذف همه" in t for t in texts)


# ── /panel entry point ───────────────────────────────────────────────────────


async def test_cmd_panel_shows_channels(db):
    _, ch = await _setup(db)
    msg = FakeMessage(user_id=1)
    msg.chat = SimpleNamespace(id=1, type="private")
    await cmd_panel(msg, db=db, settings=SETTINGS)
    assert len(msg.replies) >= 1
    text = msg.replies[0][0]
    assert "کانال" in text


async def test_cmd_panel_no_channels(db):
    await store.upsert_user(db, tg_user_id=42, role="admin")
    msg = FakeMessage(user_id=42)
    msg.chat = SimpleNamespace(id=42, type="private")
    await cmd_panel(msg, db=db, settings=SETTINGS)
    assert "ندارید" in msg.texts[0]


# ── Toggle actions ───────────────────────────────────────────────────────────


async def test_toggle_pause(db):
    _, ch = await _setup(db)
    callback = SimpleNamespace(
        message=SimpleNamespace(edit_text=AsyncMock()),
        from_user=SimpleNamespace(id=1),
        answer=AsyncMock(),
        data=PanelCB(action="toggle", target="pause", tid=ch.id).pack(),
    )
    await on_toggle(
        callback,
        callback_data=PanelCB(action="toggle", target="pause", tid=ch.id),
        db=db,
    )
    assert await is_channel_paused(db, ch.id) is True
    callback.message.edit_text.assert_awaited()


async def test_toggle_promo_pin(db):
    _, ch = await _setup(db)
    assert ch.promo_pin is True
    callback = SimpleNamespace(
        message=SimpleNamespace(edit_text=AsyncMock()),
        from_user=SimpleNamespace(id=1),
        answer=AsyncMock(),
    )
    await on_toggle(
        callback,
        callback_data=PanelCB(action="toggle", target="promo_pin", tid=ch.id),
        db=db,
    )
    updated = await store.get_channel(db, ch.id)
    assert updated.promo_pin is False


async def test_toggle_join_pause(db):
    _, ch = await _setup(db)
    callback = SimpleNamespace(
        message=SimpleNamespace(edit_text=AsyncMock()),
        from_user=SimpleNamespace(id=1),
        answer=AsyncMock(),
    )
    await on_toggle(
        callback,
        callback_data=PanelCB(action="toggle", target="join_pause", tid=ch.id),
        db=db,
    )
    assert await is_channel_joins_paused(db, ch.id) is True
