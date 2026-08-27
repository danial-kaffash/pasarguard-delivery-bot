"""Tests for bot.migration — first-run .env → DB migration."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.migration import migrate_from_env
from bot.promo import get_channel_promo_state, set_channel_promo_state
from storage import db as store


@pytest.fixture
async def db(tmp_path):
    conn = await store.connect(tmp_path / "test.db")
    try:
        yield conn
    finally:
        await conn.close()


def _settings(**overrides):
    base = {
        "panel_base_url": "https://panel.test",
        "panel_admin_username": "admin",
        "panel_admin_password": "secret",
        "panel_verify_ssl": True,
        "panel_timeout_seconds": 15.0,
        "trial_protocols": "vless",
        "auto_delete_days": 11,
        "channel_id": -1001234567890,
        "trial_data_limit_gb": 5.0,
        "trial_days": 3,
        "on_hold_grace_days": 7,
        "allow_regrant_after_days": 30,
        "trial_max_member_age_days": 0.0,
        "join_approval_delay_seconds": 10,
        "promo_interval_hours": 6.0,
        "promo_pin": True,
        "promo_silent": True,
        "owner_tg_ids": [42, 99],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# ── Migration runs once ──────────────────────────────────────────────────────


async def test_migrate_creates_panel_and_channel(db):
    settings = _settings()
    result = await migrate_from_env(db, settings)
    assert result is True

    panels = await store.list_panels(db)
    assert len(panels) == 1
    assert panels[0].name == "Default"
    assert panels[0].base_url == "https://panel.test"

    channels = await store.list_channels(db)
    assert len(channels) == 1
    assert channels[0].tg_channel_id == -1001234567890


async def test_migrate_skips_if_panels_exist(db):
    await store.create_panel(
        db, name="Existing", base_url="https://x",
        admin_username="x", admin_password="x",
    )
    settings = _settings()
    result = await migrate_from_env(db, settings)
    assert result is False
    assert len(await store.list_panels(db)) == 1  # unchanged


async def test_migrate_skips_if_no_env_values(db):
    settings = _settings(panel_base_url="", channel_id=0)
    result = await migrate_from_env(db, settings)
    assert result is False


# ── Offer groups migration ───────────────────────────────────────────────────


async def test_migrate_offer_groups(db):
    # Seed legacy offer groups.
    await store.upsert_offer_group(db, 2, "🇳🇱 هلند")
    await store.upsert_offer_group(db, 5, "🇹🇷 ترکیه")

    settings = _settings()
    await migrate_from_env(db, settings)

    channels = await store.list_channels(db)
    ch = channels[0]
    offers = await store.list_channel_offer_groups(db, ch.id)
    assert len(offers) == 2
    labels = {o.group_id: o.label for o in offers}
    assert labels[2] == "🇳🇱 هلند"
    assert labels[5] == "🇹🇷 ترکیه"
    # All should be linked to the migrated panel.
    panels = await store.list_panels(db)
    assert all(o.panel_id == panels[0].id for o in offers)


# ── Promo state migration ────────────────────────────────────────────────────


async def test_migrate_promo_state(db):
    # Set legacy promo state.
    await store.set_promo_state(db, -1001234567890, message_id=777, next_run_at=999999.0)

    settings = _settings()
    await migrate_from_env(db, settings)

    channels = await store.list_channels(db)
    ch = channels[0]
    state = await get_channel_promo_state(db, ch.id)
    assert state is not None
    assert state.message_id == 777
    assert state.next_run_at == 999999.0


# ── Settings migration ───────────────────────────────────────────────────────


async def test_migrate_settings(db):
    await store.set_setting(db, "promo_text", "متن قدیمی")
    await store.set_setting(db, "paused", "true")
    await store.set_setting(db, "joins_paused", "false")

    settings = _settings()
    await migrate_from_env(db, settings)

    channels = await store.list_channels(db)
    ch = channels[0]

    assert await store.get_setting(db, f"channel:{ch.id}:promo_text") == "متن قدیمی"
    assert await store.get_setting(db, f"channel:{ch.id}:paused") == "true"
    assert await store.get_setting(db, f"channel:{ch.id}:joins_paused") == "false"


# ── Owner IDs migration ──────────────────────────────────────────────────────


async def test_migrate_owner_ids_as_superadmins(db):
    settings = _settings(owner_tg_ids=[42, 99])
    await migrate_from_env(db, settings)

    user42 = await store.get_user(db, 42)
    user99 = await store.get_user(db, 99)
    assert user42 is not None and user42.role == "superadmin"
    assert user99 is not None and user99.role == "superadmin"


# ── Trial settings migration ─────────────────────────────────────────────────


async def test_migrate_trial_settings(db):
    settings = _settings(
        trial_data_limit_gb=10.0,
        trial_days=7,
        on_hold_grace_days=14,
        allow_regrant_after_days=60,
        trial_max_member_age_days=3.0,
        join_approval_delay_seconds=30,
        promo_interval_hours=12.0,
        promo_pin=False,
        promo_silent=False,
    )
    await migrate_from_env(db, settings)

    channels = await store.list_channels(db)
    ch = channels[0]
    assert ch.trial_data_limit_gb == 10.0
    assert ch.trial_days == 7
    assert ch.on_hold_grace_days == 14
    assert ch.allow_regrant_after_days == 60
    assert ch.trial_max_member_age_days == 3.0
    assert ch.join_approval_delay_seconds == 30
    assert ch.promo_interval_hours == 12.0
    assert ch.promo_pin is False
    assert ch.promo_silent is False


# ── Full migration end-to-end ────────────────────────────────────────────────


async def test_full_migration(db):
    """Simulate a real existing single-tenant DB being migrated."""
    # Seed legacy data.
    await store.upsert_offer_group(db, 2, "NL")
    await store.upsert_offer_group(db, 5, "TR")
    await store.set_setting(db, "promo_text", "🎁 تست رایگان!")
    await store.set_setting(db, "paused", "false")
    await store.set_promo_state(db, -1001234567890, message_id=42, next_run_at=1234567890.0)

    settings = _settings(owner_tg_ids=[1])
    result = await migrate_from_env(db, settings)
    assert result is True

    # Verify everything.
    panels = await store.list_panels(db)
    assert len(panels) == 1
    assert panels[0].admin_password == "secret"

    channels = await store.list_channels(db)
    assert len(channels) == 1
    ch = channels[0]

    offers = await store.list_channel_offer_groups(db, ch.id)
    assert len(offers) == 2

    state = await get_channel_promo_state(db, ch.id)
    assert state.message_id == 42

    user = await store.get_user(db, 1)
    assert user.role == "superadmin"

    # Verify channel-scoped settings.
    assert await store.get_setting(db, f"channel:{ch.id}:promo_text") == "🎁 تست رایگان!"
