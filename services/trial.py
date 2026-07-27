"""Trial business logic: eligibility, panel-user creation, group caching.

Kept free of aiogram imports so it can be unit-tested with plain fakes.
"""

from __future__ import annotations

import logging
import secrets
import string
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import aiosqlite

from panel.client import PasarGuardApiClient
from panel.exceptions import PanelConflictError
from panel.models import (
    DataLimitResetStrategy,
    UserCreate,
    UserResponse,
    UserStatusCreate,
)
from storage import db as store

logger = logging.getLogger(__name__)

_USERNAME_ALPHABET = string.ascii_lowercase + string.digits
_USERNAME_RETRIES = 2
PANEL_GROUPS_CACHE_TTL = 300.0  # seconds

# module-level cache: (monotonic timestamp, {panel group id: panel group name})
_groups_cache: tuple[float, dict[int, str]] | None = None


def reset_panel_groups_cache() -> None:
    """Mainly for tests."""
    global _groups_cache
    _groups_cache = None


# ── username & payload construction ─────────────────────────────────────────


def generate_panel_username(tg_user_id: int) -> str:
    """e.g. t123456789_ab12cd — safe for the panel's username rules."""
    suffix = "".join(secrets.choice(_USERNAME_ALPHABET) for _ in range(6))
    return f"t{tg_user_id}_{suffix}"


def build_trial_user(
    *,
    settings,
    username: str,
    tg_user_id: int,
    group_ids: list[int],
    now: datetime | None = None,
) -> UserCreate:
    """Build the on-hold 5 GB trial payload (PLAN.md §5.3)."""
    now = now or datetime.now(UTC)
    return UserCreate(
        username=username,
        data_limit=settings.trial_data_limit_bytes,
        data_limit_reset_strategy=DataLimitResetStrategy.NO_RESET,
        status=UserStatusCreate.ON_HOLD,
        on_hold_expire_duration=settings.trial_days * 86400,
        on_hold_timeout=now + timedelta(days=settings.on_hold_grace_days),
        group_ids=group_ids,
        proxy_settings={protocol: {} for protocol in settings.trial_protocol_list},
        note=f"telegram-greet-bot tg_id={tg_user_id}",
        auto_delete_in_days=settings.auto_delete_days,
    )


# ── eligibility ──────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Eligibility:
    eligible: bool
    reason: str | None = None  # "active" | "cooldown"
    grant: store.TrialGrant | None = None
    retry_after_days: float | None = None


def check_eligibility(
    grant: store.TrialGrant | None,
    settings,
    now: datetime | None = None,
) -> Eligibility:
    """One trial per user; re-grant allowed after the cooldown window."""
    now = now or datetime.now(UTC)
    if grant is None:
        return Eligibility(eligible=True)

    lifetime = timedelta(days=settings.on_hold_grace_days + settings.trial_days)
    if not grant.revoked and grant.created_at + lifetime > now:
        return Eligibility(eligible=False, reason="active", grant=grant)

    cooldown_end = grant.created_at + timedelta(days=settings.allow_regrant_after_days)
    if cooldown_end > now:
        remaining = (cooldown_end - now).total_seconds() / 86400
        return Eligibility(
            eligible=False, reason="cooldown", grant=grant, retry_after_days=remaining
        )
    return Eligibility(eligible=True, grant=grant)


# ── offered groups (curated list, validated against the panel) ──────────────


async def fetch_panel_groups_map(panel: PasarGuardApiClient, force: bool = False) -> dict[int, str]:
    """GET /api/groups/simple with a 5-minute in-memory cache."""
    global _groups_cache
    if (
        not force
        and _groups_cache
        and (time.monotonic() - _groups_cache[0]) < PANEL_GROUPS_CACHE_TTL
    ):
        return _groups_cache[1]
    groups = await panel.list_groups_simple()
    mapping = {g.id: g.name for g in groups}
    _groups_cache = (time.monotonic(), mapping)
    return mapping


async def get_offered_groups(
    panel: PasarGuardApiClient, db: aiosqlite.Connection
) -> tuple[list[store.OfferGroup], list[int]]:
    """The curated offer list, minus ids the panel no longer knows about.

    Returns (valid offer groups in display order, stale ids that were skipped).
    """
    offers = await store.list_offer_groups(db)
    if not offers:
        return [], []
    panel_map = await fetch_panel_groups_map(panel)
    valid = [o for o in offers if o.id in panel_map]
    stale = [o.id for o in offers if o.id not in panel_map]
    if stale:
        logger.warning("Offer list contains ids missing from the panel: %s", stale)
    return valid, stale


# ── trial creation with conflict retry ──────────────────────────────────────


async def create_trial(
    panel: PasarGuardApiClient,
    *,
    settings,
    tg_user_id: int,
    group_ids: list[int],
) -> tuple[UserResponse, str]:
    """Create the trial on the panel; retry once with a fresh username on 409."""
    last_error: PanelConflictError | None = None
    for _ in range(_USERNAME_RETRIES):
        username = generate_panel_username(tg_user_id)
        user = build_trial_user(
            settings=settings, username=username, tg_user_id=tg_user_id, group_ids=group_ids
        )
        try:
            return await panel.create_user(user), username
        except PanelConflictError as exc:
            logger.warning("Username %s collided (%s); regenerating.", username, exc.detail)
            last_error = exc
    assert last_error is not None
    raise last_error
