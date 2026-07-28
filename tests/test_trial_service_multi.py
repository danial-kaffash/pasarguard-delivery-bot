"""Tests for multi-tenant trial service functions.

Covers get_channel_offered_groups and integration with ChannelSettings.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from panel.exceptions import PanelConflictError
from services import trial as trial_service
from services.channel_settings import ChannelSettings
from storage import db as store
from tests.helpers import FakePanel, FakePanelManager, make_panel_user


@pytest.fixture
async def db(tmp_path):
    conn = await store.connect(tmp_path / "test.db")
    try:
        yield conn
    finally:
        await conn.close()


async def _setup(db, *, panel_groups=(2, 5)):
    panel = await store.create_panel(
        db, name="NL", base_url="https://nl.test",
        admin_username="admin", admin_password="pw",
    )
    ch = await store.create_channel(db, tg_channel_id=-100123, title="Test")
    for gid in panel_groups:
        await store.upsert_channel_offer_group(
            db, channel_id=ch.id, panel_id=panel.id, group_id=gid, label=f"Group {gid}",
        )
    return panel, ch


# ── get_channel_offered_groups ───────────────────────────────────────────────


async def test_get_channel_offered_groups_valid(db):
    panel_row, ch = await _setup(db)
    pm = FakePanelManager()
    pm.register(panel_row.id, FakePanel(groups=[(2, "NL"), (5, "TR")]))

    valid, stale = await trial_service.get_channel_offered_groups(pm, db, ch.id)
    assert len(valid) == 2
    assert len(stale) == 0
    assert {o.group_id for o in valid} == {2, 5}


async def test_get_channel_offered_groups_stale(db):
    panel_row, ch = await _setup(db, panel_groups=[2, 99])
    pm = FakePanelManager()
    pm.register(panel_row.id, FakePanel(groups=[(2, "NL")]))  # 99 no longer exists

    valid, stale = await trial_service.get_channel_offered_groups(pm, db, ch.id)
    assert len(valid) == 1
    assert valid[0].group_id == 2
    assert len(stale) == 1
    assert stale[0] == (panel_row.id, 99)


async def test_get_channel_offered_groups_inactive_panel(db):
    panel_row, ch = await _setup(db)
    await store.soft_delete_panel(db, panel_row.id)
    pm = FakePanelManager()

    valid, stale = await trial_service.get_channel_offered_groups(pm, db, ch.id)
    assert len(valid) == 0
    assert len(stale) == 2


async def test_get_channel_offered_groups_empty(db):
    panel_row = await store.create_panel(
        db, name="P", base_url="https://p", admin_username="a", admin_password="b",
    )
    ch = await store.create_channel(db, tg_channel_id=-1, title="Empty")
    pm = FakePanelManager()

    valid, stale = await trial_service.get_channel_offered_groups(pm, db, ch.id)
    assert len(valid) == 0
    assert len(stale) == 0


async def test_get_channel_offered_groups_multi_panel(db):
    """Groups from multiple panels — each validated against its own panel."""
    p1 = await store.create_panel(
        db, name="NL", base_url="https://nl", admin_username="a", admin_password="b",
    )
    p2 = await store.create_panel(
        db, name="TR", base_url="https://tr", admin_username="a", admin_password="b",
    )
    ch = await store.create_channel(db, tg_channel_id=-1, title="Multi")
    await store.upsert_channel_offer_group(db, channel_id=ch.id, panel_id=p1.id, group_id=2, label="NL")
    await store.upsert_channel_offer_group(db, channel_id=ch.id, panel_id=p2.id, group_id=5, label="TR")

    pm = FakePanelManager()
    pm.register(p1.id, FakePanel(groups=[(2, "NL")]))
    pm.register(p2.id, FakePanel(groups=[(5, "TR")]))

    valid, stale = await trial_service.get_channel_offered_groups(pm, db, ch.id)
    assert len(valid) == 2
    assert len(stale) == 0
    panel_ids = {o.panel_id for o in valid}
    assert panel_ids == {p1.id, p2.id}


# ── ChannelSettings integration with eligibility ────────────────────────────


async def test_eligibility_with_channel_settings(db):
    """check_eligibility works with ChannelSettings."""
    panel_row = await store.create_panel(
        db, name="P", base_url="https://p", admin_username="a", admin_password="b",
        auto_delete_days=11,
    )
    ch = await store.create_channel(
        db, tg_channel_id=-1, title="T",
        on_hold_grace_days=7, trial_days=3, allow_regrant_after_days=30,
    )
    cs = ChannelSettings(ch, panel_row)

    # No grant → eligible.
    assert trial_service.check_eligibility(None, cs).eligible is True

    # Active grant → not eligible.
    grant = store.TrialGrant(
        tg_user_id=1, panel_username="t1", created_at=datetime.now(UTC),
    )
    result = trial_service.check_eligibility(grant, cs)
    assert result.eligible is False
    assert result.reason == "active"


async def test_build_trial_user_with_channel_settings(db):
    """build_trial_user works with ChannelSettings."""
    panel_row = await store.create_panel(
        db, name="P", base_url="https://p", admin_username="a", admin_password="b",
        protocols="vless,trojan", auto_delete_days=14,
    )
    ch = await store.create_channel(
        db, tg_channel_id=-1, title="T",
        trial_data_limit_gb=10.0, trial_days=7, on_hold_grace_days=14,
    )
    cs = ChannelSettings(ch, panel_row)

    user = trial_service.build_trial_user(
        settings=cs, username="t1_abc", tg_user_id=1, group_ids=[2, 5],
    )
    assert user.data_limit == 10 * 1024**3
    assert user.on_hold_expire_duration == 7 * 86400
    assert "vless" in user.proxy_settings
    assert "trojan" in user.proxy_settings
    assert user.auto_delete_in_days == 14


async def test_create_trial_with_channel_settings(db):
    """Full create_trial flow using ChannelSettings."""
    from tests.helpers import FakePanel as FakePanelClient

    panel_row = await store.create_panel(
        db, name="P", base_url="https://p", admin_username="a", admin_password="b",
        protocols="vless", auto_delete_days=11,
    )
    ch = await store.create_channel(
        db, tg_channel_id=-1, title="T",
        trial_data_limit_gb=5.0, trial_days=3, on_hold_grace_days=7,
    )
    cs = ChannelSettings(ch, panel_row)
    fake_panel = FakePanelClient(groups=[(2, "NL")])

    panel_user, username = await trial_service.create_trial(
        fake_panel, settings=cs, tg_user_id=42, group_ids=[2],
    )
    assert username.startswith("t42_")
    assert panel_user.subscription_url
    assert len(fake_panel.create_calls) == 1
