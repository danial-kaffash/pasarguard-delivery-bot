"""Backup and restore commands.

/backup  — sends the SQLite database file as a Telegram document
/export  — exports configuration as portable JSON (panels, channels, users, groups)
/import  — restores configuration from a JSON file (upsert semantics)
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from html import escape as html_escape
from io import BytesIO
from pathlib import Path

import aiosqlite
from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Document, Message

from storage import db as store

logger = logging.getLogger(__name__)

router = Router(name="backup")

EXPORT_VERSION = 1


# ═══════════════════════════════════════════════════════════════════════════════
# ── /backup — send the SQLite file ────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


@router.message(Command("backup"))
async def cmd_backup(
    message: Message,
    db: aiosqlite.Connection,
    settings,
) -> None:
    """Send the SQLite database file as a Telegram document."""
    db_path = Path(settings.db_path)
    if not db_path.exists():
        await message.answer("❌ فایل دیتابیس یافت نشد.")
        return

    # Ensure all pending writes are flushed.
    await db.commit()

    try:
        file_bytes = db_path.read_bytes()
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        filename = f"pasarguard_backup_{timestamp}.db"
        doc = BufferedInputFile(file_bytes, filename=filename)
        await message.answer_document(
            document=doc,
            caption=f"💾 بکاپ دیتابیس — {timestamp}\nحجم: {len(file_bytes) / 1024:.1f} KB",
        )
    except Exception as exc:
        logger.exception("Backup failed")
        await message.answer(f"❌ خطا در ارسال بکاپ: {exc}")


# ═══════════════════════════════════════════════════════════════════════════════
# ── /export — JSON configuration export ───────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


@router.message(Command("export"))
async def cmd_export(
    message: Message,
    db: aiosqlite.Connection,
) -> None:
    """Export configuration as a portable JSON file."""
    try:
        data = await _build_export(db)
        json_bytes = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        filename = f"pasarguard_config_{timestamp}.json"
        doc = BufferedInputFile(json_bytes, filename=filename)

        summary = (
            f"📤 خروجی تنظیمات — {timestamp}\n"
            f"🖥 پنل‌ها: {len(data['panels'])} | "
            f"📺 کانال‌ها: {len(data['channels'])} | "
            f"👥 کاربران: {len(data['users'])} | "
            f"🎯 گروه‌ها: {len(data['channel_offer_groups'])}"
        )
        await message.answer_document(document=doc, caption=summary)
    except Exception as exc:
        logger.exception("Export failed")
        await message.answer(f"❌ خطا در خروجی: {exc}")


async def _build_export(db: aiosqlite.Connection) -> dict:
    """Build the export data structure from the database."""
    panels = await store.list_panels(db, active_only=False)
    channels = await store.list_channels(db, active_only=False)
    users = await store.list_users(db)

    # Collect channel admins.
    channel_admins = []
    for ch in channels:
        admins = await store.list_channel_admins(db, ch.id)
        for u in admins:
            channel_admins.append({"tg_user_id": u.tg_user_id, "channel_tg_id": ch.tg_channel_id})

    # Collect offer groups.
    channel_offer_groups = []
    for ch in channels:
        offers = await store.list_channel_offer_groups(db, ch.id)
        for o in offers:
            channel_offer_groups.append({
                "channel_tg_id": ch.tg_channel_id,
                "panel_index": _panel_index(panels, o.panel_id),
                "group_id": o.group_id,
                "label": o.label,
                "sort_order": o.sort_order,
            })

    return {
        "version": EXPORT_VERSION,
        "exported_at": datetime.now(UTC).isoformat(),
        "panels": [
            {
                "name": p.name,
                "base_url": p.base_url,
                "admin_username": p.admin_username,
                "verify_ssl": p.verify_ssl,
                "timeout_seconds": p.timeout_seconds,
                "protocols": p.protocols,
                "auto_delete_days": p.auto_delete_days,
                "active": p.active,
            }
            for p in panels
        ],
        "channels": [
            {
                "tg_channel_id": ch.tg_channel_id,
                "title": ch.title,
                "trial_data_limit_gb": ch.trial_data_limit_gb,
                "trial_days": ch.trial_days,
                "on_hold_grace_days": ch.on_hold_grace_days,
                "allow_regrant_after_days": ch.allow_regrant_after_days,
                "trial_max_member_age_days": ch.trial_max_member_age_days,
                "join_approval_delay_seconds": ch.join_approval_delay_seconds,
                "promo_interval_hours": ch.promo_interval_hours,
                "promo_pin": ch.promo_pin,
                "promo_silent": ch.promo_silent,
                "active": ch.active,
            }
            for ch in channels
        ],
        "users": [
            {"tg_user_id": u.tg_user_id, "role": u.role}
            for u in users
        ],
        "channel_admins": channel_admins,
        "channel_offer_groups": channel_offer_groups,
        # Settings with channel-scoped keys.
        "settings": await _export_settings(db),
    }


def _panel_index(panels: list[store.Panel], panel_id: int) -> int:
    """Find the 0-based index of a panel in the list (by DB id)."""
    for i, p in enumerate(panels):
        if p.id == panel_id:
            return i
    return -1


async def _export_settings(db: aiosqlite.Connection) -> dict[str, str]:
    """Export all settings from the settings table."""
    rows = await db.execute_fetchall("SELECT key, value FROM settings ORDER BY key")
    return {r["key"]: r["value"] for r in rows}


# ═══════════════════════════════════════════════════════════════════════════════
# ── /import — restore from JSON ───────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


@router.message(Command("import"))
async def cmd_import(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: aiosqlite.Connection,
) -> None:
    """Restore configuration from a JSON file.

    Reply to a JSON export file with /import, or send /import with a file attached.
    Panel passwords are NOT included in the export — you'll need to re-enter them
    via /panel or /editpanel after import.
    """
    # Check if the message has a document attached.
    doc = message.document
    if not doc:
        # Check if this is a reply to a message with a document.
        if message.reply_to_message and message.reply_to_message.document:
            doc = message.reply_to_message.document
        else:
            await message.answer(
                "📝 فایل JSON خروجی را ارسال کنید:\n"
                "۱. فایل را بفرستید\n"
                "۲. روی آن ریپلای بزنید و بنویسید: /import"
            )
            return

    if not doc.file_name or not doc.file_name.endswith(".json"):
        await message.answer("❌ فایل باید پسوند .json داشته باشد.")
        return

    try:
        file = await bot.get_file(doc.file_id)
        file_bytes = await bot.download_file(file.file_path)
        data = json.loads(file_bytes.read().decode("utf-8"))
    except Exception as exc:
        await message.answer(f"❌ خطا در خواندن فایل: {exc}")
        return

    version = data.get("version")
    if version != EXPORT_VERSION:
        await message.answer(f"❌ نسخه فایل خروجی ({version}) با نسخه فعلی ({EXPORT_VERSION}) سازگار نیست.")
        return

    try:
        result = await _apply_import(db, bot, data)
        await message.answer(
            f"✅ تنظیمات بازیابی شد:\n\n"
            f"🖥 پنل‌ها: {result['panels']} (رمزها باید دوباره تنظیم شوند)\n"
            f"📺 کانال‌ها: {result['channels']}\n"
            f"👥 کاربران: {result['users']}\n"
            f"🔗 انتسابات: {result['assignments']}\n"
            f"🎯 گروه‌ها: {result['offer_groups']}\n"
            f"⚙️ تنظیمات: {result['settings']}\n\n"
            f"⚠️ رمزهای پنل‌ها ذخیره نشده‌اند.\n"
            f"از /panel → پنل → 🔑 تغییر رمز برای تنظیم آن‌ها استفاده کنید."
        )
    except Exception as exc:
        logger.exception("Import failed")
        await message.answer(f"❌ خطا در بازیابی: {exc}")


async def _apply_import(db: aiosqlite.Connection, bot: Bot, data: dict) -> dict:
    """Apply import data to the database. Returns counts of imported items."""
    counts = {"panels": 0, "channels": 0, "users": 0, "assignments": 0, "offer_groups": 0, "settings": 0}

    # Panels — create or update by name.
    panel_id_map: dict[int, int] = {}  # export_index → db_id
    for i, p_data in enumerate(data.get("panels", [])):
        existing = await _find_panel_by_name(db, p_data["name"])
        if existing:
            await store.update_panel(
                db, existing.id,
                base_url=p_data["base_url"],
                admin_username=p_data["admin_username"],
                verify_ssl=p_data.get("verify_ssl", True),
                timeout_seconds=p_data.get("timeout_seconds", 15.0),
                protocols=p_data.get("protocols", "vless"),
                auto_delete_days=p_data.get("auto_delete_days", 11),
                active=p_data.get("active", True),
            )
            panel_id_map[i] = existing.id
        else:
            panel = await store.create_panel(
                db,
                name=p_data["name"],
                base_url=p_data["base_url"],
                admin_username=p_data["admin_username"],
                admin_password="",  # password not in export
                verify_ssl=p_data.get("verify_ssl", True),
                timeout_seconds=p_data.get("timeout_seconds", 15.0),
                protocols=p_data.get("protocols", "vless"),
                auto_delete_days=p_data.get("auto_delete_days", 11),
            )
            panel_id_map[i] = panel.id
        counts["panels"] += 1

    # Channels — create or update by tg_channel_id.
    channel_id_map: dict[int, int] = {}  # tg_channel_id → db_id
    for ch_data in data.get("channels", []):
        tg_id = ch_data["tg_channel_id"]
        existing = await store.get_channel_by_tg_id(db, tg_id)
        fields = {
            k: ch_data[k] for k in [
                "title", "trial_data_limit_gb", "trial_days", "on_hold_grace_days",
                "allow_regrant_after_days", "trial_max_member_age_days",
                "join_approval_delay_seconds", "promo_interval_hours",
                "promo_pin", "promo_silent", "active",
            ] if k in ch_data
        }
        if existing:
            await store.update_channel(db, existing.id, **fields)
            channel_id_map[tg_id] = existing.id
        else:
            # create_channel doesn't accept 'active' — it defaults to 1.
            create_fields = {k: v for k, v in fields.items() if k != "active"}
            ch = await store.create_channel(db, tg_channel_id=tg_id, **create_fields)
            if "active" in fields and not fields["active"]:
                await store.soft_delete_channel(db, ch.id)
            channel_id_map[tg_id] = ch.id
        counts["channels"] += 1

    # Users.
    for u_data in data.get("users", []):
        await store.upsert_user(
            db, tg_user_id=u_data["tg_user_id"], role=u_data.get("role", "user"),
        )
        counts["users"] += 1

    # Channel admins.
    for ca_data in data.get("channel_admins", []):
        ch_db_id = channel_id_map.get(ca_data["channel_tg_id"])
        if ch_db_id:
            await store.assign_channel_admin(db, ca_data["tg_user_id"], ch_db_id)
            counts["assignments"] += 1

    # Offer groups.
    for og_data in data.get("channel_offer_groups", []):
        ch_db_id = channel_id_map.get(og_data["channel_tg_id"])
        panel_db_id = panel_id_map.get(og_data.get("panel_index", -1))
        if ch_db_id and panel_db_id:
            await store.upsert_channel_offer_group(
                db,
                channel_id=ch_db_id,
                panel_id=panel_db_id,
                group_id=og_data["group_id"],
                label=og_data["label"],
            )
            counts["offer_groups"] += 1

    # Settings.
    for key, value in data.get("settings", {}).items():
        # Skip channel-scoped settings that reference old channel IDs —
        # they'll be recreated when channels are configured.
        await store.set_setting(db, key, value)
        counts["settings"] += 1

    # Try to fetch channel titles from Telegram.
    for tg_id, ch_db_id in channel_id_map.items():
        ch = await store.get_channel(db, ch_db_id)
        if ch and not ch.title:
            try:
                chat = await bot.get_chat(tg_id)
                if chat.title:
                    await store.update_channel(db, ch_db_id, title=chat.title)
            except Exception:
                pass

    return counts


async def _find_panel_by_name(db: aiosqlite.Connection, name: str) -> store.Panel | None:
    """Find a panel by name."""
    panels = await store.list_panels(db, active_only=False)
    for p in panels:
        if p.name == name:
            return p
    return None
