"""Entrypoint (M3): aiogram dispatcher + channel promo scheduler.

The trial flow (M4) plugs into the same dispatcher via extra routers.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from storage import db as store

from .config import get_settings
from .handlers.start import router as start_router
from .logging_setup import setup_logging
from .promo import run_scheduler

logger = logging.getLogger(__name__)


def build_dispatcher(settings) -> tuple[Bot, Dispatcher]:
    bot = Bot(
        settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp["settings"] = settings

    dp.include_router(start_router)

    async def on_startup() -> None:
        db = await store.connect(settings.db_path)
        seeded = await store.seed_offer_groups_from_file(db, settings.offer_groups_file)
        if seeded:
            logger.info("Seeded %d offer group(s) from %s", seeded, settings.offer_groups_file)
        dp["db"] = db
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
        db = dp.get("db")
        if db:
            await db.close()
        logger.info("Shutdown complete.")

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    return bot, dp


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set — see .env.example")
    if not settings.channel_id:
        raise SystemExit("CHANNEL_ID is not set — see .env.example")

    bot, dp = build_dispatcher(settings)
    logger.info(
        "Trial: %.1f GB (%d bytes) on-hold — %d-day usage window, %d-day grace, protocols=%s",
        settings.trial_data_limit_gb,
        settings.trial_data_limit_bytes,
        settings.trial_days,
        settings.on_hold_grace_days,
        ",".join(settings.trial_protocol_list),
    )
    # Only receive updates we actually handle (message for now; chat_member in M5).
    asyncio.run(dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()))


if __name__ == "__main__":
    main()
