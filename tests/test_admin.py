"""Tests for multi-tenant admin commands.

Tests exercise the handler functions directly with FakeMessage stubs,
the real SQLite layer, and FakePanelManager.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.handlers import admin
from bot.pause import is_channel_joins_paused, is_channel_paused
from storage import db as store
from tests.helpers import FakeBot, FakeMessage, FakePanel, FakePanelManager, make_settings

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


async def _setup_superadmin(db):
    await store.upsert_user(db, tg_user_id=1, role="superadmin")


async def _setup_channel(db, *, panel_groups=None):
    """Create a panel, channel, and optionally offer groups. Returns (panel, channel)."""
    panel = await store.create_panel(
        db,
        name="TestPanel",
        base_url="https://panel.test",
        admin_username="admin",
        admin_password="pw",
    )
    ch = await store.create_channel(
        db,
        tg_channel_id=-1001234567890,
        title="Test",
    )
    if panel_groups:
        for gid in panel_groups:
            await store.upsert_channel_offer_group(
                db,
                channel_id=ch.id,
                panel_id=panel.id,
                group_id=gid,
                label=f"group-{gid}",
            )
    return panel, ch


def _pm(panel_row, fake_panel):
    pm = FakePanelManager()
    pm.register(panel_row.id, fake_panel)
    return pm


# ── Auth filters ─────────────────────────────────────────────────────────────


async def test_is_superadmin_via_legacy_owner_ids(db):
    """Legacy OWNER_TG_IDS still work for bootstrapping."""
    flt = admin.IsSuperadmin()
    assert await flt(FakeMessage(user_id=1), db=db, settings=SETTINGS) is True
    assert await flt(FakeMessage(user_id=999), db=db, settings=SETTINGS) is False


async def test_is_superadmin_via_db_role(db):
    await store.upsert_user(db, tg_user_id=42, role="superadmin")
    flt = admin.IsSuperadmin()
    assert await flt(FakeMessage(user_id=42), db=db, settings=SETTINGS) is True


async def test_is_channel_admin_with_assignment(db):
    await store.upsert_user(db, tg_user_id=42, role="admin")
    ch = await store.create_channel(db, tg_channel_id=-1)
    await store.assign_channel_admin(db, 42, ch.id)
    flt = admin.IsChannelAdmin()
    assert await flt(FakeMessage(user_id=42), db=db, settings=SETTINGS) is True


async def test_is_channel_admin_no_assignment(db):
    await store.upsert_user(db, tg_user_id=42, role="admin")
    flt = admin.IsChannelAdmin()
    # Admin with no channel assignments → rejected.
    assert await flt(FakeMessage(user_id=42), db=db, settings=SETTINGS) is False


# ── Superadmin: panels ───────────────────────────────────────────────────────


async def test_addpanel(db):
    await _setup_superadmin(db)
    msg = FakeMessage(user_id=1)
    await admin.cmd_addpanel(msg, cmd("NL https://nl.test admin secret"), db=db)
    assert "✅" in msg.texts[0]
    panels = await store.list_panels(db)
    assert len(panels) == 1
    assert panels[0].name == "NL"


async def test_addpanel_bad_args(db):
    await _setup_superadmin(db)
    msg = FakeMessage(user_id=1)
    await admin.cmd_addpanel(msg, cmd(""), db=db)
    assert "کاربرد" in msg.texts[0]


async def test_panels_list(db):
    await _setup_superadmin(db)
    await store.create_panel(
        db, name="A", base_url="https://a", admin_username="a", admin_password="a"
    )
    await store.create_panel(
        db, name="B", base_url="https://b", admin_username="b", admin_password="b"
    )
    msg = FakeMessage(user_id=1)
    await admin.cmd_panels(msg, db=db)
    assert "A" in msg.texts[0] and "B" in msg.texts[0]


async def test_editpanel(db):
    await _setup_superadmin(db)
    panel = await store.create_panel(
        db,
        name="Old",
        base_url="https://old",
        admin_username="a",
        admin_password="b",
    )
    msg = FakeMessage(user_id=1)
    await admin.cmd_editpanel(msg, cmd(f"{panel.id} name NewName"), db=db)
    updated = await store.get_panel(db, panel.id)
    assert updated.name == "NewName"


# ── Superadmin: channels ─────────────────────────────────────────────────────


async def test_addchannel(db):
    await _setup_superadmin(db)
    msg = FakeMessage(user_id=1)
    bot = FakeBot()
    await admin.cmd_addchannel(msg, cmd("-100999 My Channel"), bot=bot, db=db)
    assert "✅" in msg.texts[0]
    ch = await store.get_channel_by_tg_id(db, -100999)
    assert ch is not None


async def test_addchannel_duplicate(db):
    await _setup_superadmin(db)
    await store.create_channel(db, tg_channel_id=-100999)
    msg = FakeMessage(user_id=1)
    bot = FakeBot()
    await admin.cmd_addchannel(msg, cmd("-100999"), bot=bot, db=db)
    assert "❌" in msg.texts[0]


async def test_addchannel_reactivates_deleted(db):
    """Re-adding a soft-deleted channel reactivates it instead of failing."""
    await _setup_superadmin(db)
    ch = await store.create_channel(db, tg_channel_id=-100999)
    await store.soft_delete_channel(db, ch.id)
    msg = FakeMessage(user_id=1)
    bot = FakeBot()
    await admin.cmd_addchannel(msg, cmd("-100999"), bot=bot, db=db)
    assert "✅" in msg.texts[0]
    assert "فعال" in msg.texts[0]
    updated = await store.get_channel(db, ch.id)
    assert updated.active is True


async def test_channels_list(db):
    await _setup_superadmin(db)
    await store.create_channel(db, tg_channel_id=-1, title="A")
    await store.create_channel(db, tg_channel_id=-2, title="B")
    msg = FakeMessage(user_id=1)
    await admin.cmd_channels(msg, db=db)
    assert "A" in msg.texts[0] and "B" in msg.texts[0]


async def test_removechannel(db):
    await _setup_superadmin(db)
    ch = await store.create_channel(db, tg_channel_id=-100999)
    msg = FakeMessage(user_id=1)
    await admin.cmd_removechannel(msg, cmd("-100999"), db=db)
    assert "✅" in msg.texts[0]
    updated = await store.get_channel(db, ch.id)
    assert updated.active is False


# ── Superadmin: users & assign ───────────────────────────────────────────────


async def test_promote_and_demote(db):
    await _setup_superadmin(db)
    msg = FakeMessage(user_id=1)
    await admin.cmd_promote(msg, cmd("42 admin"), db=db)
    user = await store.get_user(db, 42)
    assert user.role == "admin"

    await admin.cmd_demote(msg, cmd("42"), db=db)
    user = await store.get_user(db, 42)
    assert user.role == "user"


async def test_assign_and_unassign(db):
    await _setup_superadmin(db)
    ch = await store.create_channel(db, tg_channel_id=-100999)
    msg = FakeMessage(user_id=1)
    await admin.cmd_assign(msg, cmd("42 -100999"), db=db)
    assert await store.is_channel_admin(db, 42, ch.id) is True

    await admin.cmd_unassign(msg, cmd("42 -100999"), db=db)
    assert await store.is_channel_admin(db, 42, ch.id) is False


async def test_users_list(db):
    await _setup_superadmin(db)
    await store.upsert_user(db, tg_user_id=42, role="admin", username="alice")
    msg = FakeMessage(user_id=1)
    await admin.cmd_users(msg, db=db)
    assert "alice" in msg.texts[0] or "42" in msg.texts[0]


async def test_sysstats(db):
    await _setup_superadmin(db)
    msg = FakeMessage(user_id=1)
    await admin.cmd_sysstats(msg, db=db)
    assert "آمار سیستم" in msg.texts[0]


# ── Channel commands: pause/resume ───────────────────────────────────────────


async def test_pause_and_resume_channel(db):
    await _setup_superadmin(db)
    panel, ch = await _setup_channel(db)
    msg = FakeMessage(user_id=1)
    await admin.cmd_pause(msg, cmd(str(ch.tg_channel_id)), db=db, settings=SETTINGS)
    assert await is_channel_paused(db, ch.id) is True

    await admin.cmd_resume(msg, cmd(str(ch.tg_channel_id)), db=db, settings=SETTINGS)
    assert await is_channel_paused(db, ch.id) is False


async def test_pause_in_channel_context(db):
    """When sent in the channel itself, no explicit channel_id needed."""
    await _setup_superadmin(db)
    panel, ch = await _setup_channel(db)
    msg = FakeMessage(user_id=1)
    msg.chat = SimpleNamespace(id=ch.tg_channel_id, type="supergroup")
    msg.text = "/pause"
    await admin.cmd_pause(msg, cmd(""), db=db, settings=SETTINGS)
    assert await is_channel_paused(db, ch.id) is True


async def test_pausejoins_resumejoins(db):
    await _setup_superadmin(db)
    panel, ch = await _setup_channel(db)
    msg = FakeMessage(user_id=1)
    await admin.cmd_pausejoins(msg, cmd(str(ch.tg_channel_id)), db=db, settings=SETTINGS)
    assert await is_channel_joins_paused(db, ch.id) is True

    await admin.cmd_resumejoins(msg, cmd(str(ch.tg_channel_id)), db=db, settings=SETTINGS)
    assert await is_channel_joins_paused(db, ch.id) is False


# ── Channel commands: promo ──────────────────────────────────────────────────


async def test_setpromo_saves_per_channel(db):
    await _setup_superadmin(db)
    panel, ch = await _setup_channel(db)
    msg = FakeMessage(user_id=1)
    await admin.cmd_setpromo(msg, cmd(f"{ch.tg_channel_id} 🎁 متن جدید"), db=db, settings=SETTINGS)
    key = f"channel:{ch.id}:promo_text"
    assert await store.get_setting(db, key) == "🎁 متن جدید"
    assert "✅" in msg.texts[0]


async def test_setpromo_empty_rejected(db):
    await _setup_superadmin(db)
    panel, ch = await _setup_channel(db)
    msg = FakeMessage(user_id=1)
    await admin.cmd_setpromo(msg, cmd(str(ch.tg_channel_id)), db=db, settings=SETTINGS)
    assert "کاربرد" in msg.texts[0]


async def test_setinterval(db):
    await _setup_superadmin(db)
    panel, ch = await _setup_channel(db)
    msg = FakeMessage(user_id=1)
    await admin.cmd_setinterval(msg, cmd(f"{ch.tg_channel_id} 12"), db=db, settings=SETTINGS)
    key = f"channel:{ch.id}:promo_interval_hours"
    assert await store.get_setting(db, key) in ("12", "12.0")


async def test_settrial(db):
    await _setup_superadmin(db)
    panel, ch = await _setup_channel(db)
    msg = FakeMessage(user_id=1)
    await admin.cmd_settrial(
        msg, cmd(f"{ch.tg_channel_id} data_limit_gb 10"), db=db, settings=SETTINGS
    )
    updated = await store.get_channel(db, ch.id)
    assert updated.trial_data_limit_gb == 10.0


async def test_setjoindelay(db):
    await _setup_superadmin(db)
    panel, ch = await _setup_channel(db)
    msg = FakeMessage(user_id=1)
    await admin.cmd_setjoindelay(msg, cmd(f"{ch.tg_channel_id} 30"), db=db, settings=SETTINGS)
    updated = await store.get_channel(db, ch.id)
    assert updated.join_approval_delay_seconds == 30


# ── Channel commands: offer groups ───────────────────────────────────────────


async def test_setoffer_and_deloffer(db):
    await _setup_superadmin(db)
    panel, ch = await _setup_channel(db)
    msg = FakeMessage(user_id=1)
    await admin.cmd_setoffer(
        msg, cmd(f"{ch.tg_channel_id} {panel.id} 5 🇳🇱 هلند"), db=db, settings=SETTINGS
    )
    offers = await store.list_channel_offer_groups(db, ch.id)
    assert len(offers) == 1
    assert offers[0].label == "🇳🇱 هلند"

    await admin.cmd_deloffer(msg, cmd(f"{ch.tg_channel_id} {panel.id} 5"), db=db, settings=SETTINGS)
    offers = await store.list_channel_offer_groups(db, ch.id)
    assert len(offers) == 0


async def test_clearoffers(db):
    await _setup_superadmin(db)
    panel, ch = await _setup_channel(db, panel_groups=[2, 5])
    msg = FakeMessage(user_id=1)
    await admin.cmd_clearoffers(msg, cmd(str(ch.tg_channel_id)), db=db, settings=SETTINGS)
    assert await store.list_channel_offer_groups(db, ch.id) == []


async def test_offergroups_shows_list(db):
    await _setup_superadmin(db)
    panel, ch = await _setup_channel(db, panel_groups=[2, 5])
    pm = _pm(panel, FakePanel(groups=[(2, "NL"), (5, "TR")]))
    msg = FakeMessage(user_id=1)
    await admin.cmd_offergroups(
        msg, cmd(str(ch.tg_channel_id)), db=db, settings=SETTINGS, panel_manager=pm
    )
    assert "🇳🇱" in msg.texts[0] or "group-2" in msg.texts[0]


# ── Channel commands: stats ──────────────────────────────────────────────────


async def test_reset_revokes_grant(db):
    await _setup_superadmin(db)
    panel, ch = await _setup_channel(db)
    await store.record_grant(db, tg_user_id=42, panel_username="t42_x", channel_id=ch.id)
    msg = FakeMessage(user_id=1)
    await admin.cmd_reset(msg, cmd(f"{ch.tg_channel_id} 42"), db=db, settings=SETTINGS)
    grant = await store.get_latest_grant(db, 42)
    assert grant.revoked is True


async def test_stats_shows_channel_data(db):
    await _setup_superadmin(db)
    panel, ch = await _setup_channel(db, panel_groups=[2])
    await store.record_grant(db, tg_user_id=1, panel_username="t1_a", channel_id=ch.id)
    await store.upsert_chat_member(db, ch.tg_channel_id, 1, "member")
    msg = FakeMessage(user_id=1)
    await admin.cmd_stats(msg, cmd(str(ch.tg_channel_id)), db=db, settings=SETTINGS)
    text = msg.texts[0]
    assert str(ch.tg_channel_id) in text
    assert "اعضا" in text or "1" in text


# ── DM requires channel_id ───────────────────────────────────────────────────


async def test_dm_requires_channel_id(db):
    """In DM without a channel_id, the command should ask for one."""
    await _setup_superadmin(db)
    await _setup_channel(db)
    msg = FakeMessage(user_id=1)
    msg.chat = SimpleNamespace(id=1, type="private")  # DM
    await admin.cmd_stats(msg, cmd(""), db=db, settings=SETTINGS)
    assert "آیدی کانال" in msg.texts[0] or "پی‌وی" in msg.texts[0]


async def test_dm_unknown_channel(db):
    await _setup_superadmin(db)
    msg = FakeMessage(user_id=1)
    msg.chat = SimpleNamespace(id=1, type="private")
    await admin.cmd_stats(msg, cmd("-999"), db=db, settings=SETTINGS)
    assert "یافت نشد" in msg.texts[0]
