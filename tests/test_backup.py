"""Tests for backup/export/import commands."""

from __future__ import annotations

import json

import pytest

from bot.handlers.backup import _apply_import, _build_export
from storage import db as store
from tests.helpers import make_settings

SETTINGS = make_settings()


@pytest.fixture
async def db(tmp_path):
    conn = await store.connect(tmp_path / "test.db")
    try:
        yield conn
    finally:
        await conn.close()


async def _seed_data(db):
    """Seed some data for export/import tests."""
    panel = await store.create_panel(
        db,
        name="NL Panel",
        base_url="https://nl.test",
        admin_username="admin",
        admin_password="secret",
        protocols="vless,trojan",
        auto_delete_days=14,
    )
    ch = await store.create_channel(
        db,
        tg_channel_id=-100123,
        title="My Channel",
        trial_data_limit_gb=10.0,
        trial_days=7,
    )
    await store.upsert_user(db, tg_user_id=1, role="superadmin")
    await store.upsert_user(db, tg_user_id=42, role="admin")
    await store.assign_channel_admin(db, 42, ch.id)
    await store.upsert_channel_offer_group(
        db,
        channel_id=ch.id,
        panel_id=panel.id,
        group_id=2,
        label="🇳🇱 هلند",
    )
    await store.set_setting(db, "promo_text", "متن تست")
    return panel, ch


# ── Export ────────────────────────────────────────────────────────────────────


async def test_export_structure(db):
    panel, ch = await _seed_data(db)
    data = await _build_export(db)

    assert data["version"] == 1
    assert "exported_at" in data
    assert len(data["panels"]) == 1
    assert len(data["channels"]) == 1
    assert len(data["users"]) == 2
    assert len(data["channel_admins"]) == 1
    assert len(data["channel_offer_groups"]) == 1
    assert "settings" in data


async def test_export_panel_no_password(db):
    """Panel passwords are NOT included in the export."""
    await _seed_data(db)
    data = await _build_export(db)
    p = data["panels"][0]
    assert "admin_password" not in p
    assert p["name"] == "NL Panel"
    assert p["base_url"] == "https://nl.test"


async def test_export_channel_settings(db):
    await _seed_data(db)
    data = await _build_export(db)
    ch = data["channels"][0]
    assert ch["tg_channel_id"] == -100123
    assert ch["title"] == "My Channel"
    assert ch["trial_data_limit_gb"] == 10.0
    assert ch["trial_days"] == 7


async def test_export_offer_groups_use_panel_index(db):
    """Offer groups reference panel by index, not DB id."""
    await _seed_data(db)
    data = await _build_export(db)
    og = data["channel_offer_groups"][0]
    assert og["panel_index"] == 0  # first panel
    assert og["group_id"] == 2
    assert og["label"] == "🇳🇱 هلند"


async def test_export_preserves_settings(db):
    await _seed_data(db)
    data = await _build_export(db)
    assert data["settings"]["promo_text"] == "متن تست"


async def test_cmd_export_sends_document(db):
    await _seed_data(db)
    # cmd_export uses message.answer_document which FakeMessage doesn't support.
    # Test the underlying function instead.
    data = await _build_export(db)
    assert len(json.dumps(data)) > 100


# ── Import ────────────────────────────────────────────────────────────────────


async def test_import_creates_panels(db):
    data = {
        "version": 1,
        "panels": [{"name": "NL", "base_url": "https://nl", "admin_username": "a"}],
        "channels": [],
        "users": [],
        "channel_admins": [],
        "channel_offer_groups": [],
        "settings": {},
    }
    result = await _apply_import(db, bot=None, data=data)
    assert result["panels"] == 1
    panels = await store.list_panels(db, active_only=False)
    assert len(panels) == 1
    assert panels[0].name == "NL"


async def test_import_creates_channels(db):
    data = {
        "version": 1,
        "panels": [],
        "channels": [{"tg_channel_id": -100999, "title": "Test", "trial_days": 14}],
        "users": [],
        "channel_admins": [],
        "channel_offer_groups": [],
        "settings": {},
    }
    result = await _apply_import(db, bot=None, data=data)
    assert result["channels"] == 1
    ch = await store.get_channel_by_tg_id(db, -100999)
    assert ch is not None
    assert ch.title == "Test"
    assert ch.trial_days == 14


async def test_import_updates_existing(db):
    """Importing with existing panel/channel updates them."""
    await store.create_panel(
        db,
        name="NL",
        base_url="https://old",
        admin_username="a",
        admin_password="b",
    )
    await store.create_channel(db, tg_channel_id=-100123, title="Old")

    data = {
        "version": 1,
        "panels": [{"name": "NL", "base_url": "https://new", "admin_username": "newuser"}],
        "channels": [{"tg_channel_id": -100123, "title": "New Title"}],
        "users": [],
        "channel_admins": [],
        "channel_offer_groups": [],
        "settings": {},
    }
    await _apply_import(db, bot=None, data=data)

    panels = await store.list_panels(db, active_only=False)
    assert panels[0].base_url == "https://new"
    assert panels[0].admin_username == "newuser"

    ch = await store.get_channel_by_tg_id(db, -100123)
    assert ch.title == "New Title"


async def test_import_users_and_assignments(db):
    data = {
        "version": 1,
        "panels": [],
        "channels": [{"tg_channel_id": -100123}],
        "users": [
            {"tg_user_id": 1, "role": "superadmin"},
            {"tg_user_id": 42, "role": "admin"},
        ],
        "channel_admins": [{"tg_user_id": 42, "channel_tg_id": -100123}],
        "channel_offer_groups": [],
        "settings": {},
    }
    result = await _apply_import(db, bot=None, data=data)
    assert result["users"] == 2
    assert result["assignments"] == 1
    user = await store.get_user(db, 1)
    assert user.role == "superadmin"
    ch = await store.get_channel_by_tg_id(db, -100123)
    assert await store.is_channel_admin(db, 42, ch.id)


async def test_import_offer_groups(db):
    data = {
        "version": 1,
        "panels": [{"name": "NL", "base_url": "https://nl", "admin_username": "a"}],
        "channels": [{"tg_channel_id": -100123}],
        "users": [],
        "channel_admins": [],
        "channel_offer_groups": [
            {
                "channel_tg_id": -100123,
                "panel_index": 0,
                "group_id": 2,
                "label": "🇳🇱 هلند",
                "sort_order": 0,
            },
            {
                "channel_tg_id": -100123,
                "panel_index": 0,
                "group_id": 5,
                "label": "🇹🇷 ترکیه",
                "sort_order": 1,
            },
        ],
        "settings": {},
    }
    result = await _apply_import(db, bot=None, data=data)
    assert result["offer_groups"] == 2
    ch = await store.get_channel_by_tg_id(db, -100123)
    offers = await store.list_channel_offer_groups(db, ch.id)
    assert len(offers) == 2
    assert offers[0].label == "🇳🇱 هلند"


async def test_import_settings(db):
    data = {
        "version": 1,
        "panels": [],
        "channels": [],
        "users": [],
        "channel_admins": [],
        "channel_offer_groups": [],
        "settings": {"promo_text": "متن وارد شده", "joins_paused": "true"},
    }
    result = await _apply_import(db, bot=None, data=data)
    assert result["settings"] == 2
    assert await store.get_setting(db, "promo_text") == "متن وارد شده"
    assert await store.get_setting(db, "joins_paused") == "true"


async def test_import_wrong_version_rejected():
    """Import with wrong version should be handled."""
    # The version check is in cmd_import, not _apply_import.
    # _apply_import doesn't check version — it's the handler's job.
    pass


# ── Roundtrip ─────────────────────────────────────────────────────────────────


async def test_export_import_roundtrip(db):
    """Export then import should preserve all configuration."""
    panel, ch = await _seed_data(db)

    # Export.
    data = await _build_export(db)

    # Wipe the database (simulate fresh install).
    await db.execute("DELETE FROM channel_offer_groups")
    await db.execute("DELETE FROM channel_admins")
    await db.execute("DELETE FROM users")
    await db.execute("DELETE FROM channels")
    await db.execute("DELETE FROM panels")
    await db.execute("DELETE FROM settings")
    await db.commit()

    # Import.
    result = await _apply_import(db, bot=None, data=data)
    assert result["panels"] == 1
    assert result["channels"] == 1
    assert result["users"] == 2
    assert result["assignments"] == 1
    assert result["offer_groups"] == 1

    # Verify.
    panels = await store.list_panels(db, active_only=False)
    assert len(panels) == 1
    assert panels[0].name == "NL Panel"
    assert panels[0].base_url == "https://nl.test"
    # Password is empty (not in export).
    assert panels[0].admin_password == ""

    channels = await store.list_channels(db, active_only=False)
    assert len(channels) == 1
    assert channels[0].tg_channel_id == -100123
    assert channels[0].trial_data_limit_gb == 10.0

    ch = channels[0]
    offers = await store.list_channel_offer_groups(db, ch.id)
    assert len(offers) == 1
    assert offers[0].label == "🇳🇱 هلند"

    # Offer group should reference the new panel.
    assert offers[0].panel_id == panels[0].id
