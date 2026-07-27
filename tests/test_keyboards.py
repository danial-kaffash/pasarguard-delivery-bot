"""Tests for the group multi-select keyboard."""

from __future__ import annotations

from bot.keyboards import GroupCB, build_selection_keyboard
from storage.db import OfferGroup


def _offers():
    return [
        OfferGroup(id=2, label="🇳🇱 هلند", sort_order=0),
        OfferGroup(id=5, label="🇹🇷 ترکیه", sort_order=1),
        OfferGroup(id=9, label="🇩🇪 آلمان", sort_order=2),
    ]


def test_layout_two_columns_plus_controls():
    kb = build_selection_keyboard(_offers(), selected=set())
    rows = kb.inline_keyboard
    assert [len(r) for r in rows] == [2, 1, 2]  # 3 groups packed, then controls
    assert rows[0][0].text == "🇳🇱 هلند"
    assert rows[2][0].text == "✅ تأیید انتخاب"
    assert rows[2][1].text == "❌ انصراف"


def test_selected_gets_checkmark():
    kb = build_selection_keyboard(_offers(), selected={5})
    texts = [b.text for row in kb.inline_keyboard[:2] for b in row]
    assert "✅ 🇹🇷 ترکیه" in texts
    assert "🇳🇱 هلند" in texts  # unselected, no mark


def test_callback_data_roundtrip():
    kb = build_selection_keyboard(_offers(), selected=set())
    toggle = kb.inline_keyboard[0][1]
    cb = GroupCB.unpack(toggle.callback_data)
    assert cb.action == "toggle" and cb.gid == 5

    confirm = kb.inline_keyboard[2][0]
    cb = GroupCB.unpack(confirm.callback_data)
    assert cb.action == "confirm" and cb.gid == 0
