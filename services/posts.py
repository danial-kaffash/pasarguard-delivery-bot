"""Channel posts service — manual & scheduled posting.

Pure-ish logic for the channel-posts feature:
- Button model → InlineKeyboardMarkup (native styles, no-op/copy/url actions,
  premium-emoji button icons with graceful fallback).
- Message entities (incl. custom_emoji spans) preserved from the admin's input.
- Sending with delete-previous, pin, silent, link-preview and ephemeral expiry.
- One-shot + recurring (daily/weekly, Asia/Tehran — fixed +03:30, no DST)
  scheduling and the due-scan tick that drives the scheduler loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from html import escape as html_escape

import aiosqlite
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import (
    CopyTextButton,
    DisabledButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    MessageEntity,
)

from storage import db as store
from storage.db import Channel, ChannelPost

logger = logging.getLogger(__name__)

# Iran abolished DST in 2022 — a fixed offset is correct year-round.
TEHRAN = timezone(timedelta(hours=3, minutes=30), name="Asia/Tehran")

POSTS_TICK_SECONDS = 30.0

BUTTON_STYLES = {"green": "success", "red": "danger", "blue": "primary", None: None}
BUTTON_ACTIONS = ("url", "disabled", "copy")


@dataclass(slots=True)
class SendResult:
    message_id: int
    used_fallback: bool = False
    expires_at: str | None = None


# ── UTF-16 entity math (Telegram entity offsets are UTF-16 code units) ───────


def _utf16_span(text: str, offset: int, length: int) -> tuple[int, int]:
    """Convert a UTF-16 (offset, length) span into code-point indices."""
    start = end = len(text)
    u16 = 0
    for i, ch in enumerate(text):
        if u16 >= offset and start == len(text):
            start = i
        if u16 >= offset + length:
            end = i
            break
        u16 += 2 if ord(ch) > 0xFFFF else 1
    return start, end


def extract_button_icon(text: str, entities: list[dict] | None) -> tuple[str, str, str] | None:
    """Find the first premium-emoji span in a button label.

    Returns (custom_emoji_id, label_without_the_emoji_char, emoji_char),
    or None when the label has no custom emoji.
    """
    for ent in entities or []:
        if ent.get("type") == "custom_emoji" and ent.get("custom_emoji_id"):
            start, end = _utf16_span(text, ent.get("offset", 0), ent.get("length", 0))
            char = text[start:end]
            clean = (text[:start] + text[end:]).strip()
            return ent["custom_emoji_id"], clean, char
    return None


# ── keyboard building ────────────────────────────────────────────────────────


def build_keyboard(buttons: list[dict], *, with_icons: bool = True) -> InlineKeyboardMarkup | None:
    """Render the stored button dicts as an inline keyboard.

    ``with_icons=False`` is the premium-fallback representation: button icons
    are dropped and the original label (with the plain emoji char) is used.
    """
    if not buttons:
        return None
    rows: dict[int, list[InlineKeyboardButton]] = {}
    for i, b in enumerate(buttons):
        icon = b.get("icon")
        use_icon = bool(icon) and with_icons
        label = (b.get("label_clean") if use_icon else None) or b.get("label") or "•"
        kwargs: dict = {"text": label}
        if b.get("style"):
            kwargs["style"] = b["style"]
        if use_icon:
            kwargs["icon_custom_emoji_id"] = icon
        action = b.get("action") or {"type": "disabled"}
        if action.get("type") == "url":
            kwargs["url"] = action.get("url", "")
        elif action.get("type") == "copy":
            kwargs["copy_text"] = CopyTextButton(text=action.get("text", label))
        else:
            kwargs["disabled"] = DisabledButton()
        rows.setdefault(int(b.get("row", i)), []).append(InlineKeyboardButton(**kwargs))
    return InlineKeyboardMarkup(inline_keyboard=[rows[r] for r in sorted(rows)])


def buttons_to_json(buttons: list[dict]) -> str:
    return json.dumps(buttons, ensure_ascii=False)


def buttons_from_json(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return data if isinstance(data, list) else []


# ── entities ─────────────────────────────────────────────────────────────────


def entities_to_json(entities: list[MessageEntity] | None) -> str | None:
    if not entities:
        return None
    return json.dumps([e.model_dump(exclude_none=True) for e in entities], ensure_ascii=False)


def entities_from_json(raw: str | None) -> list[MessageEntity] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return [MessageEntity(**d) for d in data]


def strip_custom_emoji_entities(entities: list[MessageEntity] | None) -> list[MessageEntity] | None:
    """Drop custom-emoji spans, keeping every other entity (bold, links, …)."""
    if entities is None:
        return None
    return [e for e in entities if e.type != "custom_emoji"]


def _is_custom_emoji_error(exc: Exception) -> bool:
    return "emoji" in str(exc).lower()


# ── schedule parsing / recurrence (Asia/Tehran, fixed +03:30) ───────────────


def parse_schedule_input(raw: str, now: datetime) -> datetime:
    """Parse a Tehran-local schedule time into UTC.

    Accepted: ``YYYY-MM-DD HH:MM`` (also with /), or a bare ``HH:MM``
    (today if still in the future, otherwise tomorrow).
    """
    raw = raw.strip()
    parsed = None
    used_time_only = False
    for fmt in ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
        try:
            parsed = datetime.strptime(raw, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        try:
            parsed = datetime.strptime(raw, "%H:%M")
            used_time_only = True
        except ValueError:
            raise ValueError(f"unrecognized time: {raw!r}") from None

    if used_time_only:
        local_now = now.astimezone(TEHRAN)
        parsed = parsed.replace(
            year=local_now.year, month=local_now.month, day=local_now.day, tzinfo=TEHRAN
        )
        if parsed <= local_now:
            parsed += timedelta(days=1)
    else:
        parsed = parsed.replace(tzinfo=TEHRAN)
    return parsed.astimezone(UTC)


def parse_hhmm(raw: str | None) -> tuple[int, int]:
    if not raw:
        raise ValueError("missing HH:MM")
    parts = raw.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"bad HH:MM: {raw!r}")
    hh, mm = int(parts[0]), int(parts[1])
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"bad HH:MM: {raw!r}")
    return hh, mm


def parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    return datetime.fromisoformat(raw)


def next_occurrence(post: ChannelPost, after: datetime) -> datetime:
    """The next UTC run of a recurring post strictly after ``after``."""
    hh, mm = parse_hhmm(post.recur_at)
    after_local = after.astimezone(TEHRAN)
    cand = after_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if cand <= after_local:
        cand += timedelta(days=7 if post.recurrence == "weekly" else 1)
    return cand.astimezone(UTC)


def format_tehran(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.astimezone(TEHRAN).strftime("%Y-%m-%d %H:%M")


# ── sending ──────────────────────────────────────────────────────────────────


def build_send_kwargs(
    post: ChannelPost,
    *,
    entities: list[MessageEntity] | None,
    keyboard: InlineKeyboardMarkup | None,
    chat_id: int | None = None,
) -> dict:
    """Assemble the kwargs for send_* / edit_* methods (no parse_mode — entities)."""
    kwargs: dict = {
        "reply_markup": keyboard,
        "link_preview_options": LinkPreviewOptions(is_disabled=not post.link_preview),
    }
    if chat_id is not None:
        kwargs["chat_id"] = chat_id
    if post.media_type:
        kwargs["caption"] = post.text or ""
        if entities:
            kwargs["caption_entities"] = entities
    else:
        kwargs["text"] = post.text
        if entities:
            kwargs["entities"] = entities
    return kwargs


def _send_method(bot: Bot, post: ChannelPost):
    if post.media_type == "photo":
        return bot.send_photo
    if post.media_type == "video":
        return bot.send_video
    if post.media_type == "animation":
        return bot.send_animation
    return bot.send_message


def _media_kwarg_name(post: ChannelPost) -> str | None:
    return {"photo": "photo", "video": "video", "animation": "animation"}.get(post.media_type or "")


async def _try_send(
    bot: Bot,
    chat_id: int,
    post: ChannelPost,
    *,
    entities: list[MessageEntity] | None,
    keyboard: InlineKeyboardMarkup | None,
    silent: bool,
) -> object:
    kwargs = build_send_kwargs(post, entities=entities, keyboard=keyboard)
    kwargs["disable_notification"] = silent
    media = _media_kwarg_name(post)
    if media:
        kwargs[media] = post.media_file_id
        kwargs.pop("text", None)
    method = _send_method(bot, post)
    return await method(chat_id=chat_id, **kwargs)


async def send_post(
    bot: Bot,
    db: aiosqlite.Connection,
    post: ChannelPost,
    channel: Channel,
    *,
    now: datetime | None = None,
    fallback_notify_chat_id: int | None = None,
) -> SendResult:
    """Send one post to its channel: delete-previous → send → pin → expiry bookkeeping."""
    now = now or datetime.now(UTC)

    if post.delete_previous:
        prev = await store.last_published_post(db, post.channel_id)
        if prev and prev.id != post.id and prev.tg_message_id:
            try:
                await bot.delete_message(channel.tg_channel_id, prev.tg_message_id)
            except TelegramAPIError as exc:
                logger.warning("Could not delete previous post %s: %s", prev.id, exc)

    buttons = buttons_from_json(post.buttons_json)
    entities = entities_from_json(post.entities_json)

    used_fallback = False
    try:
        sent = await _try_send(
            bot,
            channel.tg_channel_id,
            post,
            entities=entities,
            keyboard=build_keyboard(buttons, with_icons=True),
            silent=post.silent,
        )
    except TelegramBadRequest as exc:
        if not _is_custom_emoji_error(exc):
            raise
        logger.warning("Premium emoji rejected for post %s — retrying without: %s", post.id, exc)
        sent = await _try_send(
            bot,
            channel.tg_channel_id,
            post,
            entities=strip_custom_emoji_entities(entities),
            keyboard=build_keyboard(buttons, with_icons=False),
            silent=post.silent,
        )
        used_fallback = True
        if fallback_notify_chat_id:
            try:
                await bot.send_message(
                    fallback_notify_chat_id,
                    "⚠️ ایموجی‌های پرمیوم فقط با نام کاربری Fragment قابل ارسال به کانال هستند؛ "
                    "پست بدون آن‌ها ارسال شد. (/checkpremium)",
                )
            except TelegramAPIError:
                pass

    if post.pin:
        try:
            await bot.pin_chat_message(
                chat_id=channel.tg_channel_id,
                message_id=sent.message_id,
                disable_notification=True,
            )
        except TelegramAPIError as exc:
            logger.warning("Could not pin post %s: %s", post.id, exc)

    expires_at = None
    if post.ephemeral_hours and post.ephemeral_hours > 0:
        expires_at = (now + timedelta(hours=post.ephemeral_hours)).isoformat()

    return SendResult(
        message_id=sent.message_id, used_fallback=used_fallback, expires_at=expires_at
    )


async def edit_published_post(
    bot: Bot,
    post: ChannelPost,
    channel: Channel,
) -> bool:
    """Edit a published post in place (text/caption + buttons; media itself stays)."""
    if not post.tg_message_id:
        return False
    buttons = buttons_from_json(post.buttons_json)
    entities = entities_from_json(post.entities_json)
    base = {
        "chat_id": channel.tg_channel_id,
        "message_id": post.tg_message_id,
        "link_preview_options": LinkPreviewOptions(is_disabled=not post.link_preview),
    }

    async def apply(text_kw: str, entities_kw: str, ents, keyboard) -> None:
        method = bot.edit_message_text if text_kw == "text" else bot.edit_message_caption
        kwargs = dict(base)
        kwargs[text_kw] = post.text or ""
        if ents:
            kwargs[entities_kw] = ents
        if keyboard:
            kwargs["reply_markup"] = keyboard
        await method(**kwargs)

    try:
        if post.media_type:
            await apply(
                "caption", "caption_entities", entities, build_keyboard(buttons, with_icons=True)
            )
        else:
            await apply("text", "entities", entities, build_keyboard(buttons, with_icons=True))
    except TelegramBadRequest as exc:
        if not _is_custom_emoji_error(exc):
            raise
        logger.warning(
            "Premium emoji rejected while editing post %s — retrying without: %s", post.id, exc
        )
        if post.media_type:
            await apply(
                "caption",
                "caption_entities",
                strip_custom_emoji_entities(entities),
                build_keyboard(buttons, with_icons=False),
            )
        else:
            await apply(
                "text",
                "entities",
                strip_custom_emoji_entities(entities),
                build_keyboard(buttons, with_icons=False),
            )
    return True


async def send_preview(bot: Bot, chat_id: int, post: ChannelPost) -> None:
    """Render a draft post to an admin chat — exactly what the channel will see."""
    await _try_send(
        bot,
        chat_id,
        post,
        entities=entities_from_json(post.entities_json),
        keyboard=build_keyboard(buttons_from_json(post.buttons_json)),
        silent=False,
    )


# ── scheduler dispatch ───────────────────────────────────────────────────────


async def send_and_record(
    bot: Bot,
    db: aiosqlite.Connection,
    post: ChannelPost,
    channel: Channel,
    now: datetime,
    fallback_notify_chat_id: int | None = None,
) -> None:
    try:
        result = await send_post(
            bot, db, post, channel, now=now, fallback_notify_chat_id=fallback_notify_chat_id
        )
    except TelegramAPIError as exc:
        logger.exception("Post %s failed to send", post.id)
        await store.update_channel_post(db, post.id, status="failed", error=str(exc)[:500])
        return
    updates: dict = {
        "tg_message_id": result.message_id,
        "sent_at": now.isoformat(),
        "last_sent_at": now.isoformat(),
        "expires_at": result.expires_at,
        "error": None,
    }
    if post.recurrence == "none":
        updates["status"] = "sent"
    else:
        updates["status"] = "recurring"
        updates["scheduled_at"] = next_occurrence(post, now).isoformat()
    await store.update_channel_post(db, post.id, **updates)


async def dispatch_due_posts(
    bot: Bot, db: aiosqlite.Connection, *, fallback_notify_chat_id: int | None = None
) -> int:
    """One scheduler tick: send due one-shot + recurring posts, expire ephemeral ones."""
    now = datetime.now(UTC)
    now_iso = now.isoformat()
    sent = 0

    for post in await store.list_due_scheduled_posts(db, now_iso):
        channel = await store.get_channel(db, post.channel_id)
        if channel is None:
            await store.update_channel_post(
                db, post.id, status="cancelled", error="channel missing"
            )
            continue
        await send_and_record(bot, db, post, channel, now, fallback_notify_chat_id)
        sent += 1

    for post in await store.list_recurring_posts(db):
        scheduled_at = parse_dt(post.scheduled_at)
        if scheduled_at is None or scheduled_at > now:
            continue
        channel = await store.get_channel(db, post.channel_id)
        if channel is None:
            await store.update_channel_post(
                db, post.id, status="cancelled", error="channel missing"
            )
            continue
        await send_and_record(bot, db, post, channel, now, fallback_notify_chat_id)
        sent += 1

    for post in await store.list_expired_posts(db, now_iso):
        channel = await store.get_channel(db, post.channel_id)
        if channel is None:
            await store.update_channel_post(db, post.id, expires_at=None, tg_message_id=None)
            continue
        try:
            await bot.delete_message(channel.tg_channel_id, post.tg_message_id)
        except TelegramAPIError as exc:
            logger.warning("Could not delete expired post %s: %s", post.id, exc)
        await store.update_channel_post(
            db,
            post.id,
            expires_at=None,
            tg_message_id=None,
            status="expired" if post.status == "sent" else post.status,
        )
    return sent


async def run_posts_scheduler(
    bot: Bot,
    db: aiosqlite.Connection,
    settings,
    fallback_notify_chat_id: int | None = None,
) -> None:
    """Background loop: dispatch due posts every POSTS_TICK_SECONDS."""
    if fallback_notify_chat_id is None and getattr(settings, "owner_tg_ids", None):
        fallback_notify_chat_id = settings.owner_tg_ids[0]
    logger.info("Channel-posts scheduler started.")
    while True:
        try:
            await dispatch_due_posts(bot, db, fallback_notify_chat_id=fallback_notify_chat_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Channel-posts scheduler tick failed")
        await asyncio.sleep(POSTS_TICK_SECONDS)


# ── misc presentation helpers ────────────────────────────────────────────────


def post_summary_line(post: ChannelPost) -> str:
    """One-line Persian summary for lists."""
    icons = {
        "scheduled": "⏰",
        "recurring": "🔁",
        "sent": "📌",
        "failed": "❌",
        "cancelled": "🚫",
        "expired": "💨",
        "draft": "📝",
    }
    when = ""
    if post.status == "scheduled" and post.scheduled_at:
        when = format_tehran(parse_dt(post.scheduled_at))
    elif post.status == "recurring":
        when = (
            f"هر {'هفته' if post.recurrence == 'weekly' else 'روز'} {post.recur_at or ''}".strip()
        )
    elif post.status in ("sent", "failed") and post.sent_at:
        when = format_tehran(parse_dt(post.sent_at))
    preview = html_escape((post.text or "بدون متن")[:40].replace("\n", " "))
    return f"{icons.get(post.status, '•')} #{post.id} — {preview}" + (f" ({when})" if when else "")
