"""Entrypoint (M3): aiogram dispatcher + channel promo scheduler.

The trial flow (M4) plugs into the same dispatcher via extra routers.
Multi-tenant: uses PanelManager for multiple panels.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import ErrorEvent

from panel.client import PasarGuardApiClient
from panel.manager import PanelManager
from storage import db as store

from . import texts
from .config import get_settings
from .handlers.admin import router as admin_router
from .handlers.backup import router as backup_router
from .handlers.join_request import router as join_request_router
from .handlers.member_events import router as member_events_router
from .handlers.panel import router as panel_router
from .handlers.trial import router as trial_router
from .logging_setup import setup_logging
from .middlewares import RateLimitMiddleware
from .promo import run_scheduler

logger = logging.getLogger(__name__)


def build_dispatcher(settings) -> tuple[Bot, Dispatcher]:
    bot = Bot(
        settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp["settings"] = settings

    dp.include_router(admin_router)
    dp.include_router(backup_router)
    dp.include_router(panel_router)
    dp.include_router(trial_router)
    dp.include_router(join_request_router)
    dp.include_router(member_events_router)

    limiter = RateLimitMiddleware(settings.rate_limit_per_minute)
    dp.message.middleware(limiter)
    dp.callback_query.middleware(limiter)

    async def on_startup() -> None:
        db = await store.connect(settings.db_path)

        # Seed legacy offer groups from file (if table is empty).
        seeded = await store.seed_offer_groups_from_file(db, settings.offer_groups_file)
        if seeded:
            logger.info("Seeded %d offer group(s) from %s", seeded, settings.offer_groups_file)

        # First-run migration: seed multi-tenant tables from .env.
        from .migration import migrate_from_env
        migrated = await migrate_from_env(db, settings)
        if migrated:
            logger.info("First-run migration from .env completed.")

        # Fetch channel titles from Telegram for any channels with empty titles.
        await _refresh_channel_titles(bot, db)

        # Multi-tenant panel manager.
        panel_manager = PanelManager()

        # Legacy single panel client — kept for backward compatibility.
        legacy_panel = None
        panels = await store.list_panels(db, active_only=True)
        if panels:
            legacy_panel = panel_manager.get_client(panels[0])
        elif settings.panel_base_url:
            # Fallback: create a panel from .env if migration didn't run.
            logger.info("No panels in DB — creating from .env.")
            from storage import crypto  # noqa: F811
            panel_row = await store.create_panel(
                db,
                name="Default",
                base_url=settings.panel_base_url,
                admin_username=settings.panel_admin_username,
                admin_password=settings.panel_admin_password,
                verify_ssl=settings.panel_verify_ssl,
                timeout_seconds=settings.panel_timeout_seconds,
                protocols=settings.trial_protocols,
                auto_delete_days=settings.auto_delete_days,
            )
            legacy_panel = panel_manager.get_client(panel_row)

        dp["db"] = db
        dp["panel_manager"] = panel_manager
        dp["panel"] = legacy_panel

        dp["scheduler_task"] = asyncio.create_task(
            run_scheduler(bot, db, settings), name="promo-scheduler"
        )
        me = await bot.get_me()
        logger.info(
            "Bot @%s is up. Deep link: https://t.me/%s?start=join", me.username, me.username
        )

    async def on_shutdown() -> None:
        task = dp.get("scheduler_task")
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        panel_manager: PanelManager | None = dp.get("panel_manager")
        if panel_manager:
            await panel_manager.close_all()
        # Legacy panel close (if manager wasn't used).
        legacy = dp.get("panel")
        if legacy and not panel_manager:
            await legacy.aclose()
        db = dp.get("db")
        if db:
            await db.close()
        logger.info("Shutdown complete.")

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    dp.errors.register(on_error)
    return bot, dp


async def on_error(event: ErrorEvent, bot: Bot) -> bool:
    """Last-resort handler: log, and politely inform the user when possible."""
    logger.exception("Unhandled error in update handler: %s", event.exception)
    update = event.update
    chat_id = None
    if update and update.message:
        chat_id = update.message.chat.id
    elif update and update.callback_query and update.callback_query.message:
        chat_id = update.callback_query.message.chat.id
    if chat_id:
        with contextlib.suppress(Exception):
            await bot.send_message(chat_id, texts.ERROR_TRY_AGAIN)
    return True


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set — see .env.example")

    bot, dp = build_dispatcher(settings)
    # Only receive updates we actually handle.
    asyncio.run(dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()))


async def _refresh_channel_titles(bot: Bot, db) -> None:
    """Fetch channel titles from Telegram for channels with empty titles."""
    try:
        channels = await store.list_channels(db, active_only=False)
        for ch in channels:
            if ch.title:
                continue
            try:
                chat = await bot.get_chat(ch.tg_channel_id)
                if chat.title:
                    await store.update_channel(db, ch.id, title=chat.title)
                    logger.info("Fetched title for channel %s: %s", ch.tg_channel_id, chat.title)
            except Exception:
                logger.debug("Could not fetch title for channel %s", ch.tg_channel_id)
    except Exception:
        logger.warning("Could not refresh channel titles.")


if __name__ == "__main__":
    main()
