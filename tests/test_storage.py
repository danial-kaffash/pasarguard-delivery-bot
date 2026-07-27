"""Tests for the SQLite storage layer."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from storage import db as store


@pytest.fixture
async def db(tmp_path):
    conn = await store.connect(tmp_path / "test.db")
    try:
        yield conn
    finally:
        await conn.close()


async def test_settings_roundtrip_and_default(db):
    assert await store.get_setting(db, "missing") is None
    assert await store.get_setting(db, "missing", "fallback") == "fallback"
    await store.set_setting(db, "promo_text", "hello")
    assert await store.get_setting(db, "promo_text") == "hello"
    await store.set_setting(db, "promo_text", "updated")
    assert await store.get_setting(db, "promo_text") == "updated"


async def test_promo_state_set_and_overwrite(db):
    assert await store.get_promo_state(db) is None
    await store.set_promo_state(db, channel_id=-1001, message_id=42, next_run_at=123.5)
    state = await store.get_promo_state(db)
    assert state.channel_id == -1001 and state.message_id == 42 and state.next_run_at == 123.5
    # single-row table: overwriting keeps exactly one row
    await store.set_promo_state(db, channel_id=-1001, message_id=43, next_run_at=456.0)
    state = await store.get_promo_state(db)
    assert state.message_id == 43


async def test_offer_groups_upsert_order_delete_clear(db):
    await store.upsert_offer_group(db, 2, "🇳🇱 هلند")
    await store.upsert_offer_group(db, 5, "🇹🇷 ترکیه")
    groups = await store.list_offer_groups(db)
    assert [(g.id, g.label) for g in groups] == [(2, "🇳🇱 هلند"), (5, "🇹🇷 ترکیه")]
    assert [g.sort_order for g in groups] == [0, 1]

    # update an existing entry keeps its id
    await store.upsert_offer_group(db, 2, "🇳🇱 آمستردام")
    groups = await store.list_offer_groups(db)
    assert groups[0].label == "🇳🇱 آمستردام"

    assert await store.delete_offer_group(db, 2) is True
    assert await store.delete_offer_group(db, 999) is False
    assert [g.id for g in await store.list_offer_groups(db)] == [5]

    assert await store.clear_offer_groups(db) == 1
    assert await store.list_offer_groups(db) == []


async def test_offer_groups_reorder(db):
    for gid in (2, 5, 9):
        await store.upsert_offer_group(db, gid, f"g{gid}")
    await store.reorder_offer_groups(db, [9, 2, 5])
    assert [g.id for g in await store.list_offer_groups(db)] == [9, 2, 5]


async def test_seed_from_file(tmp_path, db):
    seed = tmp_path / "offer_groups.json"
    seed.write_text(
        '[{"id": 2, "label": "🇳🇱 هلند"}, {"id": 5, "label": "🇹🇷 ترکیه"}]', encoding="utf-8"
    )
    assert await store.seed_offer_groups_from_file(db, seed) == 2
    assert [g.id for g in await store.list_offer_groups(db)] == [2, 5]

    # seeding again does nothing while the table is non-empty
    seed.write_text('[{"id": 77, "label": "x"}]', encoding="utf-8")
    assert await store.seed_offer_groups_from_file(db, seed) == 0
    assert [g.id for g in await store.list_offer_groups(db)] == [2, 5]


async def test_seed_empty_and_malformed_files(tmp_path, db):
    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    assert await store.seed_offer_groups_from_file(db, empty) == 0

    bad = tmp_path / "bad.json"
    bad.write_text('[{"id": 2, "label": "ok"}, {"nope": true}]', encoding="utf-8")
    assert await store.seed_offer_groups_from_file(db, bad) == 1
    assert [g.id for g in await store.list_offer_groups(db)] == [2]


# ── trial grants ─────────────────────────────────────────────────────────────


async def test_trial_grant_record_and_get(db):
    assert await store.get_latest_grant(db, 12345) is None
    expire = datetime(2026, 8, 7, 0, 0, tzinfo=UTC)
    await store.record_grant(
        db,
        tg_user_id=12345,
        tg_username="ali",
        panel_username="t12345_ab12cd",
        panel_user_id=101,
        group_ids=[2, 5],
        data_limit=5 * 1024**3,
        expire_at=expire,
    )
    grant = await store.get_latest_grant(db, 12345)
    assert grant.panel_username == "t12345_ab12cd"
    assert grant.group_ids == [2, 5]
    assert grant.data_limit == 5 * 1024**3
    assert grant.expire_at == expire
    assert grant.revoked is False
    assert grant.created_at.tzinfo is not None  # tz-aware for eligibility math


async def test_trial_grant_re_record_replaces_and_unrevokes(db):
    await store.record_grant(db, tg_user_id=1, panel_username="old_user")
    assert await store.revoke_grant(db, 1) is True
    assert (await store.get_latest_grant(db, 1)).revoked is True

    await store.record_grant(db, tg_user_id=1, panel_username="new_user")
    grant = await store.get_latest_grant(db, 1)
    assert grant.panel_username == "new_user"
    assert grant.revoked is False
    assert await store.revoke_grant(db, 999) is False


async def test_list_grants(db):
    await store.record_grant(db, tg_user_id=1, panel_username="a")
    await store.record_grant(db, tg_user_id=2, panel_username="b")
    grants = await store.list_grants(db)
    assert {g.tg_user_id for g in grants} == {1, 2}


# ── chat members & events ────────────────────────────────────────────────────


async def test_chat_members_upsert_and_count(db):
    await store.upsert_chat_member(db, -100, 1, "member")
    await store.upsert_chat_member(db, -100, 2, "administrator")
    await store.upsert_chat_member(db, -100, 3, "left")
    assert await store.count_chat_members(db, -100) == 2
    # status update replaces, doesn't duplicate
    await store.upsert_chat_member(db, -100, 1, "left")
    assert await store.count_chat_members(db, -100) == 1


async def test_member_events_count_with_since(db):
    from datetime import UTC, datetime, timedelta

    await store.record_member_event(db, -100, 1, "join")
    await store.record_member_event(db, -100, 2, "join")
    await store.record_member_event(db, -100, 1, "leave")

    since = datetime.now(UTC) - timedelta(minutes=1)
    assert await store.count_member_events(db, "join", since) == 2
    assert await store.count_member_events(db, "leave", since) == 1
    future = datetime.now(UTC) + timedelta(minutes=1)
    assert await store.count_member_events(db, "join", future) == 0
