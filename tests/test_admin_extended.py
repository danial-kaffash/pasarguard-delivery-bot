"""Extended admin command tests — filling coverage gaps.

Covers: editchannel, groups, offergroups, reorder, joinstats, setmaxage,
        and edge cases.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.handlers import admin
from bot.pause import set_channel_joins_paused
from storage import db as store
from tests.helpers import FakeMessage, FakePanel, FakePanelManager, make_settings

SETTINGS = make_settings()


def cmd(s: str) -> SimpleNamespace:
    return SimpleNamespace(args=s)


@pytest.fixture
async def db(tmp_path):
    conn = await store.connect(tmp_path / "test.db")
    try:
        yield conn
    finally:
        await conn.close()


async def _setup(db, *, panel_groups=None):
    await store.upsert_user(db, tg_user_id=1, role="superadmin")
    panel = await store.create_panel(
        db, name="TestPanel", base_url="https://panel.test",
        admin_username="admin", admin_password="pw",
    )
    ch = await store.create_channel(db, tg_channel_id=-1001234567890, title="Test")
    if panel_groups:
        for gid in panel_groups:
            await store.upsert_channel_offer_group(
                db, channel_id=ch.id, panel_id=panel.id, group_id=gid, label=f"group-{gid}",
            )
    return panel, ch


# ── editchannel ──────────────────────────────────────────────────────────────


async def test_editchannel_updates_field(db):
    await _setup(db)
    msg = FakeMessage(user_id=1)
    await admin.cmd_editchannel(msg, cmd("-1001234567890 title NewTitle"), db=db)
    assert "✅" in msg.texts[0]
    ch = await store.get_channel_by_tg_id(db, -1001234567890)
    assert ch.title == "NewTitle"


async def test_editchannel_numeric_field(db):
    await _setup(db)
    msg = FakeMessage(user_id=1)
    await admin.cmd_editchannel(msg, cmd("-1001234567890 trial_days 14"), db=db)
    ch = await store.get_channel_by_tg_id(db, -1001234567890)
    assert ch.trial_days == 14


async def test_editchannel_not_found(db):
    await _setup(db)
    msg = FakeMessage(user_id=1)
    await admin.cmd_editchannel(msg, cmd("-999 title X"), db=db)
    assert "یافت نشد" in msg.texts[0]


async def test_editchannel_bad_args(db):
    await _setup(db)
    msg = FakeMessage(user_id=1)
    await admin.cmd_editchannel(msg, cmd(""), db=db)
    assert "کاربرد" in msg.texts[0]


# ── groups ───────────────────────────────────────────────────────────────────


async def test_groups_lists_from_all_panels(db):
    panel, ch = await _setup(db)
    pm = FakePanelManager()
    pm.register(panel.id, FakePanel(groups=[(2, "NL"), (5, "TR")]))
    msg = FakeMessage(user_id=1)
    await admin.cmd_groups(msg, cmd(str(ch.tg_channel_id)), db=db, settings=SETTINGS, panel_manager=pm)
    assert "2 —" in msg.texts[0] or "NL" in msg.texts[0]


async def test_groups_no_panels(db):
    await store.upsert_user(db, tg_user_id=1, role="superadmin")
    await store.create_channel(db, tg_channel_id=-1, title="Empty")
    pm = FakePanelManager()
    msg = FakeMessage(user_id=1)
    await admin.cmd_groups(msg, cmd("-1"), db=db, settings=SETTINGS, panel_manager=pm)
    assert "هیچ پنل" in msg.texts[0]


# ── offergroups ──────────────────────────────────────────────────────────────


async def test_offergroups_shows_list(db):
    panel, ch = await _setup(db, panel_groups=[2, 5])
    pm = FakePanelManager()
    pm.register(panel.id, FakePanel(groups=[(2, "NL"), (5, "TR")]))
    msg = FakeMessage(user_id=1)
    await admin.cmd_offergroups(msg, cmd(str(ch.tg_channel_id)), db=db, settings=SETTINGS, panel_manager=pm)
    assert "group-2" in msg.texts[0] or "NL" in msg.texts[0]


async def test_offergroups_empty(db):
    panel, ch = await _setup(db)
    pm = FakePanelManager()
    pm.register(panel.id, FakePanel(groups=[]))
    msg = FakeMessage(user_id=1)
    await admin.cmd_offergroups(msg, cmd(str(ch.tg_channel_id)), db=db, settings=SETTINGS, panel_manager=pm)
    assert "خالی" in msg.texts[0]


# ── reorder ──────────────────────────────────────────────────────────────────


async def test_reorder_works(db):
    panel, ch = await _setup(db, panel_groups=[2, 5, 9])
    msg = FakeMessage(user_id=1)
    await admin.cmd_reorder(
        msg, cmd(f"{ch.tg_channel_id} {panel.id}:9,{panel.id}:2,{panel.id}:5"),
        db=db, settings=SETTINGS,
    )
    offers = await store.list_channel_offer_groups(db, ch.id)
    assert [o.group_id for o in offers] == [9, 2, 5]


async def test_reorder_bad_args(db):
    panel, ch = await _setup(db, panel_groups=[2])
    msg = FakeMessage(user_id=1)
    await admin.cmd_reorder(msg, cmd(f"{ch.tg_channel_id} bad"), db=db, settings=SETTINGS)
    assert "کاربرد" in msg.texts[0]


# ── joinstats ────────────────────────────────────────────────────────────────


async def test_joinstats_shows_data(db):
    panel, ch = await _setup(db)
    await store.record_grant(db, tg_user_id=10, panel_username="t10", source="join_request")
    await store.record_member_event(db, ch.tg_channel_id, 20, "join_request")
    msg = FakeMessage(user_id=1)
    await admin.cmd_joinstats(msg, cmd(str(ch.tg_channel_id)), db=db, settings=SETTINGS)
    assert "آمار درخواست عضویت" in msg.texts[0]


async def test_joinstats_shows_paused_status(db):
    panel, ch = await _setup(db)
    await set_channel_joins_paused(db, ch.id, True)
    msg = FakeMessage(user_id=1)
    await admin.cmd_joinstats(msg, cmd(str(ch.tg_channel_id)), db=db, settings=SETTINGS)
    assert "متوقف" in msg.texts[0]


# ── setmaxage ────────────────────────────────────────────────────────────────


async def test_setmaxage_sets_value(db):
    panel, ch = await _setup(db)
    msg = FakeMessage(user_id=1)
    await admin.cmd_setmaxage(msg, cmd(f"{ch.tg_channel_id} 7"), db=db, settings=SETTINGS)
    updated = await store.get_channel(db, ch.id)
    assert updated.trial_max_member_age_days == 7.0


async def test_setmaxage_zero_disables(db):
    panel, ch = await _setup(db)
    await store.update_channel(db, ch.id, trial_max_member_age_days=7.0)
    msg = FakeMessage(user_id=1)
    await admin.cmd_setmaxage(msg, cmd(f"{ch.tg_channel_id} 0"), db=db, settings=SETTINGS)
    updated = await store.get_channel(db, ch.id)
    assert updated.trial_max_member_age_days == 0.0


async def test_setmaxage_invalid(db):
    panel, ch = await _setup(db)
    msg = FakeMessage(user_id=1)
    await admin.cmd_setmaxage(msg, cmd(f"{ch.tg_channel_id} abc"), db=db, settings=SETTINGS)
    assert "کاربرد" in msg.texts[0]


# ── removepanel ──────────────────────────────────────────────────────────────


async def test_removepanel_soft_deletes(db):
    await store.upsert_user(db, tg_user_id=1, role="superadmin")
    panel = await store.create_panel(
        db, name="X", base_url="https://x", admin_username="a", admin_password="b",
    )
    msg = FakeMessage(user_id=1)
    await admin.cmd_removepanel(msg, cmd(str(panel.id)), db=db)
    assert "✅" in msg.texts[0]
    updated = await store.get_panel(db, panel.id)
    assert updated.active is False


async def test_removepanel_not_found(db):
    await store.upsert_user(db, tg_user_id=1, role="superadmin")
    msg = FakeMessage(user_id=1)
    await admin.cmd_removepanel(msg, cmd("999"), db=db)
    assert "یافت نشد" in msg.texts[0]


# ── setoffer bad args ────────────────────────────────────────────────────────


async def test_setoffer_bad_args(db):
    panel, ch = await _setup(db)
    msg = FakeMessage(user_id=1)
    await admin.cmd_setoffer(msg, cmd(f"{ch.tg_channel_id}"), db=db, settings=SETTINGS)
    assert "کاربرد" in msg.texts[0]


async def test_setoffer_panel_not_found(db):
    panel, ch = await _setup(db)
    msg = FakeMessage(user_id=1)
    await admin.cmd_setoffer(msg, cmd(f"{ch.tg_channel_id} 999 5 NL"), db=db, settings=SETTINGS)
    assert "یافت نشد" in msg.texts[0]


# ── promonow ─────────────────────────────────────────────────────────────────


async def test_promonow_publishes(db):
    panel, ch = await _setup(db)
    bot = FakeBotSimple()
    msg = FakeMessage(user_id=1)
    await admin.cmd_promonow(msg, bot=bot, command=cmd(str(ch.tg_channel_id)), db=db, settings=SETTINGS)
    assert len(bot.sent) == 1
    assert "✅" in msg.texts[0]


class FakeBotSimple:
    def __init__(self):
        self.sent = []
        self.pinned = []
        self._next_id = 1000

    async def get_me(self):
        return SimpleNamespace(username="TestBot")

    async def send_message(self, chat_id, text, **kwargs):
        self._next_id += 1
        self.sent.append({"chat_id": chat_id, "text": text})
        return SimpleNamespace(message_id=self._next_id)

    async def pin_chat_message(self, chat_id, message_id, **kwargs):
        self.pinned.append({"chat_id": chat_id, "message_id": message_id})

    async def delete_message(self, chat_id, message_id):
        pass
