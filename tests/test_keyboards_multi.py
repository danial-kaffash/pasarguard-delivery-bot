"""Tests for keyboards.py with ChannelOfferGroup (multi-tenant keyboard)."""

from __future__ import annotations

from bot.keyboards import GroupCB, _offer_id, build_selection_keyboard
from storage.db import ChannelOfferGroup, OfferGroup


def test_offer_id_with_legacy_offer_group():
    og = OfferGroup(id=42, label="NL", sort_order=0)
    assert _offer_id(og) == 42


def test_offer_id_with_channel_offer_group():
    cog = ChannelOfferGroup(channel_id=1, panel_id=1, group_id=42, label="NL", sort_order=0)
    assert _offer_id(cog) == 42


def test_keyboard_with_channel_offer_groups():
    offers = [
        ChannelOfferGroup(channel_id=1, panel_id=1, group_id=2, label="🇳🇱 هلند", sort_order=0),
        ChannelOfferGroup(channel_id=1, panel_id=1, group_id=5, label="🇹🇷 ترکیه", sort_order=1),
        ChannelOfferGroup(channel_id=1, panel_id=2, group_id=9, label="🇩🇪 آلمان", sort_order=2),
    ]
    kb = build_selection_keyboard(offers, selected=set())
    # 3 groups in 2-column layout = 2 rows + confirm/cancel row = 3 rows.
    assert len(kb.inline_keyboard) == 3
    # No checkmarks.
    for row in kb.inline_keyboard[:-1]:
        for btn in row:
            if btn.text != "✅ تأیید انتخاب" and btn.text != "❌ انصراف":
                assert "✅" not in btn.text


def test_keyboard_with_selection():
    offers = [
        ChannelOfferGroup(channel_id=1, panel_id=1, group_id=2, label="NL", sort_order=0),
        ChannelOfferGroup(channel_id=1, panel_id=1, group_id=5, label="TR", sort_order=1),
    ]
    kb = build_selection_keyboard(offers, selected={2})
    # First button should have ✅, second should not.
    first_btn = kb.inline_keyboard[0][0]
    second_btn = kb.inline_keyboard[0][1]
    assert "✅" in first_btn.text
    assert "✅" not in second_btn.text


def test_keyboard_callback_data():
    offers = [
        ChannelOfferGroup(channel_id=1, panel_id=1, group_id=42, label="NL", sort_order=0),
    ]
    kb = build_selection_keyboard(offers, selected=set())
    btn = kb.inline_keyboard[0][0]
    cb = GroupCB.unpack(btn.callback_data)
    assert cb.action == "toggle"
    assert cb.gid == 42


def test_keyboard_confirm_cancel_buttons():
    offers = [
        ChannelOfferGroup(channel_id=1, panel_id=1, group_id=2, label="NL", sort_order=0),
    ]
    kb = build_selection_keyboard(offers, selected=set())
    last_row = kb.inline_keyboard[-1]
    assert len(last_row) == 2
    confirm_cb = GroupCB.unpack(last_row[0].callback_data)
    cancel_cb = GroupCB.unpack(last_row[1].callback_data)
    assert confirm_cb.action == "confirm"
    assert cancel_cb.action == "cancel"
