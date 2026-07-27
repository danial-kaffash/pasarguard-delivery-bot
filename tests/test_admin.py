"""Tests for owner commands (bot/handlers/admin.py).

Handler functions are plain async callables — we invoke them directly
with FakeMessage / CommandObject stubs and the real SQLite layer.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from bot.handlers import admin
from panel.exceptions import PanelAuthError
from storage import db as store
from tests.helpers import FakeBot, FakeMessage, FakePanel, make_settings

SETTINGS = make_settings()


def args(s: str) -> SimpleNamespace:
    return SimpleNamespace(args=s)


@pytest.fixture
async def db(tmp_path):
    conn = await store.connect(tmp_path / "test.db")
    try:
        yield conn
    finally:
        await conn.close()


# ── owner filter ─────────────────────────────────────────────────────────────


async def test_owner_filter():
    flt = admin.IsOwner()
    assert await flt(FakeMessage(user_id=1), settings=SETTINGS) is True
    assert await flt(FakeMessage(user_id=999), settings=SETTINGS) is False


# ── promo commands ───────────────────────────────────────────────────────────


async def test_setpromo_saves_and_rejects_empty(db):
    msg = FakeMessage()
    await admin.cmd_setpromo(msg, args("🎁 متن جدید"), db=db)
    assert await store.get_setting(db, "promo_text") == "🎁 متن جدید"
    assert "✅" in msg.texts[0]

    msg2 = FakeMessage()
    await admin.cmd_setpromo(msg2, args(""), db=db)
    assert "کاربرد" in msg2.texts[0]
    assert await store.get_setting(db, "promo_text") == "🎁 متن جدید"  # unchanged


async def test_setinterval_valid_and_invalid(db):
    ok = FakeMessage()
    await admin.cmd_setinterval(ok, args("2.5"), db=db)
    assert await store.get_setting(db, "promo_interval_hours") == "2.5"

    for bad in ("", "abc", "-1", "99999"):
        msg = FakeMessage()
        await admin.cmd_setinterval(msg, args(bad), db=db)
        assert "کاربرد" in msg.texts[0]


async def test_promonow_publishes_and_reschedules(db):
    bot = FakeBot()
    msg = FakeMessage()
    await admin.cmd_promonow(msg, bot=bot, db=db, settings=SETTINGS)

    assert len(bot.sent) == 1 and bot.sent[0]["chat_id"] == SETTINGS.channel_id
    assert len(bot.pinned) == 1
    state = await store.get_promo_state(db)
    assert state.message_id == bot.pinned[0]["message_id"]
    assert state.next_run_at > time.time() + 5 * 3600  # ~6h ahead
    assert "✅" in msg.texts[0]


async def test_getpromo_shows_text_and_interval(db):
    msg = FakeMessage()
    await admin.cmd_getpromo(msg, db=db, settings=SETTINGS)
    joined = "\n".join(msg.texts)
    assert "6" in joined  # default interval
    # second message is the plain-text preview of the Persian seed file
    assert "تست رایگان" in msg.texts[-1]


# ── group commands ───────────────────────────────────────────────────────────


async def test_groups_lists_panel_groups(db):
    panel = FakePanel(groups=[(2, "NL-Amazon"), (5, "TR-Istanbul")])
    msg = FakeMessage()
    await admin.cmd_groups(msg, panel=panel)
    assert "2 — NL-Amazon" in msg.texts[0]
    assert "5 — TR-Istanbul" in msg.texts[0]


async def test_groups_panel_error(db):
    class BrokenPanel:
        async def list_groups_simple(self):
            raise PanelAuthError("login failed")

    msg = FakeMessage()
    await admin.cmd_groups(msg, panel=BrokenPanel())
    assert "❌" in msg.texts[0]


async def test_setoffer_and_deloffer(db):
    msg = FakeMessage()
    await admin.cmd_setoffer(msg, args("2 🇳🇱 هلند"), db=db)
    offers = await store.list_offer_groups(db)
    assert [(o.id, o.label) for o in offers] == [(2, "🇳🇱 هلند")]

    bad = FakeMessage()
    await admin.cmd_setoffer(bad, args("not-a-number label"), db=db)
    assert "کاربرد" in bad.texts[0]

    hit = FakeMessage()
    await admin.cmd_deloffer(hit, args("2"), db=db)
    assert await store.list_offer_groups(db) == []

    miss = FakeMessage()
    await admin.cmd_deloffer(miss, args("77"), db=db)
    assert "نبود" in miss.texts[0]


async def test_offergroups_shows_list_and_stale_warning(db):
    await store.upsert_offer_group(db, 2, "🇳🇱 هلند")
    await store.upsert_offer_group(db, 99, "👻 حذف‌شده")
    panel = FakePanel(groups=[(2, "NL")])  # 99 no longer exists
    msg = FakeMessage()
    await admin.cmd_offergroups(msg, panel=panel, db=db)
    assert "🇳🇱 هلند" in msg.texts[0]
    assert "99" in msg.texts[0]  # stale warning


async def test_reorder_valid_and_mismatch(db):
    for gid in (2, 5, 9):
        await store.upsert_offer_group(db, gid, f"g{gid}")

    ok = FakeMessage()
    await admin.cmd_reorder(ok, args("9,2,5"), db=db)
    assert [o.id for o in await store.list_offer_groups(db)] == [9, 2, 5]

    bad = FakeMessage()
    await admin.cmd_reorder(bad, args("2,5"), db=db)  # missing 9
    assert "کاربرد" in bad.texts[0] or "idهای فعلی" in bad.texts[0]


async def test_clearoffers(db):
    await store.upsert_offer_group(db, 2, "x")
    await store.upsert_offer_group(db, 5, "y")
    msg = FakeMessage()
    await admin.cmd_clearoffers(msg, db=db)
    assert "2" in msg.texts[0]
    assert await store.list_offer_groups(db) == []


# ── grants & stats ───────────────────────────────────────────────────────────


async def test_reset_revokes_grant(db):
    await store.record_grant(db, tg_user_id=42, panel_username="t42_x")
    msg = FakeMessage()
    await admin.cmd_reset(msg, args("42"), db=db)
    assert (await store.get_latest_grant(db, 42)).revoked is True

    miss = FakeMessage()
    await admin.cmd_reset(miss, args("43"), db=db)
    assert "نشده بود" in miss.texts[0]

    bad = FakeMessage()
    await admin.cmd_reset(bad, args("abc"), db=db)
    assert "کاربرد" in bad.texts[0]


async def test_stats_numbers(db):
    await store.record_grant(db, tg_user_id=1, panel_username="t1_a")
    await store.record_grant(db, tg_user_id=2, panel_username="t2_b")
    await store.revoke_grant(db, 2)  # 1 active, 1 revoked
    await store.upsert_chat_member(db, SETTINGS.channel_id, 1, "member")
    await store.upsert_chat_member(db, SETTINGS.channel_id, 2, "left")
    await store.record_member_event(db, SETTINGS.channel_id, 1, "join")
    await store.record_member_event(db, SETTINGS.channel_id, 2, "leave")
    await store.upsert_offer_group(db, 2, "g2")

    msg = FakeMessage()
    await admin.cmd_stats(msg, db=db, settings=SETTINGS)
    text = msg.texts[0]
    assert "کل تست‌های داده‌شده: <b>2</b>" in text
    assert "تست‌های فعال: <b>1</b>" in text
    assert "عضوهای کانال (دیده‌شده): <b>1</b>" in text
