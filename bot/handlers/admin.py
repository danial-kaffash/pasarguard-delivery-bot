"""Admin commands — multi-tenant with role-based access.

Superadmin commands (global):
  /addpanel  /panels  /editpanel  /removepanel
  /addchannel  /channels  /editchannel  /removechannel
  /assign  /unassign  /promote  /demote  /users  /sysstats

Channel-scoped commands (superadmin or channel admin):
  /pause  /resume  /pausejoins  /resumejoins
  /setpromo  /setinterval  /promonow  /getpromo
  /settrial  /setjoindelay  /setmaxage
  /groups  /offergroups  /setoffer  /deloffer  /reorder  /clearoffers
  /reset  /stats  /joinstats
  /newpost  /posts  /checkpremium  (channel-posts feature)

Channel context:
  - In a channel/group chat: inferred from event.chat.id
  - In DM: explicit channel_id as first arg, e.g. /stats <channel_id>
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

from panel.manager import PanelManager
from services import trial as trial_service
from storage import db as store
from storage.db import Channel

from ..handlers.join_request import get_join_delay
from ..pause import (
    is_channel_joins_paused,
    is_channel_paused,
    set_channel_joins_paused,
    set_channel_paused,
)
from ..promo import (
    get_interval_hours,
    get_promo_text,
    publish_promo,
)

logger = logging.getLogger(__name__)

router = Router(name="admin")

MAX_INTERVAL_HOURS = 24 * 30

# Channel-scoped settings key patterns.
_CH_PROMO_TEXT = "channel:{cid}:promo_text"
_CH_PROMO_INTERVAL = "channel:{cid}:promo_interval_hours"
_CH_MAX_AGE = "channel:{cid}:trial_max_member_age_days"


# ═══════════════════════════════════════════════════════════════════════════════
# ── Auth filters ───────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


async def _get_role(db: aiosqlite.Connection, tg_user_id: int) -> str | None:
    """Return the user's role from the DB, or None if not found."""
    user = await store.get_user(db, tg_user_id)
    return user.role if user else None


class IsSuperadmin(BaseFilter):
    """Pass only if the user's role is 'superadmin' in the users table,
    OR their id is in the legacy OWNER_TG_IDS (for bootstrapping)."""

    async def __call__(self, message: Message, db: aiosqlite.Connection, settings) -> bool:
        uid = message.from_user.id
        if uid in settings.owner_tg_ids:
            return True
        return await _get_role(db, uid) == "superadmin"


class IsChannelAdmin(BaseFilter):
    """Pass if the user is superadmin or an assigned admin of the resolved channel."""

    async def __call__(self, message: Message, db: aiosqlite.Connection, settings) -> bool:
        uid = message.from_user.id
        if uid in settings.owner_tg_ids:
            return True
        role = await _get_role(db, uid)
        if role == "superadmin":
            return True
        if role == "admin":
            # Channel is resolved later by the command handler.
            # Here we just verify they have *some* admin assignment.
            channels = await store.list_user_channels(db, uid)
            return len(channels) > 0
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# ── Channel resolver ───────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


async def _resolve_channel(
    message: Message,
    command: CommandObject,
    db: aiosqlite.Connection,
    settings,
) -> Channel | None:
    """Resolve the target channel for a command.

    - In a channel/group: use event.chat.id
    - In DM: require explicit channel_id as first arg
    Returns None and sends an error message if resolution fails.
    """
    if message.chat.type != "private":
        # In a channel — use the current chat.
        ch = await store.get_channel_by_tg_id(db, message.chat.id)
        if ch is None:
            await message.answer("❌ این چت در لیست کانال‌های مدیریت‌شده نیست.")
        return ch

    # In DM — need explicit channel_id.
    raw = (command.args or "").strip().split()[0] if command.args else ""
    if not raw.lstrip("-").isdigit():
        await message.answer(
            "📝 در پی‌وی باید آیدی کانال رو مشخص کنی.\nمثال: <code>/stats -1001234567890</code>"
        )
        return None

    tg_channel_id = int(raw)
    ch = await store.get_channel_by_tg_id(db, tg_channel_id)
    if ch is None:
        await message.answer(f"❌ کانال <code>{tg_channel_id}</code> یافت نشد.")
        return None

    # Verify the user has access.
    uid = message.from_user.id
    if uid in settings.owner_tg_ids:
        return ch
    role = await _get_role(db, uid)
    if role == "superadmin":
        return ch
    if role == "admin" and await store.is_channel_admin(db, uid, ch.id):
        return ch

    await message.answer("❌ شما دسترسی مدیریت این کانال را ندارید.")
    return None


def _strip_channel_arg(command: CommandObject) -> str:
    """Remove the leading channel_id arg from command.args, return the rest."""
    raw = (command.args or "").strip()
    parts = raw.split(maxsplit=1)
    if parts and parts[0].lstrip("-").isdigit():
        return parts[1] if len(parts) > 1 else ""
    return raw


async def _resolve_channel_with_args(
    message: Message,
    command: CommandObject,
    db: aiosqlite.Connection,
    settings,
) -> tuple[Channel | None, list[str]]:
    """Resolve channel and return remaining args.

    In a channel: channel from chat context, all args are available.
    In DM: channel from first arg, remaining args returned.
    """
    if message.chat.type != "private":
        ch = await store.get_channel_by_tg_id(db, message.chat.id)
        if ch is None:
            await message.answer("❌ این چت در لیست کانال‌های مدیریت‌شده نیست.")
            return None, []
        return ch, (command.args or "").strip().split()

    # DM: first arg is channel_id.
    raw = (command.args or "").strip()
    parts = raw.split(maxsplit=1)
    if not parts or not parts[0].lstrip("-").isdigit():
        await message.answer(
            "📝 در پی‌وی باید آیدی کانال رو مشخص کنی.\nمثال: <code>/stats -1001234567890</code>"
        )
        return None, []

    tg_channel_id = int(parts[0])
    remaining = parts[1].strip().split() if len(parts) > 1 else []
    ch = await store.get_channel_by_tg_id(db, tg_channel_id)
    if ch is None:
        await message.answer(f"❌ کانال <code>{tg_channel_id}</code> یافت نشد.")
        return None, []

    # Verify access.
    uid = message.from_user.id
    if uid in settings.owner_tg_ids:
        return ch, remaining
    role = await _get_role(db, uid)
    if role == "superadmin":
        return ch, remaining
    if role == "admin" and await store.is_channel_admin(db, uid, ch.id):
        return ch, remaining

    await message.answer("❌ شما دسترسی مدیریت این کانال را ندارید.")
    return None, []


# ═══════════════════════════════════════════════════════════════════════════════
# ── Superadmin commands ────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


@router.message(IsSuperadmin(), Command("addpanel"))
async def cmd_addpanel(message: Message, command: CommandObject, db: aiosqlite.Connection) -> None:
    """Add a new PasarGuard panel. Usage: /addpanel <name> <url> <user> <pass>"""
    args = (command.args or "").strip().split(maxsplit=3)
    if len(args) < 4:
        await message.answer(
            "📝 کاربرد: <code>/addpanel NL https://nl.example.com admin password123</code>"
        )
        return
    name, url, username, password = args[0], args[1], args[2], args[3]
    panel = await store.create_panel(
        db,
        name=name,
        base_url=url,
        admin_username=username,
        admin_password=password,
    )
    await message.answer(
        f"✅ پنل «{name}» اضافه شد (id={panel.id}).\n"
        f"🔗 {url}\n"
        f"برای تنظیم SSL/timeout/protocols: "
        f"<code>/editpanel {panel.id} &lt;field&gt; &lt;value&gt;</code>"
    )


@router.message(IsSuperadmin(), Command("panels"))
async def cmd_panels(message: Message, db: aiosqlite.Connection) -> None:
    """List all panels."""
    panels = await store.list_panels(db, active_only=False)
    if not panels:
        await message.answer("هیچ پنلی ثبت نشده. <code>/addpanel</code>")
        return
    lines = []
    for p in panels:
        status = "✅" if p.active else "🗑"
        lines.append(f"{status} <b>#{p.id}</b> {p.name} — <code>{p.base_url}</code>")
    await message.answer("🖥 <b>پنل‌ها:</b>\n" + "\n".join(lines))


@router.message(IsSuperadmin(), Command("editpanel"))
async def cmd_editpanel(
    message: Message,
    command: CommandObject,
    db: aiosqlite.Connection,
) -> None:
    """Edit a panel field. Usage: /editpanel <id> <field> <value>"""
    args = (command.args or "").strip().split(maxsplit=2)
    if len(args) < 3 or not args[0].isdigit():
        await message.answer(
            "📝 کاربرد: <code>/editpanel 1 password newpass123</code>\n"
            "فیلدها: name, base_url, admin_username, admin_password, verify_ssl, "
            "timeout_seconds, protocols, auto_delete_days"
        )
        return
    panel_id, field, value = int(args[0]), args[1], args[2]
    # Type coercion for numeric/bool fields.
    if field in ("verify_ssl",):
        value = value.lower() in ("1", "true", "yes")
    elif field in ("timeout_seconds",):
        value = float(value)
    elif field in ("auto_delete_days",):
        value = int(value)
    if await store.update_panel(db, panel_id, **{field: value}):
        await message.answer(f"✅ پنل #{panel_id}: فیلد {field} به‌روزرسانی شد.")
    else:
        await message.answer(f"❌ پنل #{panel_id} یافت نشد.")


@router.message(IsSuperadmin(), Command("removepanel"))
async def cmd_removepanel(
    message: Message,
    command: CommandObject,
    db: aiosqlite.Connection,
) -> None:
    """Soft-delete a panel. Usage: /removepanel <id>"""
    raw = (command.args or "").strip()
    if not raw.isdigit():
        await message.answer("📝 کاربرد: <code>/removepanel 1</code>")
        return
    panel_id = int(raw)
    # TODO: check if any active channel references this panel before deleting.
    # For now, just soft-delete.
    if await store.soft_delete_panel(db, panel_id):
        await message.answer(f"✅ پنل #{panel_id} غیرفعال شد.")
    else:
        await message.answer(f"❌ پنل #{panel_id} یافت نشد.")


@router.message(IsSuperadmin(), Command("addchannel"))
async def cmd_addchannel(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: aiosqlite.Connection,
) -> None:
    """Add a channel. Usage: /addchannel <tg_channel_id>"""
    args = (command.args or "").strip().split()
    if not args or not args[0].lstrip("-").isdigit():
        await message.answer("📝 کاربرد: <code>/addchannel -1001234567890</code>")
        return
    tg_id = int(args[0])
    existing = await store.get_channel_by_tg_id(db, tg_id)
    if existing:
        if not existing.active:
            # Reactivate the soft-deleted channel.
            await store.update_channel(db, existing.id, active=True)
            # Try to refresh the title.
            try:
                chat = await bot.get_chat(tg_id)
                if chat.title:
                    await store.update_channel(db, existing.id, title=chat.title)
            except Exception:
                pass
            ch = await store.get_channel(db, existing.id)
            display = f"{ch.title} (<code>{tg_id}</code>)" if ch.title else f"<code>{tg_id}</code>"
            await message.answer(f"✅ کانال #{ch.id} دوباره فعال شد: {display}")
        else:
            await message.answer(f"❌ کانال <code>{tg_id}</code> قبلاً ثبت شده (#{existing.id}).")
        return

    # Try to fetch the real channel title from Telegram.
    title = ""
    try:
        chat = await bot.get_chat(tg_id)
        title = chat.title or ""
    except Exception:
        logger.warning("Could not fetch chat info for %s — title will be empty.", tg_id)

    ch = await store.create_channel(db, tg_channel_id=tg_id, title=title)
    display = f"{title} (<code>{tg_id}</code>)" if title else f"<code>{tg_id}</code>"
    await message.answer(
        f"✅ کانال #{ch.id} ثبت شد: {display}\n"
        f"برای افزودن گروه: <code>/setoffer {tg_id} "
        f"&lt;panel_id&gt; &lt;group_id&gt; &lt;label&gt;</code>"
    )


@router.message(IsSuperadmin(), Command("refreshchannels"))
async def cmd_refreshchannels(
    message: Message,
    bot: Bot,
    db: aiosqlite.Connection,
) -> None:
    """Re-fetch channel titles from Telegram for all channels with empty titles."""
    channels = await store.list_channels(db, active_only=False)
    updated = 0
    for ch in channels:
        if ch.title:
            continue
        try:
            chat = await bot.get_chat(ch.tg_channel_id)
            if chat.title:
                await store.update_channel(db, ch.id, title=chat.title)
                updated += 1
        except Exception:
            logger.warning("Could not fetch chat info for %s", ch.tg_channel_id)
    if updated:
        await message.answer(f"✅ عنوان {updated} کانال از تلگرام دریافت شد.")
    else:
        await message.answer("همهٔ کانال‌ها عنوان دارند یا دریافت عنوان ممکن نبود.")


@router.message(IsSuperadmin(), Command("channels"))
async def cmd_channels(message: Message, db: aiosqlite.Connection) -> None:
    """List all channels."""
    channels = await store.list_channels(db, active_only=False)
    if not channels:
        await message.answer("هیچ کانالی ثبت نشده. <code>/addchannel</code>")
        return
    lines = []
    for ch in channels:
        status = "✅" if ch.active else "🗑"
        title = ch.title or "(بدون عنوان)"
        lines.append(f"{status} <b>#{ch.id}</b> {title} (<code>{ch.tg_channel_id}</code>)")
    await message.answer("📺 <b>کانال‌ها:</b>\n" + "\n".join(lines))


@router.message(IsSuperadmin(), Command("editchannel"))
async def cmd_editchannel(
    message: Message,
    command: CommandObject,
    db: aiosqlite.Connection,
) -> None:
    """Edit a channel field. Usage: /editchannel <tg_id> <field> <value>"""
    args = (command.args or "").strip().split(maxsplit=2)
    if len(args) < 3 or not args[0].lstrip("-").isdigit():
        await message.answer(
            "📝 کاربرد: <code>/editchannel -100123 title عنوان جدید</code>\n"
            "فیلدها: title, trial_data_limit_gb, trial_days, on_hold_grace_days, "
            "allow_regrant_after_days, trial_max_member_age_days, "
            "join_approval_delay_seconds, promo_interval_hours, promo_pin, promo_silent, "
            "post_delete_previous"
        )
        return
    tg_id = int(args[0])
    field, value = args[1], args[2]
    ch = await store.get_channel_by_tg_id(db, tg_id)
    if not ch:
        await message.answer(f"❌ کانال <code>{tg_id}</code> یافت نشد.")
        return
    # Type coercion.
    if field in ("promo_pin", "promo_silent", "post_delete_previous"):
        value = value.lower() in ("1", "true", "yes")
    elif field in (
        "trial_days",
        "on_hold_grace_days",
        "allow_regrant_after_days",
        "join_approval_delay_seconds",
    ):
        value = int(value)
    elif field in ("trial_data_limit_gb", "trial_max_member_age_days", "promo_interval_hours"):
        value = float(value)
    if await store.update_channel(db, ch.id, **{field: value}):
        await message.answer(f"✅ کانال #{ch.id}: فیلد {field} به‌روزرسانی شد.")
    else:
        await message.answer(f"❌ خطا در به‌روزرسانی کانال #{ch.id}.")


@router.message(IsSuperadmin(), Command("removechannel"))
async def cmd_removechannel(
    message: Message,
    command: CommandObject,
    db: aiosqlite.Connection,
) -> None:
    """Soft-delete a channel. Usage: /removechannel <tg_id>"""
    raw = (command.args or "").strip()
    if not raw.lstrip("-").isdigit():
        await message.answer("📝 کاربرد: <code>/removechannel -1001234567890</code>")
        return
    tg_id = int(raw)
    ch = await store.get_channel_by_tg_id(db, tg_id)
    if not ch:
        await message.answer(f"❌ کانال <code>{tg_id}</code> یافت نشد.")
        return
    if await store.soft_delete_channel(db, ch.id):
        await message.answer(f"✅ کانال #{ch.id} (<code>{tg_id}</code>) غیرفعال شد.")
    else:
        await message.answer("❌ خطا در غیرفعال‌سازی.")


@router.message(IsSuperadmin(), Command("assign"))
async def cmd_assign(
    message: Message,
    command: CommandObject,
    db: aiosqlite.Connection,
) -> None:
    """Assign an admin to a channel. Usage: /assign <user_id> <channel_tg_id>"""
    args = (command.args or "").strip().split()
    if len(args) < 2 or not args[0].isdigit() or not args[1].lstrip("-").isdigit():
        await message.answer("📝 کاربرد: <code>/assign 42 -1001234567890</code>")
        return
    uid, tg_ch = int(args[0]), int(args[1])
    ch = await store.get_channel_by_tg_id(db, tg_ch)
    if not ch:
        await message.answer(f"❌ کانال <code>{tg_ch}</code> یافت نشد.")
        return
    # Ensure user exists as admin.
    await store.upsert_user(db, tg_user_id=uid, role="admin")
    await store.assign_channel_admin(db, uid, ch.id)
    await message.answer(f"✅ کاربر <code>{uid}</code> به کانال #{ch.id} اضافه شد.")


@router.message(IsSuperadmin(), Command("unassign"))
async def cmd_unassign(
    message: Message,
    command: CommandObject,
    db: aiosqlite.Connection,
) -> None:
    """Remove an admin from a channel. Usage: /unassign <user_id> <channel_tg_id>"""
    args = (command.args or "").strip().split()
    if len(args) < 2 or not args[0].isdigit() or not args[1].lstrip("-").isdigit():
        await message.answer("📝 کاربرد: <code>/unassign 42 -1001234567890</code>")
        return
    uid, tg_ch = int(args[0]), int(args[1])
    ch = await store.get_channel_by_tg_id(db, tg_ch)
    if not ch:
        await message.answer(f"❌ کانال <code>{tg_ch}</code> یافت نشد.")
        return
    if await store.unassign_channel_admin(db, uid, ch.id):
        await message.answer(f"✅ کاربر <code>{uid}</code> از کانال #{ch.id} حذف شد.")
    else:
        await message.answer(f"کاربر <code>{uid}</code> مدیر این کانال نبود.")


@router.message(IsSuperadmin(), Command("promote"))
async def cmd_promote(
    message: Message,
    command: CommandObject,
    db: aiosqlite.Connection,
) -> None:
    """Promote a user. Usage: /promote <user_id> <role>"""
    args = (command.args or "").strip().split()
    if len(args) < 2 or not args[0].isdigit():
        await message.answer(
            "📝 کاربرد: <code>/promote 42 admin</code>\n(نقش‌ها: superadmin, admin)"
        )
        return
    uid, role = int(args[0]), args[1]
    if role not in store.VALID_ROLES or role == "user":
        await message.answer("❌ نقش باید <code>superadmin</code> یا <code>admin</code> باشد.")
        return
    await store.upsert_user(db, tg_user_id=uid, role=role)
    await message.answer(f"✅ کاربر <code>{uid}</code> به نقش <b>{role}</b> ارتقا یافت.")


@router.message(IsSuperadmin(), Command("demote"))
async def cmd_demote(
    message: Message,
    command: CommandObject,
    db: aiosqlite.Connection,
) -> None:
    """Demote a user to 'user'. Usage: /demote <user_id>"""
    raw = (command.args or "").strip()
    if not raw.isdigit():
        await message.answer("📝 کاربرد: <code>/demote 42</code>")
        return
    uid = int(raw)
    await store.upsert_user(db, tg_user_id=uid, role="user")
    await message.answer(f"✅ کاربر <code>{uid}</code> به نقش کاربر عادی تغییر یافت.")


@router.message(IsSuperadmin(), Command("users"))
async def cmd_users(message: Message, db: aiosqlite.Connection) -> None:
    """List all users with roles."""
    users = await store.list_users(db)
    if not users:
        await message.answer("هیچ کاربری ثبت نشده.")
        return
    lines = []
    for u in users:
        role_label = {"superadmin": "⭐", "admin": "👤"}.get(u.role, "👥")
        name = f"@{u.username}" if u.username else f"<code>{u.tg_user_id}</code>"
        lines.append(f"{role_label} {name} — {u.role}")
    await message.answer("👥 <b>کاربران:</b>\n" + "\n".join(lines))


@router.message(IsSuperadmin(), Command("sysstats"))
async def cmd_sysstats(message: Message, db: aiosqlite.Connection) -> None:
    """System-wide stats."""
    panels = await store.list_panels(db)
    channels = await store.list_channels(db)
    users = await store.list_users(db)
    grants = await store.list_grants(db)
    await message.answer(
        "📊 <b>آمار سیستم</b>\n\n"
        f"🖥 پنل‌ها: <b>{len(panels)}</b>\n"
        f"📺 کانال‌ها: <b>{len(channels)}</b>\n"
        f"👥 کاربران: <b>{len(users)}</b>\n"
        f"🎁 کل تست‌ها: <b>{len(grants)}</b>"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# ── Channel-scoped commands ────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


# ── pause / resume ───────────────────────────────────────────────────────────


@router.message(IsChannelAdmin(), Command("pause"))
async def cmd_pause(
    message: Message,
    command: CommandObject,
    db: aiosqlite.Connection,
    settings,
) -> None:
    ch = await _resolve_channel(message, command, db, settings)
    if not ch:
        return
    await set_channel_paused(db, ch.id, True)
    await message.answer(
        f"⏸ کانال <code>{ch.tg_channel_id}</code> متوقف شد.\nبرای شروع دوباره: /resume"
    )


@router.message(IsChannelAdmin(), Command("resume"))
async def cmd_resume(
    message: Message,
    command: CommandObject,
    db: aiosqlite.Connection,
    settings,
) -> None:
    ch = await _resolve_channel(message, command, db, settings)
    if not ch:
        return
    await set_channel_paused(db, ch.id, False)
    await message.answer(f"▶️ کانال <code>{ch.tg_channel_id}</code> فعال شد.")


# ── join-request controls ────────────────────────────────────────────────────


@router.message(IsChannelAdmin(), Command("pausejoins"))
async def cmd_pausejoins(
    message: Message,
    command: CommandObject,
    db: aiosqlite.Connection,
    settings,
) -> None:
    ch = await _resolve_channel(message, command, db, settings)
    if not ch:
        return
    await set_channel_joins_paused(db, ch.id, True)
    await message.answer(f"⏸ درخواست عضویت برای کانال #{ch.id} بدون تست تأیید می‌شه.")


@router.message(IsChannelAdmin(), Command("resumejoins"))
async def cmd_resumejoins(
    message: Message,
    command: CommandObject,
    db: aiosqlite.Connection,
    settings,
) -> None:
    ch = await _resolve_channel(message, command, db, settings)
    if not ch:
        return
    await set_channel_joins_paused(db, ch.id, False)
    await message.answer(f"▶️ ارسال تست از طریق درخواست عضویت برای کانال #{ch.id} فعال شد.")


# ── promo management ─────────────────────────────────────────────────────────


@router.message(IsChannelAdmin(), Command("setpromo"))
async def cmd_setpromo(
    message: Message,
    command: CommandObject,
    db: aiosqlite.Connection,
    settings,
) -> None:
    ch, args = await _resolve_channel_with_args(message, command, db, settings)
    if not ch:
        return
    text = " ".join(args).strip()
    if not text:
        await message.answer("📝 کاربرد: <code>/setpromo [-100123] متن پیام تبلیغاتی</code>")
        return
    if len(text) > 4096:
        await message.answer("❌ متن خیلی طولانیه (حداکثر ۴۰۹۶ کاراکتر).")
        return
    await store.set_setting(db, _CH_PROMO_TEXT.format(cid=ch.id), text)
    await message.answer(f"✅ متن تبلیغاتی کانال #{ch.id} ذخیره شد.")


@router.message(IsChannelAdmin(), Command("setinterval"))
async def cmd_setinterval(
    message: Message,
    command: CommandObject,
    db: aiosqlite.Connection,
    settings,
) -> None:
    ch, args = await _resolve_channel_with_args(message, command, db, settings)
    if not ch:
        return
    raw = args[0] if args else ""
    try:
        hours = float(raw)
    except ValueError:
        hours = -1
    if hours <= 0 or hours > MAX_INTERVAL_HOURS:
        await message.answer("📝 کاربرد: <code>/setinterval [-100123] 6</code>")
        return
    await store.set_setting(db, _CH_PROMO_INTERVAL.format(cid=ch.id), str(hours))
    await message.answer(f"✅ فاصلهٔ ارسال پست کانال #{ch.id}: هر <b>{hours:g}</b> ساعت.")


@router.message(IsChannelAdmin(), Command("promonow"))
async def cmd_promonow(
    message: Message,
    bot: Bot,
    command: CommandObject,
    db: aiosqlite.Connection,
    settings,
) -> None:
    ch = await _resolve_channel(message, command, db, settings)
    if not ch:
        return
    try:
        msg_id = await publish_promo(
            bot,
            db,
            channel_id=ch.tg_channel_id,
            pin=ch.promo_pin,
            silent=ch.promo_silent,
        )
    except Exception as exc:
        logger.exception("Manual promo publish failed for channel %s", ch.id)
        await message.answer(f"❌ ارسال ناموفق بود: {exc}")
        return
    interval = await get_interval_hours(db, ch.promo_interval_hours)
    await store.set_promo_state(db, ch.tg_channel_id, msg_id, time.time() + interval * 3600)
    await message.answer(f"✅ پست تبلیغاتی برای کانال #{ch.id} ارسال شد (msg_id={msg_id}).")


@router.message(IsChannelAdmin(), Command("getpromo"))
async def cmd_getpromo(
    message: Message,
    command: CommandObject,
    db: aiosqlite.Connection,
    settings,
) -> None:
    ch = await _resolve_channel(message, command, db, settings)
    if not ch:
        return
    # Get channel-specific promo text, falling back to global.
    key = _CH_PROMO_TEXT.format(cid=ch.id)
    text = await store.get_setting(db, key)
    if not text:
        text = await get_promo_text(db)
    interval = await get_interval_hours(db, ch.promo_interval_hours)
    state = await store.get_promo_state(db)
    next_run = (
        datetime.fromtimestamp(state.next_run_at, UTC).strftime("%Y-%m-%d %H:%M UTC")
        if state and state.channel_id == ch.tg_channel_id
        else "—"
    )
    await message.answer(
        f"📺 کانال #{ch.id} (<code>{ch.tg_channel_id}</code>)\n"
        f"⏱ فاصله: هر <b>{interval:g}</b> ساعت\n"
        f"🕐 ارسال بعدی: {next_run}\n\n"
        "👇 پیش‌نمایش متن فعلی:"
    )
    await message.answer(text, parse_mode=None)


# ── trial settings ───────────────────────────────────────────────────────────


@router.message(IsChannelAdmin(), Command("settrial"))
async def cmd_settrial(
    message: Message,
    command: CommandObject,
    db: aiosqlite.Connection,
    settings,
) -> None:
    """Change trial settings. Usage: /settrial <tg_id> <field> <value>"""
    ch = await _resolve_channel(message, command, db, settings)
    if not ch:
        return
    args = _strip_channel_arg(command).strip().split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "📝 کاربرد: <code>/settrial [-100123] data_limit_gb 10</code>\n"
            "فیلدها: data_limit_gb, days, grace, regrant"
        )
        return
    field_map = {
        "data_limit_gb": "trial_data_limit_gb",
        "days": "trial_days",
        "grace": "on_hold_grace_days",
        "regrant": "allow_regrant_after_days",
    }
    field_key = args[0]
    if field_key not in field_map:
        await message.answer(f"❌ فیلد نامعتبر. فیلدها: {', '.join(field_map.keys())}")
        return
    try:
        value = float(args[1])
    except ValueError:
        await message.answer("❌ مقدار باید عدد باشد.")
        return
    if field_key in ("days", "grace", "regrant"):
        value = int(value)
    if await store.update_channel(db, ch.id, **{field_map[field_key]: value}):
        await message.answer(f"✅ کانال #{ch.id}: {field_key} = {value}")
    else:
        await message.answer("❌ خطا در به‌روزرسانی.")


@router.message(IsChannelAdmin(), Command("setjoindelay"))
async def cmd_setjoindelay(
    message: Message,
    command: CommandObject,
    db: aiosqlite.Connection,
    settings,
) -> None:
    ch = await _resolve_channel(message, command, db, settings)
    if not ch:
        return
    raw = _strip_channel_arg(command).strip()
    try:
        seconds = int(raw)
    except ValueError:
        seconds = -1
    if seconds < 0 or seconds > 3600:
        await message.answer(
            "📝 کاربرد: <code>/setjoindelay [-100123] 10</code>\n(ثانیه، بین ۰ تا ۳۶۰۰)"
        )
        return
    await store.update_channel(db, ch.id, join_approval_delay_seconds=seconds)
    await message.answer(f"✅ تأخیر تأیید کانال #{ch.id}: <b>{seconds}</b> ثانیه.")


@router.message(IsChannelAdmin(), Command("setmaxage"))
async def cmd_setmaxage(
    message: Message,
    command: CommandObject,
    db: aiosqlite.Connection,
    settings,
) -> None:
    ch = await _resolve_channel(message, command, db, settings)
    if not ch:
        return
    raw = _strip_channel_arg(command).strip()
    try:
        days = float(raw)
    except ValueError:
        days = -1
    if days < 0 or days > 3650:
        await message.answer("📝 کاربرد: <code>/setmaxage [-100123] 7</code>\n(۰ = غیرفعال)")
        return
    await store.update_channel(db, ch.id, trial_max_member_age_days=days)
    if days == 0:
        await message.answer(f"✅ محدودیت سابقهٔ عضویت کانال #{ch.id} حذف شد.")
    else:
        await message.answer(f"✅ کانال #{ch.id}: فقط اعضای <b>{days:g} روزه</b> تست می‌گیرن.")


# ── offer groups ─────────────────────────────────────────────────────────────


@router.message(IsChannelAdmin(), Command("groups"))
async def cmd_groups(
    message: Message,
    command: CommandObject,
    db: aiosqlite.Connection,
    settings,
    panel_manager: PanelManager,
) -> None:
    ch = await _resolve_channel(message, command, db, settings)
    if not ch:
        return
    # Show groups from all active panels (for adding new offer groups).
    all_panels = await store.list_panels(db)
    lines = []
    for p in all_panels:
        try:
            groups = await panel_manager.list_groups(p, force=True)
        except Exception:
            lines.append(f"⚠️ پنل #{p.id} ({p.name}): خطا در دریافت گروه‌ها")
            continue
        if groups:
            group_lines = [f"  {gid} — {name}" for gid, name in sorted(groups.items())]
            lines.append(
                f"<b>پنل #{p.id} ({p.name}):</b>\n<code>" + "\n".join(group_lines) + "</code>"
            )
        else:
            lines.append(f"پنل #{p.id} ({p.name}): بدون گروه")
    if not lines:
        await message.answer("هیچ پنل فعالی وجود نداره.")
        return
    await message.answer(
        "📋 <b>گروه‌های موجود:</b>\n\n"
        + "\n\n".join(lines)
        + "\n\nبرای افزودن: <code>/setoffer &lt;tg_id&gt; &lt;panel_id&gt; "
        + "&lt;group_id&gt; &lt;label&gt;</code>"
    )


@router.message(IsChannelAdmin(), Command("offergroups"))
async def cmd_offergroups(
    message: Message,
    command: CommandObject,
    db: aiosqlite.Connection,
    settings,
    panel_manager: PanelManager,
) -> None:
    ch = await _resolve_channel(message, command, db, settings)
    if not ch:
        return
    offers, stale = await trial_service.get_channel_offered_groups(
        panel_manager,
        db,
        ch.id,
    )
    if not offers and not stale:
        await message.answer(
            "لیست گروه‌های پیشنهادی خالیه.\n"
            "افزودن: <code>/setoffer &lt;tg_id&gt; &lt;panel_id&gt; "
            "&lt;group_id&gt; &lt;label&gt;</code>"
        )
        return
    lines = [
        f"{i}. {o.label} <i>(panel={o.panel_id}, group={o.group_id})</i>"
        for i, o in enumerate(offers, 1)
    ]
    text = f"🎯 <b>گروه‌های پیشنهادی کانال #{ch.id}:</b>\n" + "\n".join(lines)
    if stale:
        text += f"\n\n⚠️ گروه‌های حذف‌شده: <code>{stale}</code>"
    await message.answer(text)


@router.message(IsChannelAdmin(), Command("setoffer"))
async def cmd_setoffer(
    message: Message,
    command: CommandObject,
    db: aiosqlite.Connection,
    settings,
) -> None:
    """Add an offer group. Usage: /setoffer <tg_id> <panel_id> <group_id> <label>"""
    ch = await _resolve_channel(message, command, db, settings)
    if not ch:
        return
    args = _strip_channel_arg(command).strip().split(maxsplit=2)
    if len(args) < 3 or not args[0].isdigit() or not args[1].isdigit():
        await message.answer(
            "📝 کاربرد: <code>/setoffer -100123 2 5 🇳🇱 هلند</code>\n"
            "(panel_id=2, group_id=5, label=🇳🇱 هلند)"
        )
        return
    panel_id, group_id = int(args[0]), int(args[1])
    label = args[2].strip()
    if not label:
        await message.answer("📝 برچسب نمی‌تونه خالی باشه.")
        return
    panel = await store.get_panel(db, panel_id)
    if not panel:
        await message.answer(f"❌ پنل #{panel_id} یافت نشد.")
        return
    await store.upsert_channel_offer_group(
        db,
        channel_id=ch.id,
        panel_id=panel_id,
        group_id=group_id,
        label=label,
    )
    await message.answer(f"✅ «{label}» (panel={panel_id}, group={group_id}) اضافه شد.")


@router.message(IsChannelAdmin(), Command("deloffer"))
async def cmd_deloffer(
    message: Message,
    command: CommandObject,
    db: aiosqlite.Connection,
    settings,
) -> None:
    """Remove an offer group.
    In channel: /deloffer <panel_id> <group_id>
    In DM:      /deloffer <channel_tg_id> <panel_id> <group_id>
    """
    ch, args = await _resolve_channel_with_args(message, command, db, settings)
    if not ch:
        return
    if len(args) < 2 or not all(a.isdigit() for a in args[:2]):
        await message.answer(
            "📝 کاربرد: <code>/deloffer [tg_id] &lt;panel_id&gt; &lt;group_id&gt;</code>"
        )
        return
    panel_id, group_id = int(args[0]), int(args[1])
    if await store.delete_channel_offer_group(
        db,
        channel_id=ch.id,
        panel_id=panel_id,
        group_id=group_id,
    ):
        await message.answer(f"✅ گروه {group_id} از پنل #{panel_id} حذف شد.")
    else:
        await message.answer(f"گروه {group_id} در لیست نبود.")


@router.message(IsChannelAdmin(), Command("reorder"))
async def cmd_reorder(
    message: Message,
    command: CommandObject,
    db: aiosqlite.Connection,
    settings,
) -> None:
    """Reorder offer groups. Usage: /reorder <tg_id> <panel>:<group>,<panel>:<group>,..."""
    ch = await _resolve_channel(message, command, db, settings)
    if not ch:
        return
    raw = _strip_channel_arg(command).strip()
    try:
        ordered = []
        for part in raw.split(","):
            p, g = part.strip().split(":")
            ordered.append((int(p), int(g)))
    except (ValueError, AttributeError):
        await message.answer(
            "📝 کاربرد: <code>/reorder -100123 2:5,2:9,3:1</code>\n(panel_id:group_id pairs)"
        )
        return
    await store.reorder_channel_offer_groups(db, ch.id, ordered)
    await message.answer("✅ ترتیب گروه‌ها به‌روزرسانی شد.")


@router.message(IsChannelAdmin(), Command("clearoffers"))
async def cmd_clearoffers(
    message: Message,
    command: CommandObject,
    db: aiosqlite.Connection,
    settings,
) -> None:
    ch = await _resolve_channel(message, command, db, settings)
    if not ch:
        return
    count = await store.clear_channel_offer_groups(db, ch.id)
    await message.answer(f"✅ {count} گروه از کانال #{ch.id} حذف شد.")


# ── grants & stats ───────────────────────────────────────────────────────────


@router.message(IsChannelAdmin(), Command("reset"))
async def cmd_reset(
    message: Message,
    command: CommandObject,
    db: aiosqlite.Connection,
    settings,
) -> None:
    ch = await _resolve_channel(message, command, db, settings)
    if not ch:
        return
    raw = _strip_channel_arg(command).strip().lstrip("@")
    if not raw.isdigit():
        await message.answer("📝 کاربرد: <code>/reset [-100123] 123456789</code>")
        return
    tg_user_id = int(raw)
    if await store.revoke_grant(db, tg_user_id):
        await message.answer(f"✅ تست کاربر <code>{tg_user_id}</code> ریست شد.")
    else:
        await message.answer(f"برای کاربر <code>{tg_user_id}</code> تستی ثبت نشده بود.")


@router.message(IsChannelAdmin(), Command("stats"))
async def cmd_stats(
    message: Message,
    command: CommandObject,
    db: aiosqlite.Connection,
    settings,
) -> None:
    ch = await _resolve_channel(message, command, db, settings)
    if not ch:
        return
    now = datetime.now(UTC)
    day_ago = now - timedelta(days=1)
    lifetime = timedelta(days=ch.on_hold_grace_days + ch.trial_days)

    grants = await store.list_grants(db)
    # Filter grants for this channel (by channel_id or source_chat_id).
    ch_grants = [g for g in grants if g.channel_id == ch.id or g.source_chat_id == ch.tg_channel_id]
    active = [g for g in ch_grants if not g.revoked and g.created_at + lifetime > now]
    joins = await store.count_member_events(db, "join", day_ago)
    leaves = await store.count_member_events(db, "leave", day_ago)
    members = await store.count_chat_members(db, ch.tg_channel_id)
    offers = await store.list_channel_offer_groups(db, ch.id)

    await message.answer(
        f"📊 <b>آمار کانال #{ch.id}</b> (<code>{ch.tg_channel_id}</code>)\n\n"
        f"👥 اعضا: <b>{members}</b>\n"
        f"➕ عضویت ۲۴س: <b>{joins}</b> | ➖ ریزش: <b>{leaves}</b>\n\n"
        f"🎁 تست‌ها: <b>{len(ch_grants)}</b> (فعال: {len(active)})\n"
        f"🎯 گروه‌ها: <b>{len(offers)}</b>\n"
        f"⏸ وضعیت: {'متوقف' if await is_channel_paused(db, ch.id) else 'فعال'}"
    )


@router.message(IsChannelAdmin(), Command("joinstats"))
async def cmd_joinstats(
    message: Message,
    command: CommandObject,
    db: aiosqlite.Connection,
    settings,
) -> None:
    ch = await _resolve_channel(message, command, db, settings)
    if not ch:
        return
    now = datetime.now(UTC)
    day_ago = now - timedelta(days=1)
    epoch = datetime.min.replace(tzinfo=UTC)

    join_requests_24h = await store.count_member_events(db, "join_request", day_ago)
    join_requests_total = await store.count_member_events(db, "join_request", epoch)
    trials_via_join = await store.count_grants_by_source(db, "join_request")
    trials_via_start = await store.count_grants_by_source(db, "start")

    delay = await get_join_delay(db, ch.join_approval_delay_seconds, channel_db_id=ch.id)
    joins_paused = await is_channel_joins_paused(db, ch.id)
    status = "⏸ متوقف" if joins_paused else "▶️ فعال"

    await message.answer(
        f"📊 <b>آمار درخواست عضویت کانال #{ch.id}</b>\n\n"
        f"🔔 درخواست‌های ۲۴س: <b>{join_requests_24h}</b>\n"
        f"📋 کل: <b>{join_requests_total}</b>\n\n"
        f"🎁 تست از join_request: <b>{trials_via_join}</b>\n"
        f"🎁 تست از /start: <b>{trials_via_start}</b>\n\n"
        f"⏱ تأخیر تأیید: <b>{delay}</b> ثانیه\n"
        f"🔄 وضعیت: {status}"
    )


# ── helper ───────────────────────────────────────────────────────────────────


async def _current_promo_text(db: aiosqlite.Connection) -> str:
    return await get_promo_text(db)
