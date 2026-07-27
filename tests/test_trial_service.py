"""Tests for services.trial — the trial business logic."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from panel.exceptions import PanelConflictError
from panel.models import UserResponse
from services import trial as ts
from storage import db as store

USERNAME_RE = re.compile(r"^t\d+_[a-z0-9]{6}$")


def make_settings(**overrides):
    base = {
        "trial_data_limit_gb": 5,
        "trial_days": 3,
        "on_hold_grace_days": 7,
        "trial_protocol_list": ["vless"],
        "auto_delete_days": 11,
        "allow_regrant_after_days": 30,
    }
    base.update(overrides)
    settings = SimpleNamespace(**base)
    settings.trial_data_limit_bytes = int(base["trial_data_limit_gb"] * 1024**3)
    return settings


def make_panel_user(username: str, sub_url: str = "https://panel.test/sub/abc/") -> UserResponse:
    return UserResponse(
        id=101,
        username=username,
        status="on_hold",
        used_traffic=0,
        created_at=datetime.now(UTC),
        subscription_url=sub_url,
    )


class FakePanel:
    def __init__(self, groups=None, create_side_effects=None):
        self._groups = groups or []
        self.groups_calls = 0
        self.create_calls = []
        self._create_side_effects = list(create_side_effects or [])

    async def list_groups_simple(self):
        self.groups_calls += 1
        return [SimpleNamespace(id=i, name=n) for i, n in self._groups]

    async def create_user(self, user):
        self.create_calls.append(user)
        if self._create_side_effects:
            effect = self._create_side_effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
            return effect
        return make_panel_user(user.username)


@pytest.fixture
async def db(tmp_path):
    conn = await store.connect(tmp_path / "test.db")
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture(autouse=True)
def _fresh_cache():
    ts.reset_panel_groups_cache()
    yield
    ts.reset_panel_groups_cache()


# ── username & payload ───────────────────────────────────────────────────────


def test_generate_panel_username_format_and_uniqueness():
    names = {ts.generate_panel_username(123456789) for _ in range(200)}
    assert len(names) > 190  # random suffixes effectively unique
    for name in names:
        assert USERNAME_RE.match(name)
        assert name.startswith("t123456789_")


def test_build_trial_user_on_hold_payload():
    settings = make_settings()
    now = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    user = ts.build_trial_user(
        settings=settings, username="t42_ab12cd", tg_user_id=42, group_ids=[2, 5], now=now
    )
    payload = user.to_payload()
    assert payload["status"] == "on_hold"
    assert payload["data_limit"] == 5 * 1024**3
    assert payload["data_limit_reset_strategy"] == "no_reset"
    assert payload["on_hold_expire_duration"] == 3 * 86400
    assert payload["on_hold_timeout"] == "2026-08-03T12:00:00Z"  # now + 7 days
    assert payload["group_ids"] == [2, 5]
    assert payload["proxy_settings"] == {"vless": {}}
    assert payload["note"] == "telegram-greet-bot tg_id=42"
    assert payload["auto_delete_in_days"] == 11


# ── eligibility ──────────────────────────────────────────────────────────────


def _grant(created_days_ago: float, revoked: bool = False) -> store.TrialGrant:
    return store.TrialGrant(
        tg_user_id=1,
        panel_username="t1_x",
        created_at=datetime.now(UTC) - timedelta(days=created_days_ago),
        revoked=revoked,
    )


def test_eligibility_no_grant():
    assert ts.check_eligibility(None, make_settings()).eligible


def test_eligibility_active_grant_blocks():
    elig = ts.check_eligibility(_grant(1), make_settings())  # lifetime = 10 days
    assert not elig.eligible and elig.reason == "active"


def test_eligibility_expired_within_cooldown():
    elig = ts.check_eligibility(_grant(15), make_settings())  # expired, 30-day cooldown
    assert not elig.eligible and elig.reason == "cooldown"
    assert 14 < elig.retry_after_days < 16


def test_eligibility_after_cooldown_ok():
    assert ts.check_eligibility(_grant(40), make_settings()).eligible


def test_eligibility_revoked_recent_still_in_cooldown():
    elig = ts.check_eligibility(_grant(1, revoked=True), make_settings())
    assert not elig.eligible and elig.reason == "cooldown"


# ── trial creation ───────────────────────────────────────────────────────────


async def test_create_trial_success():
    panel = FakePanel()
    user, username = await ts.create_trial(
        panel, settings=make_settings(), tg_user_id=7, group_ids=[2]
    )
    assert USERNAME_RE.match(username)
    assert user.username == username
    assert len(panel.create_calls) == 1


async def test_create_trial_conflict_retry_succeeds():
    conflict = PanelConflictError(409, "User already exists", method="POST", path="/api/user")
    panel = FakePanel(create_side_effects=[conflict])
    user, username = await ts.create_trial(
        panel, settings=make_settings(), tg_user_id=7, group_ids=[2]
    )
    assert len(panel.create_calls) == 2
    assert panel.create_calls[0].username != panel.create_calls[1].username  # fresh username
    assert user.username == username == panel.create_calls[1].username


async def test_create_trial_conflict_twice_raises():
    conflict = PanelConflictError(409, "dup", method="POST", path="/api/user")
    panel = FakePanel(create_side_effects=[conflict, conflict])
    with pytest.raises(PanelConflictError):
        await ts.create_trial(panel, settings=make_settings(), tg_user_id=7, group_ids=[2])
    assert len(panel.create_calls) == 2


# ── offered groups ───────────────────────────────────────────────────────────


async def test_offered_groups_empty_list_skips_panel_call(db):
    panel = FakePanel(groups=[(2, "a")])
    valid, stale = await ts.get_offered_groups(panel, db)
    assert (valid, stale) == ([], [])
    assert panel.groups_calls == 0


async def test_offered_groups_filters_stale_ids_and_caches(db):
    await store.upsert_offer_group(db, 2, "🇳🇱 هلند")
    await store.upsert_offer_group(db, 5, "🇹🇷 ترکیه")
    await store.upsert_offer_group(db, 99, "👻 حذف‌شده")

    panel = FakePanel(groups=[(2, "NL"), (5, "TR")])
    valid, stale = await ts.get_offered_groups(panel, db)
    assert [o.id for o in valid] == [2, 5]
    assert stale == [99]
    assert panel.groups_calls == 1

    # second call hits the cache — no extra panel round-trip
    valid2, _ = await ts.get_offered_groups(panel, db)
    assert [o.id for o in valid2] == [2, 5]
    assert panel.groups_calls == 1
