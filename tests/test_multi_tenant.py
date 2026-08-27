"""Tests for Phase 1: multi-tenant DB schema, encryption, and CRUD.

Covers:
  - storage/crypto.py (encrypt/decrypt, no-key fallback)
  - panels CRUD (create, get, list, update, soft_delete)
  - channels CRUD (create, get by id/tg_id, list, update, soft_delete)
  - users CRUD (upsert, get, list, delete, role validation)
  - channel_admins (assign, unassign, list, is_channel_admin)
  - channel_offer_groups (upsert, list, delete, clear, reorder)
  - migration (channel_id on trial_grants)
  - trial_grants channel_id field
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from storage import crypto
from storage import db as store


@pytest.fixture
async def db(tmp_path):
    conn = await store.connect(tmp_path / "test.db")
    try:
        yield conn
    finally:
        await conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# ── Encryption ─────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


class TestCrypto:
    """Test the Fernet encryption utility."""

    def setup_method(self):
        """Reset the module-level state before each test."""
        crypto._fernet = None
        crypto._loaded = False

    def teardown_method(self):
        crypto._fernet = None
        crypto._loaded = False

    def test_no_key_passthrough(self):
        """Without DB_ENCRYPTION_KEY, encrypt/decrypt are no-ops."""
        os.environ.pop("DB_ENCRYPTION_KEY", None)
        crypto._fernet = None
        crypto._loaded = False
        assert crypto.encrypt("hello") == "hello"
        assert crypto.decrypt("hello") == "hello"

    def test_with_key_roundtrip(self):
        """With a valid Fernet key, encrypt → decrypt returns the original."""
        from cryptography.fernet import Fernet

        key = Fernet.generate_key().decode()
        os.environ["DB_ENCRYPTION_KEY"] = key
        crypto._fernet = None
        crypto._loaded = False
        try:
            encrypted = crypto.encrypt("my_secret_password")
            assert encrypted != "my_secret_password"
            assert crypto.decrypt(encrypted) == "my_secret_password"
        finally:
            os.environ.pop("DB_ENCRYPTION_KEY", None)
            crypto._fernet = None
            crypto._loaded = False

    def test_with_key_ciphertext_differs_each_time(self):
        """Fernet uses a random IV, so same plaintext produces different ciphertext."""
        from cryptography.fernet import Fernet

        key = Fernet.generate_key().decode()
        os.environ["DB_ENCRYPTION_KEY"] = key
        crypto._fernet = None
        crypto._loaded = False
        try:
            e1 = crypto.encrypt("same")
            e2 = crypto.encrypt("same")
            assert e1 != e2  # different IVs
            assert crypto.decrypt(e1) == crypto.decrypt(e2) == "same"
        finally:
            os.environ.pop("DB_ENCRYPTION_KEY", None)
            crypto._fernet = None
            crypto._loaded = False

    def test_decrypt_plaintext_fallback(self):
        """If a value was stored plaintext, decrypt returns it as-is."""
        os.environ.pop("DB_ENCRYPTION_KEY", None)
        crypto._fernet = None
        crypto._loaded = False
        # Simulate reading a plaintext value (no encryption active)
        assert crypto.decrypt("plaintext_password") == "plaintext_password"


# ═══════════════════════════════════════════════════════════════════════════════
# ── Panels ─────────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


class TestPanels:
    async def test_create_and_get(self, db):
        panel = await store.create_panel(
            db,
            name="NL",
            base_url="https://nl.test",
            admin_username="admin",
            admin_password="secret123",
        )
        assert panel.id > 0
        assert panel.name == "NL"
        assert panel.active is True

        fetched = await store.get_panel(db, panel.id)
        assert fetched is not None
        assert fetched.name == "NL"
        assert fetched.base_url == "https://nl.test"
        assert fetched.admin_password == "secret123"

    async def test_create_with_custom_settings(self, db):
        panel = await store.create_panel(
            db,
            name="TR",
            base_url="https://tr.test",
            admin_username="admin",
            admin_password="pw",
            verify_ssl=False,
            timeout_seconds=30.0,
            protocols="vless,trojan",
            auto_delete_days=14,
        )
        assert panel.verify_ssl is False
        assert panel.timeout_seconds == 30.0
        assert panel.protocols == "vless,trojan"
        assert panel.auto_delete_days == 14

    async def test_list_panels(self, db):
        await store.create_panel(
            db, name="A", base_url="https://a", admin_username="a", admin_password="a"
        )
        await store.create_panel(
            db, name="B", base_url="https://b", admin_username="b", admin_password="b"
        )
        panels = await store.list_panels(db)
        assert len(panels) == 2
        assert [p.name for p in panels] == ["A", "B"]

    async def test_list_panels_excludes_inactive(self, db):
        p1 = await store.create_panel(
            db, name="A", base_url="https://a", admin_username="a", admin_password="a"
        )
        await store.create_panel(
            db, name="B", base_url="https://b", admin_username="b", admin_password="b"
        )
        await store.soft_delete_panel(db, p1.id)
        assert len(await store.list_panels(db)) == 1
        assert len(await store.list_panels(db, active_only=False)) == 2

    async def test_update_panel(self, db):
        panel = await store.create_panel(
            db,
            name="NL",
            base_url="https://old",
            admin_username="admin",
            admin_password="old_pw",
        )
        changed = await store.update_panel(
            db,
            panel.id,
            name="NL-v2",
            base_url="https://new",
            admin_password="new_pw",
        )
        assert changed is True
        fetched = await store.get_panel(db, panel.id)
        assert fetched.name == "NL-v2"
        assert fetched.base_url == "https://new"
        assert fetched.admin_password == "new_pw"

    async def test_update_nonexistent_panel(self, db):
        assert await store.update_panel(db, 9999, name="x") is False

    async def test_soft_delete_panel(self, db):
        panel = await store.create_panel(
            db,
            name="X",
            base_url="https://x",
            admin_username="x",
            admin_password="x",
        )
        assert await store.soft_delete_panel(db, panel.id) is True
        fetched = await store.get_panel(db, panel.id)
        assert fetched is not None
        assert fetched.active is False

    async def test_get_nonexistent_panel(self, db):
        assert await store.get_panel(db, 9999) is None


# ═══════════════════════════════════════════════════════════════════════════════
# ── Channels ───────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


class TestChannels:
    async def test_create_and_get(self, db):
        ch = await store.create_channel(db, tg_channel_id=-100123)
        assert ch.id > 0
        assert ch.tg_channel_id == -100123
        assert ch.active is True
        assert ch.trial_data_limit_gb == 5.0

        fetched = await store.get_channel(db, ch.id)
        assert fetched is not None
        assert fetched.tg_channel_id == -100123

    async def test_get_by_tg_id(self, db):
        await store.create_channel(db, tg_channel_id=-100999, title="My Channel")
        ch = await store.get_channel_by_tg_id(db, -100999)
        assert ch is not None
        assert ch.title == "My Channel"

    async def test_get_by_tg_id_not_found(self, db):
        assert await store.get_channel_by_tg_id(db, -999) is None

    async def test_create_with_custom_settings(self, db):
        ch = await store.create_channel(
            db,
            tg_channel_id=-1001,
            title="Test",
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
        assert ch.trial_data_limit_gb == 10.0
        assert ch.trial_days == 7
        assert ch.promo_pin is False

    async def test_list_channels(self, db):
        await store.create_channel(db, tg_channel_id=-1)
        await store.create_channel(db, tg_channel_id=-2)
        assert len(await store.list_channels(db)) == 2

    async def test_list_channels_excludes_inactive(self, db):
        ch = await store.create_channel(db, tg_channel_id=-1)
        await store.create_channel(db, tg_channel_id=-2)
        await store.soft_delete_channel(db, ch.id)
        assert len(await store.list_channels(db)) == 1
        assert len(await store.list_channels(db, active_only=False)) == 2

    async def test_update_channel(self, db):
        ch = await store.create_channel(db, tg_channel_id=-1, title="Old")
        changed = await store.update_channel(
            db,
            ch.id,
            title="New",
            trial_data_limit_gb=20.0,
            promo_pin=False,
        )
        assert changed is True
        fetched = await store.get_channel(db, ch.id)
        assert fetched.title == "New"
        assert fetched.trial_data_limit_gb == 20.0
        assert fetched.promo_pin is False

    async def test_update_nonexistent_channel(self, db):
        assert await store.update_channel(db, 9999, title="x") is False

    async def test_soft_delete_channel(self, db):
        ch = await store.create_channel(db, tg_channel_id=-1)
        assert await store.soft_delete_channel(db, ch.id) is True
        fetched = await store.get_channel(db, ch.id)
        assert fetched.active is False

    async def test_tg_channel_id_unique(self, db):
        await store.create_channel(db, tg_channel_id=-1)
        with pytest.raises(sqlite3.IntegrityError):
            await store.create_channel(db, tg_channel_id=-1)


# ═══════════════════════════════════════════════════════════════════════════════
# ── Users ──────────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


class TestUsers:
    async def test_upsert_and_get(self, db):
        user = await store.upsert_user(db, tg_user_id=42, role="superadmin", username="alice")
        assert user.tg_user_id == 42
        assert user.role == "superadmin"

        fetched = await store.get_user(db, 42)
        assert fetched is not None
        assert fetched.role == "superadmin"
        assert fetched.username == "alice"

    async def test_upsert_updates_role(self, db):
        await store.upsert_user(db, tg_user_id=42, role="user")
        await store.upsert_user(db, tg_user_id=42, role="admin")
        user = await store.get_user(db, 42)
        assert user.role == "admin"

    async def test_invalid_role_raises(self, db):
        with pytest.raises(ValueError, match="Invalid role"):
            await store.upsert_user(db, tg_user_id=42, role="moderator")

    async def test_list_users(self, db):
        await store.upsert_user(db, tg_user_id=1, role="superadmin")
        await store.upsert_user(db, tg_user_id=2, role="admin")
        await store.upsert_user(db, tg_user_id=3, role="user")
        assert len(await store.list_users(db)) == 3
        assert len(await store.list_users(db, role="admin")) == 1

    async def test_delete_user(self, db):
        await store.upsert_user(db, tg_user_id=42, role="admin")
        assert await store.delete_user(db, 42) is True
        assert await store.get_user(db, 42) is None

    async def test_delete_nonexistent_user(self, db):
        assert await store.delete_user(db, 9999) is False

    async def test_get_nonexistent_user(self, db):
        assert await store.get_user(db, 9999) is None


# ═══════════════════════════════════════════════════════════════════════════════
# ── Channel Admins ─────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


class TestChannelAdmins:
    async def test_assign_and_check(self, db):
        await store.upsert_user(db, tg_user_id=42, role="admin")
        ch = await store.create_channel(db, tg_channel_id=-1)
        await store.assign_channel_admin(db, 42, ch.id)
        assert await store.is_channel_admin(db, 42, ch.id) is True

    async def test_assign_idempotent(self, db):
        await store.upsert_user(db, tg_user_id=42, role="admin")
        ch = await store.create_channel(db, tg_channel_id=-1)
        await store.assign_channel_admin(db, 42, ch.id)
        await store.assign_channel_admin(db, 42, ch.id)  # no error
        admins = await store.list_channel_admins(db, ch.id)
        assert len(admins) == 1

    async def test_unassign(self, db):
        await store.upsert_user(db, tg_user_id=42, role="admin")
        ch = await store.create_channel(db, tg_channel_id=-1)
        await store.assign_channel_admin(db, 42, ch.id)
        assert await store.unassign_channel_admin(db, 42, ch.id) is True
        assert await store.is_channel_admin(db, 42, ch.id) is False

    async def test_unassign_nonexistent(self, db):
        assert await store.unassign_channel_admin(db, 99, 99) is False

    async def test_list_user_channels(self, db):
        await store.upsert_user(db, tg_user_id=42, role="admin")
        ch1 = await store.create_channel(db, tg_channel_id=-1)
        ch2 = await store.create_channel(db, tg_channel_id=-2)
        await store.assign_channel_admin(db, 42, ch1.id)
        await store.assign_channel_admin(db, 42, ch2.id)
        channels = await store.list_user_channels(db, 42)
        assert len(channels) == 2

    async def test_list_user_channels_excludes_inactive(self, db):
        await store.upsert_user(db, tg_user_id=42, role="admin")
        ch1 = await store.create_channel(db, tg_channel_id=-1)
        await store.create_channel(db, tg_channel_id=-2)
        await store.assign_channel_admin(db, 42, ch1.id)
        await store.soft_delete_channel(db, ch1.id)
        assert len(await store.list_user_channels(db, 42)) == 0

    async def test_list_channel_admins(self, db):
        await store.upsert_user(db, tg_user_id=1, role="superadmin")
        await store.upsert_user(db, tg_user_id=2, role="admin")
        ch = await store.create_channel(db, tg_channel_id=-1)
        await store.assign_channel_admin(db, 1, ch.id)
        await store.assign_channel_admin(db, 2, ch.id)
        admins = await store.list_channel_admins(db, ch.id)
        assert len(admins) == 2

    async def test_delete_user_removes_assignments(self, db):
        await store.upsert_user(db, tg_user_id=42, role="admin")
        ch = await store.create_channel(db, tg_channel_id=-1)
        await store.assign_channel_admin(db, 42, ch.id)
        await store.delete_user(db, 42)
        assert await store.is_channel_admin(db, 42, ch.id) is False


# ═══════════════════════════════════════════════════════════════════════════════
# ── Channel Offer Groups ───────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


class TestChannelOfferGroups:
    async def test_upsert_and_list(self, db):
        panel = await store.create_panel(
            db,
            name="NL",
            base_url="https://nl",
            admin_username="a",
            admin_password="b",
        )
        ch = await store.create_channel(db, tg_channel_id=-1)
        await store.upsert_channel_offer_group(
            db,
            channel_id=ch.id,
            panel_id=panel.id,
            group_id=2,
            label="🇳🇱 هلند",
        )
        await store.upsert_channel_offer_group(
            db,
            channel_id=ch.id,
            panel_id=panel.id,
            group_id=5,
            label="🇹🇷 ترکیه",
        )
        groups = await store.list_channel_offer_groups(db, ch.id)
        assert len(groups) == 2
        assert groups[0].label == "🇳🇱 هلند"
        assert groups[1].label == "🇹🇷 ترکیه"
        assert groups[0].panel_id == panel.id

    async def test_upsert_updates_label(self, db):
        panel = await store.create_panel(
            db,
            name="NL",
            base_url="https://nl",
            admin_username="a",
            admin_password="b",
        )
        ch = await store.create_channel(db, tg_channel_id=-1)
        await store.upsert_channel_offer_group(
            db,
            channel_id=ch.id,
            panel_id=panel.id,
            group_id=2,
            label="Old",
        )
        await store.upsert_channel_offer_group(
            db,
            channel_id=ch.id,
            panel_id=panel.id,
            group_id=2,
            label="New",
        )
        groups = await store.list_channel_offer_groups(db, ch.id)
        assert len(groups) == 1
        assert groups[0].label == "New"

    async def test_multi_panel_groups(self, db):
        """A channel can have groups from multiple panels."""
        p1 = await store.create_panel(
            db,
            name="NL",
            base_url="https://nl",
            admin_username="a",
            admin_password="b",
        )
        p2 = await store.create_panel(
            db,
            name="TR",
            base_url="https://tr",
            admin_username="a",
            admin_password="b",
        )
        ch = await store.create_channel(db, tg_channel_id=-1)
        await store.upsert_channel_offer_group(
            db,
            channel_id=ch.id,
            panel_id=p1.id,
            group_id=2,
            label="🇳🇱 هلند",
        )
        await store.upsert_channel_offer_group(
            db,
            channel_id=ch.id,
            panel_id=p2.id,
            group_id=5,
            label="🇹🇷 ترکیه",
        )
        groups = await store.list_channel_offer_groups(db, ch.id)
        assert len(groups) == 2
        panel_ids = {g.panel_id for g in groups}
        assert panel_ids == {p1.id, p2.id}

    async def test_delete_offer_group(self, db):
        panel = await store.create_panel(
            db,
            name="NL",
            base_url="https://nl",
            admin_username="a",
            admin_password="b",
        )
        ch = await store.create_channel(db, tg_channel_id=-1)
        await store.upsert_channel_offer_group(
            db,
            channel_id=ch.id,
            panel_id=panel.id,
            group_id=2,
            label="NL",
        )
        assert (
            await store.delete_channel_offer_group(
                db,
                channel_id=ch.id,
                panel_id=panel.id,
                group_id=2,
            )
            is True
        )
        assert len(await store.list_channel_offer_groups(db, ch.id)) == 0

    async def test_clear_offer_groups(self, db):
        panel = await store.create_panel(
            db,
            name="NL",
            base_url="https://nl",
            admin_username="a",
            admin_password="b",
        )
        ch = await store.create_channel(db, tg_channel_id=-1)
        await store.upsert_channel_offer_group(
            db,
            channel_id=ch.id,
            panel_id=panel.id,
            group_id=2,
            label="A",
        )
        await store.upsert_channel_offer_group(
            db,
            channel_id=ch.id,
            panel_id=panel.id,
            group_id=5,
            label="B",
        )
        count = await store.clear_channel_offer_groups(db, ch.id)
        assert count == 2
        assert len(await store.list_channel_offer_groups(db, ch.id)) == 0

    async def test_clear_only_affects_one_channel(self, db):
        panel = await store.create_panel(
            db,
            name="NL",
            base_url="https://nl",
            admin_username="a",
            admin_password="b",
        )
        ch1 = await store.create_channel(db, tg_channel_id=-1)
        ch2 = await store.create_channel(db, tg_channel_id=-2)
        await store.upsert_channel_offer_group(
            db,
            channel_id=ch1.id,
            panel_id=panel.id,
            group_id=2,
            label="A",
        )
        await store.upsert_channel_offer_group(
            db,
            channel_id=ch2.id,
            panel_id=panel.id,
            group_id=2,
            label="B",
        )
        await store.clear_channel_offer_groups(db, ch1.id)
        assert len(await store.list_channel_offer_groups(db, ch1.id)) == 0
        assert len(await store.list_channel_offer_groups(db, ch2.id)) == 1

    async def test_reorder(self, db):
        panel = await store.create_panel(
            db,
            name="NL",
            base_url="https://nl",
            admin_username="a",
            admin_password="b",
        )
        ch = await store.create_channel(db, tg_channel_id=-1)
        await store.upsert_channel_offer_group(
            db,
            channel_id=ch.id,
            panel_id=panel.id,
            group_id=2,
            label="A",
        )
        await store.upsert_channel_offer_group(
            db,
            channel_id=ch.id,
            panel_id=panel.id,
            group_id=5,
            label="B",
        )
        await store.upsert_channel_offer_group(
            db,
            channel_id=ch.id,
            panel_id=panel.id,
            group_id=9,
            label="C",
        )
        # Reorder: C, A, B
        await store.reorder_channel_offer_groups(
            db,
            ch.id,
            [(panel.id, 9), (panel.id, 2), (panel.id, 5)],
        )
        groups = await store.list_channel_offer_groups(db, ch.id)
        assert [g.group_id for g in groups] == [9, 2, 5]


# ═══════════════════════════════════════════════════════════════════════════════
# ── Migration ──────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


class TestMigration:
    async def test_channel_id_on_trial_grants(self, db):
        """New grant with channel_id set."""
        await store.record_grant(
            db,
            tg_user_id=1,
            panel_username="t1_x",
            source="join_request",
            channel_id=5,
        )
        grant = await store.get_latest_grant(db, 1)
        assert grant is not None
        assert grant.channel_id == 5

    async def test_channel_id_defaults_to_none(self, db):
        """Grant without channel_id (legacy) returns None."""
        await store.record_grant(db, tg_user_id=2, panel_username="t2_x")
        grant = await store.get_latest_grant(db, 2)
        assert grant is not None
        assert grant.channel_id is None

    async def test_old_db_gets_new_tables(self, tmp_path):
        """An old DB (without multi-tenant tables) gets them via CREATE IF NOT EXISTS."""
        import aiosqlite

        db_path = tmp_path / "old.db"
        db = await aiosqlite.connect(db_path)
        db.row_factory = aiosqlite.Row
        # Create minimal old schema.
        await db.executescript("""
            CREATE TABLE trial_grants (
                tg_user_id INTEGER PRIMARY KEY,
                panel_username TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)
        await db.commit()
        await db.close()

        # Connect via our storage — should add all new tables.
        db = await store.connect(db_path)
        try:
            # Should be able to create a panel.
            panel = await store.create_panel(
                db,
                name="Test",
                base_url="https://t",
                admin_username="a",
                admin_password="b",
            )
            assert panel.id > 0

            # Should be able to create a channel.
            ch = await store.create_channel(db, tg_channel_id=-1)
            assert ch.id > 0

            # Old grants should have channel_id = None.
            # (no old grants in this test, but the lookup must not blow up)
            await store.get_latest_grant(db, 0)
        finally:
            await db.close()


# ═══════════════════════════════════════════════════════════════════════════════
# ── Existing tests still pass (sanity) ─────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


class TestLegacyStillWorks:
    """Verify that existing single-tenant code paths are unaffected."""

    async def test_legacy_offer_groups(self, db):
        await store.upsert_offer_group(db, 2, "NL")
        await store.upsert_offer_group(db, 5, "TR")
        groups = await store.list_offer_groups(db)
        assert len(groups) == 2

    async def test_legacy_promo_state(self, db):
        await store.set_promo_state(db, -100, 1234, 999999.0)
        state = await store.get_promo_state(db)
        assert state is not None
        assert state.channel_id == -100
        assert state.message_id == 1234

    async def test_legacy_settings(self, db):
        await store.set_setting(db, "test_key", "test_value")
        assert await store.get_setting(db, "test_key") == "test_value"

    async def test_legacy_trial_grant(self, db):
        await store.record_grant(db, tg_user_id=42, panel_username="t42_x", source="start")
        grant = await store.get_latest_grant(db, 42)
        assert grant is not None
        assert grant.source == "start"
        assert grant.channel_id is None  # legacy grant has no channel
