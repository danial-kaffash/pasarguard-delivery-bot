"""Channel join-request handler — now multi-tenant aware.

When a user sends a join request to a channel managed by the bot:
1. Resolve the channel from the DB (by Telegram channel id).
2. Check eligibility (same rules as /start).
3. If eligible → create a trial, DM the subscription link, then approve after a delay.
4. If already has a trial → approve immediately and optionally resend the sub link.
5. If in cooldown → approve immediately, mention the cooldown.
6. If joins_paused → approve immediately without a trial.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from html import escape as html_escape

import aiosqlite
from aiogram import Bot, Router
from aiogram.types import ChatJoinRequest

from panel.exceptions import PanelError
from panel.manager import PanelManager
from services import trial as trial_service
from services.channel_settings import ChannelSettings
from storage import db as store
from storage.db import ChannelOfferGroup

from .. import texts
from ..pause import is_joins_paused

logger = logging.getLogger(__name__)

router = Router(name="join_request")

# Key stored in the settings table for channel-scoped runtime override.
# Format: "channel:{channel_db_id}:join_approval_delay_seconds"
JOIN_DELAY_KEY_SUFFIX = "join_approval_delay_seconds"
# Legacy key (used by admin commands before they're updated).
JOIN_DELAY_KEY = JOIN_DELAY_KEY_SUFFIX


async def get_join_delay(
    db: aiosqlite.Connection, default: int, channel_db_id: int | None = None
) -> int:
    """Runtime override (via /setjoindelay) beats the channel default.

    When channel_db_id is given, checks the channel-scoped key first
    (``channel:{id}:join_approval_delay_seconds``), then falls back to the
    global key for backward compatibility.
    """
    if channel_db_id is not None:
        key = f"channel:{channel_db_id}:{JOIN_DELAY_KEY_SUFFIX}"
        raw = await store.get_setting(db, key)
        if raw is not None:
            try:
                return max(0, int(raw))
            except ValueError:
                pass
    # Fallback: global key (legacy admin commands).
    raw = await store.get_setting(db, JOIN_DELAY_KEY)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid join delay setting %r — using default %s", raw, default)
        return default
    return value if value >= 0 else default


def _display_name(user) -> str:
    """HTML-escaped display name for a Telegram user."""
    return html_escape(user.first_name or "دوست عزیز")


def _labels_for(ids: list[int], offers: list[ChannelOfferGroup]) -> str:
    label_map = {o.group_id: o.label for o in offers}
    return "، ".join(label_map.get(i, f"#{i}") for i in ids)


@router.chat_join_request()
async def on_join_request(
    event: ChatJoinRequest,
    bot: Bot,
    db: aiosqlite.Connection,
    panel_manager: PanelManager,
) -> None:
    """Handle a channel join request: deliver trial config then approve."""
    # Resolve the channel from the DB.
    channel = await store.get_channel_by_tg_id(db, event.chat.id)
    if channel is None:
        return  # not one of our channels

    user = event.from_user
    name = _display_name(user)
    user_id = user.id

    # Record the join-request event for stats.
    await store.record_member_event(db, event.chat.id, user_id, "join_request")
    logger.info(
        "Join request from tg_id=%s (%s) on channel %s",
        user_id,
        user.username or "no_username",
        event.chat.id,
    )

    # ── If joins are paused → approve immediately, no trial. ────────────────
    if await is_joins_paused(db):
        try:
            await event.approve()
            logger.info("Joins paused — approved tg_id=%s immediately (no trial).", user_id)
        except Exception:
            logger.exception("Failed to approve join request for tg_id=%s (joins_paused)", user_id)
        return

    # Resolve the channel's first offer group to determine the panel.
    channel_offers = await store.list_channel_offer_groups(db, channel.id)
    if not channel_offers:
        # No offer groups — just approve.
        try:
            await event.approve()
            logger.info("No offer groups — approved tg_id=%s without trial.", user_id)
        except Exception:
            logger.exception("Failed to approve join request for tg_id=%s", user_id)
        return

    # Use the first offer group's panel as the primary panel.
    primary_panel_row = await store.get_panel(db, channel_offers[0].panel_id)
    if primary_panel_row is None:
        try:
            await event.approve()
        except Exception:
            pass
        return

    settings = ChannelSettings(channel, primary_panel_row)
    panel = panel_manager.get_client(primary_panel_row)

    # ── Check eligibility ───────────────────────────────────────────────────
    grant = await store.get_latest_grant(db, user_id)
    eligibility = trial_service.check_eligibility(grant, settings)

    delay = await get_join_delay(db, settings.join_approval_delay_seconds, channel_db_id=channel.id)

    # User already has an active trial — approve immediately, resend sub link.
    if not eligibility.eligible and eligibility.reason == "active":
        sub_url = None
        if grant and grant.panel_username:
            try:
                panel_user = await panel.get_user(grant.panel_username)
                sub_url = panel_user.subscription_url or None
            except PanelError as exc:
                logger.warning(
                    "Could not re-fetch existing trial %s: %s", grant.panel_username, exc
                )

        if sub_url:
            await _safe_dm(
                bot,
                user_id,
                texts.JOIN_REQUEST_ALREADY_GRANTED.format(name=name, sub_url=sub_url),
            )
        try:
            await event.approve()
            logger.info("Approved tg_id=%s (already has active trial).", user_id)
        except Exception:
            logger.exception("Failed to approve join request for tg_id=%s", user_id)
        return

    # User is in cooldown — approve immediately, mention cooldown.
    if not eligibility.eligible and eligibility.reason == "cooldown":
        await _safe_dm(
            bot,
            user_id,
            texts.JOIN_REQUEST_COOLDOWN.format(name=name, days=eligibility.retry_after_days or 0),
        )
        try:
            await event.approve()
            logger.info("Approved tg_id=%s (in cooldown).", user_id)
        except Exception:
            logger.exception("Failed to approve join request for tg_id=%s", user_id)
        return

    # ── User is eligible — create trial ─────────────────────────────────────
    offers, _stale = await trial_service.get_channel_offered_groups(
        panel_manager,
        db,
        channel.id,
    )
    if not offers:
        await _safe_dm(bot, user_id, texts.NO_GROUPS_AVAILABLE)
        try:
            await event.approve()
            logger.info("No valid offer groups — approved tg_id=%s without trial.", user_id)
        except Exception:
            logger.exception("Failed to approve join request for tg_id=%s", user_id)
        return

    # Use the first offer group for the trial (single-select).
    selected_offer = offers[0]
    selected_ids = [selected_offer.group_id]

    # Get the panel for this specific offer group.
    offer_panel_row = await store.get_panel(db, selected_offer.panel_id)
    if offer_panel_row is None:
        await _safe_dm(bot, user_id, texts.ERROR_TRY_AGAIN)
        try:
            await event.approve()
        except Exception:
            pass
        return

    offer_panel = panel_manager.get_client(offer_panel_row)
    offer_settings = ChannelSettings(channel, offer_panel_row)

    try:
        panel_user, username = await trial_service.create_trial(
            offer_panel, settings=offer_settings, tg_user_id=user_id, group_ids=selected_ids
        )
    except PanelError:
        logger.exception("Trial creation failed for join-request tg_id=%s", user_id)
        await _safe_dm(bot, user_id, texts.ERROR_TRY_AGAIN)
        try:
            await event.approve()
        except Exception:
            logger.exception(
                "Failed to approve join request for tg_id=%s after panel error", user_id
            )
        return

    # Record the grant.
    expire_at = datetime.now(UTC) + timedelta(
        days=offer_settings.on_hold_grace_days + offer_settings.trial_days
    )
    await store.record_grant(
        db,
        tg_user_id=user_id,
        tg_username=user.username,
        panel_username=username,
        panel_user_id=panel_user.id,
        group_ids=selected_ids,
        data_limit=offer_settings.trial_data_limit_bytes,
        expire_at=expire_at,
        source_chat_id=event.chat.id,
        source="join_request",
        channel_id=channel.id,
    )

    # DM the trial config.
    if panel_user.subscription_url:
        await _safe_dm(
            bot,
            user_id,
            texts.JOIN_REQUEST_DELIVERY.format(
                name=name,
                gb=offer_settings.trial_data_limit_gb,
                sub_url=panel_user.subscription_url,
                trial_days=offer_settings.trial_days,
                grace_days=offer_settings.on_hold_grace_days,
                group_labels=_labels_for(selected_ids, offers),
                delay=delay,
            ),
        )
    else:
        logger.warning("Panel returned no subscription_url for %s", username)
        await _safe_dm(
            bot,
            user_id,
            texts.JOIN_REQUEST_DELIVERY_NO_SUB_URL.format(
                name=name, username=username, delay=delay
            ),
        )

    logger.info(
        "Granted join-request trial %s to tg_id=%s (groups=%s, channel=%s)",
        username,
        user_id,
        selected_ids,
        channel.tg_channel_id,
    )

    # ── Delayed approval ────────────────────────────────────────────────────
    if delay > 0:
        await asyncio.sleep(delay)

    try:
        await event.approve()
        logger.info("Approved join request for tg_id=%s after %ds delay.", user_id, delay)
    except Exception:
        logger.exception("Failed to approve join request for tg_id=%s after delay", user_id)

    await _safe_dm(bot, user_id, texts.JOIN_REQUEST_APPROVED)


async def _safe_dm(bot: Bot, user_id: int, text: str) -> None:
    """Send a DM to a user, silently ignoring failures."""
    try:
        await bot.send_message(user_id, text)
    except Exception:
        logger.warning(
            "Could not DM tg_id=%s (user may have blocked the bot or DM window expired).", user_id
        )
