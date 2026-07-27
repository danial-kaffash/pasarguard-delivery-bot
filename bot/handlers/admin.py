"""Owner-only admin commands (M5).

Promo:   /setpromo  /setinterval  /promonow  /getpromo
Groups:  /groups  /offergroups  /setoffer  /deloffer  /reorder  /clearoffers
Grants:  /reset  /stats
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta

import aiosqlite
from aiogram import Bot, Router
from aiogram.filters import BaseFilter, Command
from aiogram.filters.command import CommandObject
from aiogram.types import Message

from panel.client import PasarGuardApiClient
from panel.exceptions import PanelError
from services import trial as trial_service
from storage import db as store

from ..promo import (
    PROMO_INTERVAL_KEY,
    PROMO_TEXT_KEY,
    get_interval_hours,
    publish_promo,
)

logger = logging.getLogger(__name__)

router = Router(name="admin")

MAX_INTERVAL_HOURS = 24 * 30  # sanity cap: 30 days


class IsOwner(BaseFilter):
    """Reject anyone whose Telegram id is not in OWNER_TG_IDS."""

    async def __call__(self, message: Message, settings) -> bool:
        return message.from_user is not None and message.from_user.id in settings.owner_tg_ids


# ── promo management ─────────────────────────────────────────────────────────


@router.message(IsOwner(), Command("setpromo"))
async def cmd_setpromo(message: Message, command: CommandObject, db: aiosqlite.Connection) -> None:
    text = (command.args or "").strip()
    if not text:
        await message.answer("📝 کاربرد: <code>/setpromo متن پیام تبلیغاتی</code>")
        return
    if len(text) > 4096:
        await message.answer("❌ متن خیلی طولانیه (حداکثر ۴۰۹۶ کاراکتر).")
        return
    await store.set_setting(db, PROMO_TEXT_KEY, text)
    await message.answer("✅ متن پیام تبلیغاتی ذخیره شد. پست بعدی با متن جدید ارسال می‌شه.")


@router.message(IsOwner(), Command("setinterval"))
async def cmd_setinterval(
    message: Message, command: CommandObject, db: aiosqlite.Connection
) -> None:
    raw = (command.args or "").strip()
    try:
        hours = float(raw)
    except ValueError:
        hours = -1
    if hours <= 0 or hours > MAX_INTERVAL_HOURS:
        await message.answer("📝 کاربرد: <code>/setinterval 6</code>\n(عدد ساعت، بین ۰ تا ۷۲۰)")
        return
    await store.set_setting(db, PROMO_INTERVAL_KEY, str(hours))
    await message.answer(f"✅ فاصلهٔ ارسال پست تبلیغاتی شد هر <b>{hours:g}</b> ساعت.")


@router.message(IsOwner(), Command("promonow"))
async def cmd_promonow(message: Message, bot: Bot, db: aiosqlite.Connection, settings) -> None:
    try:
        msg_id = await publish_promo(
            bot,
            db,
            channel_id=settings.channel_id,
            pin=settings.promo_pin,
            silent=settings.promo_silent,
        )
    except Exception as exc:
        logger.exception("Manual promo publish failed")
        await message.answer(f"❌ ارسال ناموفق بود: {exc}")
        return
    interval = await get_interval_hours(db, settings.promo_interval_hours)
    await store.set_promo_state(db, settings.channel_id, msg_id, time.time() + interval * 3600)
    await message.answer(
        f"✅ پست تبلیغاتی ارسال و سنجاق شد (message_id={msg_id}).\n"
        f"ارسال بعدی: {interval:g} ساعت دیگه."
    )


@router.message(IsOwner(), Command("getpromo"))
async def cmd_getpromo(message: Message, db: aiosqlite.Connection, settings) -> None:
    text = await _current_promo_text(db)
    interval = await get_interval_hours(db, settings.promo_interval_hours)
    state = await store.get_promo_state(db)
    next_run = (
        datetime.fromtimestamp(state.next_run_at, UTC).strftime("%Y-%m-%d %H:%M UTC")
        if state
        else "—"
    )
    await message.answer(
        f"⏱ فاصلهٔ ارسال: هر <b>{interval:g}</b> ساعت\n"
        f"🕐 ارسال بعدی: {next_run}\n"
        f"📄 آخرین message_id: {state.message_id if state else '—'}\n\n"
        "👇 پیش‌نمایش متن فعلی (بدون تگ):"
    )
    await message.answer(text, parse_mode=None)


# ── group management ─────────────────────────────────────────────────────────


@router.message(IsOwner(), Command("groups"))
async def cmd_groups(message: Message, panel: PasarGuardApiClient) -> None:
    try:
        groups = await trial_service.fetch_panel_groups_map(panel, force=True)
    except PanelError as exc:
        await message.answer(f"❌ دریافت گروه‌ها از پنل ناموفق بود: {exc}")
        return
    if not groups:
        await message.answer("پنل هیچ گروهی نداره.")
        return
    lines = [f"{gid} — {name}" for gid, name in sorted(groups.items())]
    await message.answer(
        "📋 <b>گروه‌های پنل:</b>\n<code>"
        + "\n".join(lines)
        + "</code>\n\nبرای افزودن به لیست تست: <code>/setoffer &lt;id&gt; &lt;برچسب&gt;</code>"
    )


@router.message(IsOwner(), Command("offergroups"))
async def cmd_offergroups(
    message: Message, panel: PasarGuardApiClient, db: aiosqlite.Connection
) -> None:
    valid, stale = await trial_service.get_offered_groups(panel, db)
    if not valid and not stale:
        await message.answer(
            "لیست گروه‌های پیشنهادی خالیه — تست‌ها موقتاً متوقفن.\n"
            "افزودن: <code>/setoffer &lt;id&gt; &lt;برچسب&gt;</code>"
        )
        return
    lines = [f"{i}. {o.label} <i>(id={o.id})</i>" for i, o in enumerate(valid, 1)]
    text = "🎯 <b>گروه‌های پیشنهادی فعلی:</b>\n" + "\n".join(lines)
    if stale:
        text += f"\n\n⚠️ این idها دیگه توی پنل نیستن: <code>{', '.join(map(str, stale))}</code>"
    await message.answer(text)


@router.message(IsOwner(), Command("setoffer"))
async def cmd_setoffer(message: Message, command: CommandObject, db: aiosqlite.Connection) -> None:
    args = (command.args or "").strip()
    parts = args.split(maxsplit=1)
    if len(parts) != 2 or not parts[0].lstrip("-").isdigit():
        await message.answer("📝 کاربرد: <code>/setoffer 2 🇳🇱 هلند</code>")
        return
    group_id, label = int(parts[0]), parts[1].strip()
    if not label:
        await message.answer("📝 برچسب نمی‌تونه خالی باشه.")
        return
    await store.upsert_offer_group(db, group_id, label)
    await message.answer(f"✅ «{label}» برای گروه <code>{group_id}</code> ذخیره شد.")


@router.message(IsOwner(), Command("deloffer"))
async def cmd_deloffer(message: Message, command: CommandObject, db: aiosqlite.Connection) -> None:
    raw = (command.args or "").strip()
    if not raw.isdigit():
        await message.answer("📝 کاربرد: <code>/deloffer 2</code>")
        return
    group_id = int(raw)
    if await store.delete_offer_group(db, group_id):
        await message.answer(f"✅ گروه <code>{group_id}</code> از لیست حذف شد.")
    else:
        await message.answer(f"گروه <code>{group_id}</code> توی لیست نبود.")


@router.message(IsOwner(), Command("reorder"))
async def cmd_reorder(message: Message, command: CommandObject, db: aiosqlite.Connection) -> None:
    raw = (command.args or "").strip()
    try:
        ordered = [int(p) for p in raw.replace(" ", ",").split(",") if p]
    except ValueError:
        ordered = []
    current = {o.id for o in await store.list_offer_groups(db)}
    if not ordered or set(ordered) != current:
        current_str = ", ".join(str(i) for i in sorted(current)) or "خالی"
        await message.answer(
            "📝 باید دقیقاً همهٔ idهای فعلی رو به ترتیب دلخواه بدی.\n"
            f"idهای فعلی: <code>{current_str}</code>\n"
            "مثال: <code>/reorder 5,2,9</code>"
        )
        return
    await store.reorder_offer_groups(db, ordered)
    await message.answer("✅ ترتیب نمایش دکمه‌ها به‌روزرسانی شد.")


@router.message(IsOwner(), Command("clearoffers"))
async def cmd_clearoffers(message: Message, db: aiosqlite.Connection) -> None:
    count = await store.clear_offer_groups(db)
    await message.answer(f"✅ {count} گروه از لیست حذف شد — تا پرکردن دوبارهٔ لیست، تست‌ها متوقفن.")


# ── grants & stats ───────────────────────────────────────────────────────────


@router.message(IsOwner(), Command("reset"))
async def cmd_reset(message: Message, command: CommandObject, db: aiosqlite.Connection) -> None:
    raw = (command.args or "").strip().lstrip("@")
    if not raw.isdigit():
        await message.answer("📝 کاربرد: <code>/reset 123456789</code> (آیدی عددی کاربر تلگرام)")
        return
    tg_user_id = int(raw)
    if await store.revoke_grant(db, tg_user_id):
        await message.answer(
            f"✅ تست کاربر <code>{tg_user_id}</code> ریست شد؛ می‌تونه دوباره تست بگیره."
        )
    else:
        await message.answer(f"برای کاربر <code>{tg_user_id}</code> هیچ تستی ثبت نشده بود.")


@router.message(IsOwner(), Command("stats"))
async def cmd_stats(message: Message, db: aiosqlite.Connection, settings) -> None:
    now = datetime.now(UTC)
    day_ago = now - timedelta(days=1)
    lifetime = timedelta(days=settings.on_hold_grace_days + settings.trial_days)

    grants = await store.list_grants(db)
    active = [g for g in grants if not g.revoked and g.created_at + lifetime > now]
    joins = await store.count_member_events(db, "join", day_ago)
    leaves = await store.count_member_events(db, "leave", day_ago)
    members = await store.count_chat_members(db, settings.channel_id)
    offers = await store.list_offer_groups(db)
    state = await store.get_promo_state(db)
    next_run = (
        datetime.fromtimestamp(state.next_run_at, UTC).strftime("%Y-%m-%d %H:%M UTC")
        if state
        else "—"
    )

    await message.answer(
        "📊 <b>آمار ربات</b>\n\n"
        f"👥 عضوهای کانال (دیده‌شده): <b>{members}</b>\n"
        f"➕ عضویت ۲۴ ساعت اخیر: <b>{joins}</b> | ➖ ریزش: <b>{leaves}</b>\n\n"
        f"🎁 کل تست‌های داده‌شده: <b>{len(grants)}</b>\n"
        f"✅ تست‌های فعال: <b>{len(active)}</b>\n\n"
        f"🎯 گروه‌های پیشنهادی: <b>{len(offers)}</b>\n"
        f"📌 پست تبلیغاتی بعدی: {next_run}"
    )


# ── helpers ──────────────────────────────────────────────────────────────────


async def _current_promo_text(db: aiosqlite.Connection) -> str:
    from ..promo import get_promo_text

    return await get_promo_text(db)
