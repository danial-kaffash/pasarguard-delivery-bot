"""Channel promo poster + scheduler (PLAN.md §3.1).

Every interval the bot: deletes the previous promo post, publishes the
owner-defined message with a deep-link button, and pins it silently — so the
channel always shows exactly one pinned CTA and subscribers are never pinged.
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

from .pause import is_paused

logger = logging.getLogger(__name__)

DEFAULT_PROMO_FILE = Path(__file__).resolve().parent.parent / "texts" / "promo_fa.txt"

PROMO_TEXT_KEY = "promo_text"
PROMO_INTERVAL_KEY = "promo_interval_hours"

START_PAYLOAD = "join"  # t.me/<bot>?start=join
STARTUP_GRACE_SECONDS = 15.0  # delay of the very first post after boot
RETRY_BACKOFF_SECONDS = 60.0
PAUSE_POLL_SECONDS = 60.0  # re-check pause flag interval


# ── content & configuration resolution ──────────────────────────────────────


async def get_promo_text(db: aiosqlite.Connection) -> str:
    """Runtime override (set via /setpromo in M5) beats the seed file."""
    override = await store.get_setting(db, PROMO_TEXT_KEY)
    if override:
        return override
    if DEFAULT_PROMO_FILE.exists():
        return DEFAULT_PROMO_FILE.read_text(encoding="utf-8").strip()
    return "🎁 تست رایگان ۵ گیگابایتی! برای دریافت روی دکمهٔ زیر بزنید."


async def get_interval_hours(db: aiosqlite.Connection, default: float) -> float:
    """Runtime override (set via /setinterval in M5), else the .env default."""
    raw = await store.get_setting(db, PROMO_INTERVAL_KEY)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid promo interval setting %r — using default %s", raw, default)
        return default
    return value if value > 0 else default


def build_keyboard(deep_link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🎁 دریافت تست ۵ گیگ", url=deep_link)]]
    )


# ── publishing ───────────────────────────────────────────────────────────────


async def publish_promo(
    bot: Bot,
    db: aiosqlite.Connection,
    *,
    channel_id: int,
    pin: bool,
    silent: bool,
) -> int:
    """Replace the previous promo post; return the new message id."""
    state = await store.get_promo_state(db)
    if state and state.channel_id == channel_id:
        try:
            await bot.delete_message(chat_id=channel_id, message_id=state.message_id)
        except TelegramAPIError as exc:
            logger.warning("Could not delete previous promo post: %s", exc)

    me = await bot.get_me()
    deep_link = f"https://t.me/{me.username}?start={START_PAYLOAD}"
    text = await get_promo_text(db)

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


# ── scheduler loop ───────────────────────────────────────────────────────────


async def run_scheduler(bot: Bot, db: aiosqlite.Connection, settings) -> None:
    """Post the promo on schedule, forever (until cancelled).

    Restart-safe: the next run time is persisted, so a restart does not
    double-post — unless the state row is missing, in which case the first
    post goes out STARTUP_GRACE_SECONDS after boot.
    """
    logger.info(
        "Promo scheduler started (channel=%s, default interval=%.1fh)",
        settings.channel_id,
        settings.promo_interval_hours,
    )
    while True:
        state = await store.get_promo_state(db)
        now = time.time()
        wait = STARTUP_GRACE_SECONDS if state is None else max(0.0, state.next_run_at - now)
        logger.info("Next promo post in %.0f s", wait)
        await asyncio.sleep(wait)
        if await is_paused(db):
            logger.info(
                "Bot paused - skipping promo post; re-checking in %.0f s.", PAUSE_POLL_SECONDS
            )
            await asyncio.sleep(PAUSE_POLL_SECONDS)
            continue  # next_run stays in the past -> posts promptly after /resume
        try:
            message_id = await publish_promo(
                bot,
                db,
                channel_id=settings.channel_id,
                pin=settings.promo_pin,
                silent=settings.promo_silent,
            )
            interval_h = await get_interval_hours(db, settings.promo_interval_hours)
            next_run_at = time.time() + interval_h * 3600
            await store.set_promo_state(db, settings.channel_id, message_id, next_run_at)
            logger.info(
                "Promo post published (message_id=%s); next in %.1fh", message_id, interval_h
            )
        except asyncio.CancelledError:
            logger.info("Promo scheduler cancelled.")
            raise
        except Exception:
            logger.exception("Promo publish failed; retrying in %.0f s", RETRY_BACKOFF_SECONDS)
            await asyncio.sleep(RETRY_BACKOFF_SECONDS)
