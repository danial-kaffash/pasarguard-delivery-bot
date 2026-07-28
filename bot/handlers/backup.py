"""Backup and restore commands.

/backup  — sends the SQLite database file as a Telegram document
/export  — exports configuration as portable JSON (panels, channels, users, groups)
/import  — restores configuration from a JSON file (upsert semantics)
/restore — full restore from a .db backup file (replaces database, restarts bot)
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime
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
# ── Helpers ───────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


def _get_document(message: Message) -> Document | None:
    """Get the document from the message or its reply."""
    if message.document:
        return message.document
    if message.reply_to_message and message.reply_to_message.document:
        return message.reply_to_message.document
    return None


def _is_valid_sqlite(data: bytes) -> bool:
    """Check if the bytes look like a valid SQLite database."""
    if len(data) < 16:
        return False
    if data[:16] != b"SQLite format 3\x00":
        return False
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            f.write(data)
            tmp_path = f.name
        conn = sqlite3.connect(tmp_path)
        conn.execute("SELECT name FROM sqlite_master LIMIT 1")
        conn.close()
        return True
    except Exception:
        return False
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ═══════════════════════════════════════════════════════════════════════════════
# ── /backup — send the SQLite file ────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


@router.message(Command("backup"))
async def cmd_backup(message: Message, db: aiosqlite.Connection, settings) -> None:
    """Send the SQLite database file as a Telegram document."""
    db_path = Path(settings.db_path)
    if not db_path.exists():
        await message.answer("❌ فایل دیتابیس یافت نشد.")
        return
    await db.commit()
    try:
        file_bytes = db_path.read_bytes()
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        doc = BufferedInputFile(file_bytes, filename=f"pasarguard_backup_{ts}.db")
        await message.answer_document(
            document=doc,
            caption=f"💾 بکاپ دیتابیس — {ts}\nحجم: {len(file_bytes) / 1024:.1f} KB",
        )
    except Exception as exc:
        logger.exception("Backup failed")
        await message.answer(f"❌ خطا در ارسال بکاپ: {exc}")


# ═══════════════════════════════════════════════════════════════════════════════
# ── /restore — full database restore ──────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


@router.message(Command("restore"))
async def cmd_restore(message: Message, bot: Bot, db: aiosqlite.Connection, settings) -> None:
    """Full restore from a .db backup file.

    Reply to a .db backup file with /restore (or send the file with a caption /restore).
    The bot replaces its database and restarts.
    """
    doc = _get_document(message)
    if not doc:
        await message.answer(
            "📝 فایل بکاپ (.db) را ارسال کنید:\n"
            "۱. فایل را بفرستید\n"
            "۲. روی آن ریپلای بزنید و بنویسید: /restore"
        )
        return

    if not doc.file_name or not doc.file_name.endswith(".db"):
        await message.answer("❌ فایل باید پسوند .db داشته باشد (فایل بکاپ SQLite).")
        return

    # Download.
    try:
        tg_file = await bot.get_file(doc.file_id)
        file_bytes = await bot.download_file(tg_file.file_path)
        new_db_bytes = file_bytes.read()
    except Exception as exc:
        await message.answer(f"❌ خطا در دانلود فایل: {exc}")
        return

    # Validate.
    if not _is_valid_sqlite(new_db_bytes):
        await message.answer("❌ فایل معتبر نیست — باید یک فایل SQLite (.db) باشد.")
        return

    db_path = Path(settings.db_path)

    # Backup current DB before overwriting.
    if db_path.exists():
        ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        backup_path = db_path.with_suffix(f".db.pre-restore.{ts}")
        try:
            backup_path.write_bytes(db_path.read_bytes())
        except Exception as exc:
            logger.warning("Could not create pre-restore backup: %s", exc)

    # Close current connection.
    await db.close()

    # Replace.
    try:
        db_path.write_bytes(new_db_bytes)
    except Exception as exc:
        await message.answer(f"❌ خطا در ذخیره فایل: {exc}")
        return

    await message.answer(
        f"✅ دیتابیس بازیابی شد!\n"
        f"📦 حجم: {len(new_db_bytes) / 1024:.1f} KB\n\n"
        f"🔄 ربات در حال ریستارت…\n"
        f"⚠️ رمزهای پنل‌ها ممکن است نیاز به تنظیم مجدد داشته باشند."
    )

    logger.info("Database restored from backup (%d bytes) — restarting.", len(new_db_bytes))
    os.execv(sys.executable, [sys.executable, "-m", "bot.main"])


# ═══════════════════════════════════════════════════════════════════════════════
# ── /export — JSON configuration export ───────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


@router.message(Command("export"))
async def cmd_export(message: Message, db: aiosqlite.Connection) -> None:
    """Export configuration as a portable JSON file."""
    try:
        data = await _build_export(db)
        json_bytes = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        doc = BufferedInputFile(json_bytes, filename=f"pasarguard_config_{ts}.json")
        summary = (
            f"📤 خروجی تنظیمات — {ts}\n"
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
    panels = await store.list_panels(db, active_only=False)
    channels = await store.list_channels(db, active_only=False)
    users = await store.list_users(db)

    channel_admins = []
    for ch in channels:
        for u in await store.list_channel_admins(db, ch.id):
            channel_admins.append({"tg_user_id": u.tg_user_id, "channel_tg_id": ch.tg_channel_id})

    channel_offer_groups = []
    for ch in channels:
        for o in await store.list_channel_offer_groups(db, ch.id):
            channel_offer_groups.append({
                "channel_tg_id": ch.tg_channel_id,
                "panel_index": next((i for i, p in enumerate(panels) if p.id == o.panel_id), -1),
                "group_id": o.group_id,
                "label": o.label,
                "sort_order": o.sort_order,
            })

    settings_rows = await db.execute_fetchall("SELECT key, value FROM settings ORDER BY key")

    return {
        "version": EXPORT_VERSION,
        "exported_at": datetime.now(UTC).isoformat(),
        "panels": [
            {
                "name": p.name, "base_url": p.base_url, "admin_username": p.admin_username,
                "verify_ssl": p.verify_ssl, "timeout_seconds": p.timeout_seconds,
                "protocols": p.protocols, "auto_delete_days": p.auto_delete_days, "active": p.active,
            }
            for p in panels
        ],
        "channels": [
            {
                "tg_channel_id": ch.tg_channel_id, "title": ch.title,
                "trial_data_limit_gb": ch.trial_data_limit_gb, "trial_days": ch.trial_days,
                "on_hold_grace_days": ch.on_hold_grace_days,
                "allow_regrant_after_days": ch.allow_regrant_after_days,
                "trial_max_member_age_days": ch.trial_max_member_age_days,
                "join_approval_delay_seconds": ch.join_approval_delay_seconds,
                "promo_interval_hours": ch.promo_interval_hours,
                "promo_pin": ch.promo_pin, "promo_silent": ch.promo_silent, "active": ch.active,
            }
            for ch in channels
        ],
        "users": [{"tg_user_id": u.tg_user_id, "role": u.role} for u in users],
        "channel_admins": channel_admins,
        "channel_offer_groups": channel_offer_groups,
        "settings": {r["key"]: r["value"] for r in settings_rows},
    }


# ═══════════════════════════════════════════════════════════════════════════════
# ── /import — restore from JSON ───────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


@router.message(Command("import"))
async def cmd_import(message: Message, bot: Bot, db: aiosqlite.Connection) -> None:
    """Restore configuration from a JSON file."""
    doc = _get_document(message)
    if not doc:
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
        tg_file = await bot.get_file(doc.file_id)
        file_bytes = await bot.download_file(tg_file.file_path)
        data = json.loads(file_bytes.read().decode("utf-8"))
    except Exception as exc:
        await message.answer(f"❌ خطا در خواندن فایل: {exc}")
        return

    if data.get("version") != EXPORT_VERSION:
        await message.answer(f"❌ نسخه فایل ({data.get('version')}) سازگار نیست.")
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
    counts = {"panels": 0, "channels": 0, "users": 0, "assignments": 0, "offer_groups": 0, "settings": 0}
    panel_id_map: dict[int, int] = {}
    channel_id_map: dict[int, int] = {}

    for i, p_data in enumerate(data.get("panels", [])):
        existing = await _find_panel_by_name(db, p_data["name"])
        if existing:
            await store.update_panel(
                db, existing.id, base_url=p_data["base_url"],
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
                db, name=p_data["name"], base_url=p_data["base_url"],
                admin_username=p_data["admin_username"], admin_password="",
                verify_ssl=p_data.get("verify_ssl", True),
                timeout_seconds=p_data.get("timeout_seconds", 15.0),
                protocols=p_data.get("protocols", "vless"),
                auto_delete_days=p_data.get("auto_delete_days", 11),
            )
            panel_id_map[i] = panel.id
        counts["panels"] += 1

    for ch_data in data.get("channels", []):
        tg_id = ch_data["tg_channel_id"]
        existing = await store.get_channel_by_tg_id(db, tg_id)
        fields = {k: ch_data[k] for k in [
            "title", "trial_data_limit_gb", "trial_days", "on_hold_grace_days",
            "allow_regrant_after_days", "trial_max_member_age_days",
            "join_approval_delay_seconds", "promo_interval_hours", "promo_pin", "promo_silent", "active",
        ] if k in ch_data}
        if existing:
            await store.update_channel(db, existing.id, **fields)
            channel_id_map[tg_id] = existing.id
        else:
            create_fields = {k: v for k, v in fields.items() if k != "active"}
            ch = await store.create_channel(db, tg_channel_id=tg_id, **create_fields)
            if not fields.get("active", True):
                await store.soft_delete_channel(db, ch.id)
            channel_id_map[tg_id] = ch.id
        counts["channels"] += 1

    for u_data in data.get("users", []):
        await store.upsert_user(db, tg_user_id=u_data["tg_user_id"], role=u_data.get("role", "user"))
        counts["users"] += 1

    for ca_data in data.get("channel_admins", []):
        ch_db_id = channel_id_map.get(ca_data["channel_tg_id"])
        if ch_db_id:
            await store.assign_channel_admin(db, ca_data["tg_user_id"], ch_db_id)
            counts["assignments"] += 1

    for og_data in data.get("channel_offer_groups", []):
        ch_db_id = channel_id_map.get(og_data["channel_tg_id"])
        panel_db_id = panel_id_map.get(og_data.get("panel_index", -1))
        if ch_db_id and panel_db_id:
            await store.upsert_channel_offer_group(
                db, channel_id=ch_db_id, panel_id=panel_db_id,
                group_id=og_data["group_id"], label=og_data["label"],
            )
            counts["offer_groups"] += 1

    for key, value in data.get("settings", {}).items():
        await store.set_setting(db, key, value)
        counts["settings"] += 1

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
    for p in await store.list_panels(db, active_only=False):
        if p.name == name:
            return p
    return None
