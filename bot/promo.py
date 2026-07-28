"""Channel promo poster + scheduler — multi-channel version.

Each active channel gets its own promo post cycle:
- Own interval, pin, silent settings (from channels table)
- Own promo text (channel:{id}:promo_text setting, falling back to global)
- Own promo state (last message id, next run time)
- Own pause flag (channel:{id}:paused)

The scheduler manages all channels concurrently.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import aiosqlite
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from storage import db as store

from .pause import is_channel_paused, is_paused

logger = logging.getLogger(__name__)

DEFAULT_PROMO_FILE = Path(__file__).resolve().parent.parent / "texts" / "promo_fa.txt"

# Legacy global keys (still used by old code paths during migration).
PROMO_TEXT_KEY = "promo_text"
PROMO_INTERVAL_KEY = "promo_interval_hours"

# Channel-scoped key patterns.
_CH_PROMO_TEXT = "channel:{cid}:promo_text"
_CH_PROMO_INTERVAL = "channel:{cid}:promo_interval_hours"

STARTUP_GRACE_SECONDS = 15.0
RETRY_BACKOFF_SECONDS = 60.0
PAUSE_POLL_SECONDS = 60.0
CHANNEL_SCAN_INTERVAL = 120.0  # re-scan channels every 2 minutes


# ── content resolution ───────────────────────────────────────────────────────


def _default_promo_text() -> str:
    if DEFAULT_PROMO_FILE.exists():
        return DEFAULT_PROMO_FILE.read_text(encoding="utf-8").strip()
    return "🎁 تست رایگان ۵ گیگابایتی! برای دریافت روی دکمهٔ زیر بزنید."


async def get_promo_text(db: aiosqlite.Connection) -> str:
    """Global promo text (legacy)."""
    override = await store.get_setting(db, PROMO_TEXT_KEY)
    return override if override else _default_promo_text()


async def get_channel_promo_text(db: aiosqlite.Connection, channel_db_id: int) -> str:
    """Channel-scoped promo text, falling back to global."""
    key = _CH_PROMO_TEXT.format(cid=channel_db_id)
    override = await store.get_setting(db, key)
    if override:
        return override
    return await get_promo_text(db)


async def get_interval_hours(db: aiosqlite.Connection, default: float) -> float:
    """Global interval (legacy)."""
    raw = await store.get_setting(db, PROMO_INTERVAL_KEY)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid promo interval setting %r — using default %s", raw, default)
        return default
    return value if value > 0 else default


async def get_channel_interval(db: aiosqlite.Connection, channel_db_id: int, default: float) -> float:
    """Channel-scoped interval, falling back to global."""
    key = _CH_PROMO_INTERVAL.format(cid=channel_db_id)
    raw = await store.get_setting(db, key)
    if raw is not None:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return await get_interval_hours(db, default)


def build_keyboard(deep_link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🎁 دریافت تست ۵ گیگ", url=deep_link)]]
    )


# ── per-channel promo state ─────────────────────────────────────────────────


async def get_channel_promo_state(
    db: aiosqlite.Connection, channel_db_id: int
) -> store.PromoState | None:
    """Get the promo state for a specific channel."""
    rows = await db.execute_fetchall(
        "SELECT channel_id, message_id, next_run_at FROM promo_state "
        "WHERE channel_id = ? ORDER BY id DESC LIMIT 1",
        (channel_db_id,),
    )
    if not rows:
        return None
    r = rows[0]
    return store.PromoState(
        channel_id=r["channel_id"], message_id=r["message_id"], next_run_at=r["next_run_at"]
    )


async def set_channel_promo_state(
    db: aiosqlite.Connection,
    channel_db_id: int,
    message_id: int,
    next_run_at: float,
) -> None:
    """Set the promo state for a specific channel (upsert)."""
    existing = await db.execute_fetchall(
        "SELECT id FROM promo_state WHERE channel_id = ?", (channel_db_id,)
    )
    now = store._now()
    if existing:
        await db.execute(
            "UPDATE promo_state SET message_id = ?, next_run_at = ?, updated_at = ? "
            "WHERE channel_id = ?",
            (message_id, next_run_at, now, channel_db_id),
        )
    else:
        await db.execute(
            "INSERT INTO promo_state (channel_id, message_id, next_run_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (channel_db_id, message_id, next_run_at, now),
        )
    await db.commit()


# ── publishing ───────────────────────────────────────────────────────────────


async def publish_promo(
    bot: Bot,
    db: aiosqlite.Connection,
    *,
    channel_id: int,
    pin: bool,
    silent: bool,
    text: str | None = None,
    channel_db_id: int | None = None,
) -> int:
    """Replace the previous promo post for a channel; return the new message id.

    When channel_db_id is given, uses per-channel promo state.
    When text is given, uses it; otherwise fetches channel/global text.
    """
    # Resolve promo text.
    if text is None:
        if channel_db_id is not None:
            text = await get_channel_promo_text(db, channel_db_id)
        else:
            text = await get_promo_text(db)

    # Delete previous promo post.
    if channel_db_id is not None:
        state = await get_channel_promo_state(db, channel_db_id)
    else:
        state = await store.get_promo_state(db)
    if state and state.message_id:
        try:
            await bot.delete_message(chat_id=channel_id, message_id=state.message_id)
        except TelegramAPIError as exc:
            logger.warning("Could not delete previous promo post: %s", exc)

    me = await bot.get_me()
    # Include channel_id in deep link for multi-tenant routing.
    if channel_db_id is not None:
        deep_link = f"https://t.me/{me.username}?start=join_{channel_id}"
    else:
        deep_link = f"https://t.me/{me.username}?start=join"

    sent = await bot.send_message(
        chat_id=channel_id,
        text=text,
        reply_markup=build_keyboard(deep_link),
        disable_notification=silent,
    )
    if pin:
        try:
            await bot.pin_chat_message(
                chat_id=channel_id,
                message_id=sent.message_id,
                disable_notification=silent,
            )
        except TelegramAPIError as exc:
            logger.warning("Could not pin the promo post: %s", exc)
    return sent.message_id


# ── multi-channel scheduler ─────────────────────────────────────────────────


async def run_scheduler(bot: Bot, db: aiosqlite.Connection, settings) -> None:
    """Manage promo posting for all active channels.

    Each channel has its own interval, pause state, and promo text.
    The scheduler sleeps until the soonest channel is due, posts to it,
    then repeats.  Channels are re-scanned periodically.
    """
    # In-memory schedule: {channel_db_id: next_run_at}
    schedule: dict[int, float] = {}
    last_scan: float = 0.0

    logger.info("Multi-channel promo scheduler started.")

    while True:
        now = time.time()

        # Periodic re-scan of active channels.
        if now - last_scan >= CHANNEL_SCAN_INTERVAL or not schedule:
            try:
                channels = await store.list_channels(db, active_only=True)
                for ch in channels:
                    if ch.id not in schedule:
                        # New channel — schedule it.
                        state = await get_channel_promo_state(db, ch.id)
                        if state:
                            schedule[ch.id] = state.next_run_at
                        else:
                            schedule[ch.id] = now + STARTUP_GRACE_SECONDS
                            logger.info("Channel #%s new — first promo in %.0fs", ch.id, STARTUP_GRACE_SECONDS)
                # Remove inactive channels.
                active_ids = {ch.id for ch in channels}
                for cid in list(schedule.keys()):
                    if cid not in active_ids:
                        del schedule[cid]
                last_scan = now
            except Exception:
                logger.exception("Error scanning channels")
                await asyncio.sleep(RETRY_BACKOFF_SECONDS)
                continue

        if not schedule:
            logger.info("No active channels — sleeping %.0fs.", CHANNEL_SCAN_INTERVAL)
            await asyncio.sleep(CHANNEL_SCAN_INTERVAL)
            continue

        # Find the soonest channel.
        soonest_id = min(schedule, key=schedule.get)
        wait = max(0.0, schedule[soonest_id] - time.time())
        logger.info("Next promo: channel #%s in %.0fs", soonest_id, wait)
        await asyncio.sleep(wait)

        # Check if the channel is paused.
        if await is_channel_paused(db, soonest_id):
            logger.info("Channel #%s paused — skipping; re-checking in %.0fs.", soonest_id, PAUSE_POLL_SECONDS)
            schedule[soonest_id] = time.time() + PAUSE_POLL_SECONDS
            continue

        # Get channel settings.
        ch = await store.get_channel(db, soonest_id)
        if ch is None or not ch.active:
            schedule.pop(soonest_id, None)
            continue

        try:
            text = await get_channel_promo_text(db, ch.id)
            message_id = await publish_promo(
                bot, db,
                channel_id=ch.tg_channel_id,
                pin=ch.promo_pin,
                silent=ch.promo_silent,
                text=text,
                channel_db_id=ch.id,
            )
            interval_h = await get_channel_interval(db, ch.id, ch.promo_interval_hours)
            next_run_at = time.time() + interval_h * 3600
            await set_channel_promo_state(db, ch.id, message_id, next_run_at)
            schedule[ch.id] = next_run_at
            logger.info(
                "Promo published for channel #%s (msg_id=%s); next in %.1fh",
                ch.id, message_id, interval_h,
            )
        except asyncio.CancelledError:
            logger.info("Promo scheduler cancelled.")
            raise
        except Exception:
            logger.exception("Promo publish failed for channel #%s; retrying in %.0fs", ch.id, RETRY_BACKOFF_SECONDS)
            schedule[ch.id] = time.time() + RETRY_BACKOFF_SECONDS
