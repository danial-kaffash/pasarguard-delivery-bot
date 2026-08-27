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
    InputMediaAnimation,
    InputMediaPhoto,
    InputMediaVideo,
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
    message_ids: list[int] | None = None  # album sends produce several messages


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


def media_items_from_json(raw: str | None) -> list[dict]:
    """Album items: [{"type": "photo|video|animation", "file_id": "..."}, ...]."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return [
        d
        for d in data
        if isinstance(d, dict)
        and d.get("type") in ("photo", "video", "animation")
        and d.get("file_id")
    ]


def media_items_to_json(items: list[dict]) -> str:
    return json.dumps(
        [{"type": it["type"], "file_id": it["file_id"]} for it in items],
        ensure_ascii=False,
    )


def message_ids_from_json(raw: str | None) -> list[int]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return [i for i in data if isinstance(i, int)] if isinstance(data, list) else []


def is_album(post: ChannelPost) -> bool:
    """An album post carries 2+ media items and sends via send_media_group."""
    return len(media_items_from_json(post.media_json)) >= 2


def post_message_ids(post: ChannelPost) -> list[int]:
    """Every Telegram message id belonging to a published post."""
    ids = message_ids_from_json(post.tg_message_ids_json)
    if ids:
        return ids
    return [post.tg_message_id] if post.tg_message_id else []


async def delete_post_messages(bot: Bot, channel: Channel, post: ChannelPost) -> None:
    """Delete a post's message(s) from the channel; failures are tolerated."""
    for mid in post_message_ids(post):
        try:
            await bot.delete_message(channel.tg_channel_id, mid)
        except TelegramAPIError as exc:
            logger.warning("Could not delete message %s of post %s: %s", mid, post.id, exc)


def _build_media_group(
    post: ChannelPost, entities: list[MessageEntity] | None
) -> list[InputMediaPhoto | InputMediaVideo | InputMediaAnimation]:
    media_cls = {
        "photo": InputMediaPhoto,
        "video": InputMediaVideo,
        "animation": InputMediaAnimation,
    }
    group: list = []
    for i, item in enumerate(media_items_from_json(post.media_json)):
        kwargs: dict = {"media": item["file_id"]}
        if i == 0 and post.text:
            kwargs["caption"] = post.text
            if entities:
                kwargs["caption_entities"] = entities
        group.append(media_cls[item["type"]](**kwargs))
    return group


async def _send_album(
    bot: Bot,
    chat_id: int,
    post: ChannelPost,
    *,
    entities: list[MessageEntity] | None,
    silent: bool,
) -> list[int]:
    """Send an album via send_media_group; returns all message ids."""
    messages = await bot.send_media_group(
        chat_id=chat_id,
        media=_build_media_group(post, entities),
        disable_notification=silent,
    )
    return [m.message_id for m in messages]


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
    """Assemble the kwargs for send_* / edit_* methods (no parse_mode — entities).

    ``link_preview_options`` only exists on text methods — send_photo/video/
    animation and edit_message_caption reject it (TypeError).
    """
    kwargs: dict = {"reply_markup": keyboard}
    if chat_id is not None:
        kwargs["chat_id"] = chat_id
    if post.media_type:
        kwargs["caption"] = post.text or ""
        if entities:
            kwargs["caption_entities"] = entities
    else:
        kwargs["text"] = post.text
        kwargs["link_preview_options"] = LinkPreviewOptions(is_disabled=not post.link_preview)
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
            await delete_post_messages(bot, channel, prev)

    entities = entities_from_json(post.entities_json)

    async def notify_fallback() -> None:
        if fallback_notify_chat_id:
            try:
                await bot.send_message(
                    fallback_notify_chat_id,
                    "⚠️ ایموجی‌های پرمیوم فقط با نام کاربری Fragment قابل ارسال به کانال هستند؛ "
                    "پست بدون آن‌ها ارسال شد. (/checkpremium)",
                )
            except TelegramAPIError:
                pass

    used_fallback = False
    message_ids: list[int] | None = None
    if is_album(post):
        # Albums send via send_media_group (no keyboard support at all).
        try:
            message_ids = await _send_album(
                bot, channel.tg_channel_id, post, entities=entities, silent=post.silent
            )
        except TelegramBadRequest as exc:
            if not _is_custom_emoji_error(exc):
                raise
            logger.warning(
                "Premium emoji rejected for post %s — retrying without: %s", post.id, exc
            )
            message_ids = await _send_album(
                bot,
                channel.tg_channel_id,
                post,
                entities=strip_custom_emoji_entities(entities),
                silent=post.silent,
            )
            used_fallback = True
            await notify_fallback()
        first_id = message_ids[0]
    else:
        buttons = buttons_from_json(post.buttons_json)
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
            logger.warning(
                "Premium emoji rejected for post %s — retrying without: %s", post.id, exc
            )
            sent = await _try_send(
                bot,
                channel.tg_channel_id,
                post,
                entities=strip_custom_emoji_entities(entities),
                keyboard=build_keyboard(buttons, with_icons=False),
                silent=post.silent,
            )
            used_fallback = True
            await notify_fallback()
        first_id = sent.message_id

    if post.pin:
        try:
            await bot.pin_chat_message(
                chat_id=channel.tg_channel_id,
                message_id=first_id,
                disable_notification=True,
            )
        except TelegramAPIError as exc:
            logger.warning("Could not pin post %s: %s", post.id, exc)

    expires_at = None
    if post.ephemeral_hours and post.ephemeral_hours > 0:
        expires_at = (now + timedelta(hours=post.ephemeral_hours)).isoformat()

    return SendResult(
        message_id=first_id,
        used_fallback=used_fallback,
        expires_at=expires_at,
        message_ids=message_ids,
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
    # link_preview_options is only accepted by edit_message_text — the caption
    # variant raises TypeError on it.
    base = {
        "chat_id": channel.tg_channel_id,
        "message_id": post.tg_message_id,
    }

    async def apply(text_kw: str, entities_kw: str, ents, keyboard) -> None:
        method = bot.edit_message_text if text_kw == "text" else bot.edit_message_caption
        kwargs = dict(base)
        kwargs[text_kw] = post.text or ""
        if ents:
            kwargs[entities_kw] = ents
        if text_kw == "text":
            kwargs["link_preview_options"] = LinkPreviewOptions(is_disabled=not post.link_preview)
        if keyboard:
            kwargs["reply_markup"] = keyboard
        await method(**kwargs)

    # Albums cannot carry a keyboard at all — only single-media/text posts.
    keyboard = None if is_album(post) else build_keyboard(buttons, with_icons=True)
    try:
        if post.media_type:
            await apply("caption", "caption_entities", entities, keyboard)
        else:
            await apply("text", "entities", entities, keyboard)
    except TelegramBadRequest as exc:
        if not _is_custom_emoji_error(exc):
            raise
        logger.warning(
            "Premium emoji rejected while editing post %s — retrying without: %s", post.id, exc
        )
        stripped_ents = strip_custom_emoji_entities(entities)
        keyboard_fb = None if is_album(post) else build_keyboard(buttons, with_icons=False)
        if post.media_type:
            await apply("caption", "caption_entities", stripped_ents, keyboard_fb)
        else:
            await apply("text", "entities", stripped_ents, keyboard_fb)
    return True


async def send_preview(bot: Bot, chat_id: int, post: ChannelPost) -> None:
    """Render a draft post to an admin chat — exactly what the channel will see."""
    entities = entities_from_json(post.entities_json)
    if is_album(post):
        await _send_album(bot, chat_id, post, entities=entities, silent=False)
        return
    await _try_send(
        bot,
        chat_id,
        post,
        entities=entities,
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
        "tg_message_ids_json": json.dumps(result.message_ids) if result.message_ids else None,
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
            await store.update_channel_post(
                db, post.id, expires_at=None, tg_message_id=None, tg_message_ids_json=None
            )
            continue
        await delete_post_messages(bot, channel, post)
        await store.update_channel_post(
            db,
            post.id,
            expires_at=None,
            tg_message_id=None,
            tg_message_ids_json=None,
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
