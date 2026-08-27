"""Channel posts — /newpost wizard, /posts management, /checkpremium.

/newpost  — guided wizard: channels → content → buttons → layout →
            options → schedule → preview → confirm.
/posts    — list posts of a channel and manage them (send now, cancel,
            reschedule, edit published, delete, copy as new).
/checkpremium — echo diagnostic: can this bot post premium emojis?

All times are Asia/Tehran (+03:30, no DST) and stored as UTC ISO strings.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from html import escape as html_escape

import aiosqlite
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from services import posts as svc
from storage import db as store
from storage.db import Channel, ChannelPost

from .admin import IsChannelAdmin, _get_role

logger = logging.getLogger(__name__)

router = Router(name="posts")

EPHEMERAL_CHOICES: list[float | None] = [None, 1, 6, 12, 24]
MAX_BUTTONS = 20


class PostsCB(CallbackData, prefix="pst"):
    """Type-safe callback data for the posts feature."""

    action: str  # cht, next, badd, bdel, bact, bstyle, otgl, sched, confirm,
    #           cancel, newpost, plist, pview, pact
    tid: int = 0
    extra: str = ""


class PostWizard(StatesGroup):
    """FSM states for the /newpost wizard and the /posts text inputs."""

    picking_channels = State()
    content = State()
    menu = State()  # callback-driven menus (buttons / options / schedule / preview)
    button_label = State()
    button_url = State()
    button_copy = State()
    layout = State()
    schedule_time = State()
    reschedule = State()
    edit_text = State()
    edit_media = State()
    check_premium = State()


# ═══════════════════════════════════════════════════════════════════════════════
# ── Access / small helpers ─────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


async def _accessible_channels(db: aiosqlite.Connection, uid: int, settings) -> list[Channel]:
    """Channels this user may post to: superadmin → all, admin → assigned."""
    if uid in settings.owner_tg_ids:
        return await store.list_channels(db, active_only=True)
    role = await _get_role(db, uid)
    if role == "superadmin":
        return await store.list_channels(db, active_only=True)
    if role == "admin":
        return await store.list_user_channels(db, uid)
    return []


async def _can_manage_channel(
    db: aiosqlite.Connection, uid: int, settings, channel_db_id: int
) -> bool:
    channels = await _accessible_channels(db, uid, settings)
    return any(c.id == channel_db_id for c in channels)


def _btn(text: str, action: str, tid: int = 0, extra: str = "") -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text, callback_data=PostsCB(action=action, tid=tid, extra=extra).pack()
    )


def _ch_title(ch: Channel) -> str:
    return html_escape(ch.title or str(ch.tg_channel_id))


async def _show(message: Message, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    """Edit the current message when possible, otherwise send a new one."""
    try:
        await message.edit_text(text, reply_markup=kb)
    except TelegramAPIError:
        await message.answer(text, reply_markup=kb)


def _empty_post(**overrides) -> ChannelPost:
    """A dummy ChannelPost used for recurrence probes and previews."""
    base = dict(
        id=0,
        channel_id=0,
        group_id=None,
        created_by=0,
        created_at="",
        updated_at="",
        text="",
        entities_json=None,
        media_type=None,
        media_file_id=None,
        media_json=None,
        buttons_json="[]",
        delete_previous=False,
        pin=False,
        silent=False,
        link_preview=True,
        ephemeral_hours=None,
        expires_at=None,
        status="draft",
        scheduled_at=None,
        recurrence="none",
        recur_at=None,
        last_sent_at=None,
        sent_at=None,
        tg_message_id=None,
        tg_message_ids_json=None,
        error=None,
    )
    base.update(overrides)
    return ChannelPost(**base)


# ═══════════════════════════════════════════════════════════════════════════════
# ── Wizard keyboards ───────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


def _channel_picker(channels: list[Channel], selected: list[int]) -> InlineKeyboardMarkup:
    rows = []
    for ch in channels:
        mark = "✅" if ch.id in selected else "▫️"
        rows.append([_btn(f"{mark} {_ch_title(ch)}", "cht", ch.id)])
    rows.append([_btn("➡️ ادامه", "next", extra="content")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _content_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("📚 قالب‌ها", "tpllist")],
            [_btn("➡️ ادامه", "next", extra="buttons")],
        ]
    )


def _buttons_menu(buttons: list[dict]) -> InlineKeyboardMarkup:
    rows = [[_btn("➕ افزودن دکمه", "badd")]]
    if buttons:
        rows.append([_btn("🗑 حذف آخرین", "bdel")])
    rows.append([_btn("➡️ ادامه", "next", extra="layout")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _action_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("🔗 لینک", "bact", extra="url")],
            [_btn("🚫 بدون عملکرد", "bact", extra="disabled")],
            [_btn("📋 کپی متن", "bact", extra="copy")],
            [_btn("❌ انصراف از دکمه", "cancel")],
        ]
    )


def _style_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("🟢 سبز", "bstyle", extra="success")],
            [_btn("🔴 قرمز", "bstyle", extra="danger")],
            [_btn("🔵 آبی", "bstyle", extra="primary")],
            [_btn("⚪️ ساده", "bstyle", extra="none")],
        ]
    )


def _options_menu(opts: dict, channels: list[Channel]) -> InlineKeyboardMarkup:
    default_del = bool(channels[0].post_delete_previous) if channels else False
    del_label = f"🗑 حذف پست قبلی: {'✅' if opts['delete_previous'] else '❌'}"
    if not opts["delete_previous"] and default_del:
        del_label += " (پیش‌فرض کانال: ✅)"
    eph = opts.get("ephemeral_hours")
    eph_label = "💨 موقت: خاموش" if eph is None else f"💨 موقت: {eph:g} ساعت"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn(del_label, "otgl", extra="del_prev")],
            [
                _btn(f"📌 پین: {'✅' if opts['pin'] else '❌'}", "otgl", extra="pin"),
                _btn(f"🔇 بی‌صدا: {'✅' if opts['silent'] else '❌'}", "otgl", extra="silent"),
            ],
            [
                _btn(
                    f"🔗 پیش‌نمایش لینک: {'✅' if opts['link_preview'] else '❌'}",
                    "otgl",
                    extra="preview",
                ),
                _btn(eph_label, "otgl", extra="ephemeral"),
            ],
            [_btn("➡️ ادامه", "next", extra="schedule")],
        ]
    )


def _schedule_menu(data: dict) -> InlineKeyboardMarkup:
    mode = data.get("sched_mode", "immediate")
    rows = []
    now_mark = " ✅" if mode == "immediate" else ""
    rows.append([_btn(f"⚡️ ارسال فوری{now_mark}", "sched", extra="now")])
    if mode == "once" and data.get("sched_at"):
        when_txt = svc.format_tehran(svc.parse_dt(data["sched_at"]))
        rows.append([_btn(f"⏰ زمان‌دار ✅ ({when_txt})", "sched", extra="once")])
    else:
        rows.append([_btn("⏰ زمان‌دار", "sched", extra="once")])
    if mode == "recurring" and data.get("recur_at"):
        every = "روزانه" if data.get("recurrence") == "daily" else "هفتگی"
        recur_btn = _btn(f"🔁 تکرارشونده ✅ ({every} {data['recur_at']})", "sched", extra="recur")
        rows.append([recur_btn])
        rows.append(
            [
                _btn("روزانه", "sched", extra="daily"),
                _btn("هفتگی", "sched", extra="weekly"),
            ]
        )
    else:
        rows.append([_btn("🔁 تکرارشونده", "sched", extra="recur")])
    rows.append(
        [
            _btn(
                f"💾 ذخیره به‌عنوان قالب: {'✅' if data.get('save_template') else '❌'}",
                "sched",
                extra="template",
            )
        ]
    )
    rows.append([_btn("👁 پیش‌نمایش و تأیید", "next", extra="preview")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _preview_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("✅ تأیید و ارسال", "confirm")],
            [_btn("❌ لغو", "cancel")],
        ]
    )


# ═══════════════════════════════════════════════════════════════════════════════
# ── Wizard data helpers ────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


def _default_opts(channels: list[Channel]) -> dict:
    ch0 = channels[0] if channels else None
    return {
        "delete_previous": bool(ch0.post_delete_previous) if ch0 else False,
        "pin": False,
        "silent": False,
        "link_preview": True,
        "ephemeral_hours": None,
    }


def _extract_content(message: Message) -> dict | None:
    """Pull text/entities or media+caption out of any acceptable message."""
    media = None
    if message.photo:
        media = ("photo", message.photo[-1].file_id, message.photo[-1].file_unique_id)
    elif message.video:
        media = ("video", message.video.file_id, message.video.file_unique_id)
    elif message.animation:
        media = ("animation", message.animation.file_id, message.animation.file_unique_id)
    if media:
        return {
            "media_type": media[0],
            "media_file_id": media[1],
            "media_unique_id": media[2],
            "text": message.caption or "",
            "entities": message.caption_entities,
        }
    if message.text is not None:
        return {
            "media_type": None,
            "media_file_id": None,
            "media_unique_id": None,
            "text": message.text,
            "entities": message.entities,
        }
    return None


def _content_fields(data: dict) -> dict:
    """Content columns for create/template/preview from wizard state.

    2+ accumulated media-group items → album post (media_json); a single
    media item → classic single-media post; otherwise a text post.
    """
    items = data.get("media_items") or []
    fields = {
        "text": data.get("text") or "",
        "entities_json": data.get("entities_json"),
        "media_type": data.get("media_type"),
        "media_file_id": data.get("media_file_id"),
        "media_json": None,
    }
    if len(items) == 1:
        fields["media_type"] = items[0]["type"]
        fields["media_file_id"] = items[0]["file_id"]
    elif len(items) >= 2:
        fields["media_type"] = "album"
        fields["media_file_id"] = None
        fields["media_json"] = svc.media_items_to_json(items)
    return fields


def _is_album_data(data: dict) -> bool:
    return len(data.get("media_items") or []) >= 2


def _has_content(data: dict) -> bool:
    return (
        data.get("text") is not None
        or bool(data.get("media_type"))
        or bool(data.get("media_items"))
    )


def _apply_layout(data: dict) -> list[dict]:
    """Assign row indexes to buttons from the stored layout (e.g. [2, 1])."""
    buttons = [dict(b) for b in data.get("buttons", [])]
    if not buttons:
        return []
    layout = data.get("layout")
    if not layout:
        for i, b in enumerate(buttons):
            b["row"] = i
        return buttons
    idx = 0
    for row_i, count in enumerate(layout):
        for _ in range(count):
            if idx < len(buttons):
                buttons[idx]["row"] = row_i
                idx += 1
    while idx < len(buttons):  # overflow → one extra row
        buttons[idx]["row"] = len(layout)
        idx += 1
    return buttons


def parse_layout(raw: str | None) -> list[int] | None:
    """Parse '2,1' → [2, 1]; '-' → None (one button per row)."""
    raw = (raw or "").strip()
    if raw in ("-", "0", ""):
        return None
    try:
        parts = [int(p) for p in raw.replace("،", ",").split(",")]
    except ValueError:
        raise ValueError("bad layout") from None
    if not parts or any(p < 1 or p > 8 for p in parts):
        raise ValueError("bad layout")
    return parts


def _draft_post(data: dict) -> ChannelPost:
    """Materialize wizard state as a ChannelPost (for preview/sending)."""
    opts = data.get("opts") or {}
    content = _content_fields(data)
    return _empty_post(
        text=content["text"],
        entities_json=content["entities_json"],
        media_type=content["media_type"],
        media_file_id=content["media_file_id"],
        media_json=content["media_json"],
        buttons_json=svc.buttons_to_json(_apply_layout(data)),
        delete_previous=bool(opts.get("delete_previous")),
        pin=bool(opts.get("pin")),
        silent=bool(opts.get("silent")),
        link_preview=opts.get("link_preview", True),
        ephemeral_hours=opts.get("ephemeral_hours"),
        recurrence=data.get("recurrence", "none"),
        recur_at=data.get("recur_at"),
    )


def _schedule_menu_text(data: dict) -> str:
    mode = data.get("sched_mode", "immediate")
    if mode == "once":
        when = data.get("sched_at")
        when_txt = svc.format_tehran(svc.parse_dt(when)) if when else "تنظیم نشده"
        return (
            f"⏰ <b>زمان‌بندی</b>\n\nارسال زمان‌دار: {when_txt} (تهران)\n"
            "برای تغییر، دکمهٔ زمان‌دار را بزنید."
        )
    if mode == "recurring":
        every = "روزانه" if data.get("recurrence") == "daily" else "هفتگی"
        at = data.get("recur_at") or "تنظیم نشده"
        return (
            f"🔁 <b>زمان‌بندی</b>\n\nتکرار {every} ساعت {at} (تهران)\n"
            "برای تغییر، دکمهٔ تکرارشونده را بزنید."
        )
    return "⚡️ <b>زمان‌بندی</b>\n\nارسال فوری بعد از تأیید."


async def _enter_content_step(message: Message, state: FSMContext) -> None:
    await state.set_state(PostWizard.content)
    await message.answer(
        "✍️ <b>محتوا</b>\n\nمتن، عکس، ویدیو یا گیف پست را بفرستید "
        "(فوروارد هم قبول است — متن، فونت و ایموجی‌ها حفظ می‌شوند).",
        reply_markup=_content_menu(),
    )


async def _send_preview_and_confirm(message: Message, bot: Bot, data: dict) -> None:
    """Render the real post in the admin chat, then ask for confirmation."""
    try:
        await svc.send_preview(bot, message.chat.id, _draft_post(data))
    except TelegramAPIError as exc:
        logger.warning("Preview failed: %s", exc)
        await message.answer("⚠️ پیش‌نمایش ساخته نشد؛ با تأیید، ارسال تلاش می‌شود.")
    await message.answer("👆 پیش‌نمایش — تأیید می‌کنید؟", reply_markup=_preview_menu())


# ═══════════════════════════════════════════════════════════════════════════════
# ── Command entry points (registered first so they win over state handlers) ─
# ═══════════════════════════════════════════════════════════════════════════════


@router.message(IsChannelAdmin(), Command("newpost"))
async def cmd_newpost(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    db: aiosqlite.Connection,
    settings,
) -> None:
    """Start the post wizard. /newpost [channel_tg_id] preselects one channel."""
    channels = await _accessible_channels(db, message.from_user.id, settings)
    if not channels:
        await message.answer("❌ هیچ کانالی در دسترس شما نیست.")
        return

    raw = (command.args or "").strip()
    selected: list[int] = []
    if raw:
        if not raw.lstrip("-").isdigit():
            await message.answer("📝 کاربرد: <code>/newpost -1001234567890</code>")
            return
        ch = await store.get_channel_by_tg_id(db, int(raw))
        if not ch or ch.id not in {c.id for c in channels}:
            await message.answer("❌ کانال یافت نشد یا در دسترس شما نیست.")
            return
        selected = [ch.id]

    await state.set_state(PostWizard.picking_channels)
    await state.update_data(
        wizard=True,
        selected=selected,
        buttons=[],
        layout=None,
        opts=_default_opts(channels),
        sched_mode="immediate",
        recurrence="none",
        recur_at=None,
        sched_at=None,
        save_template=False,
    )
    if selected:
        await _enter_content_step(message, state)
    else:
        await message.answer(
            "📝 <b>پست جدید</b>\n\nکانال(های) مقصد را انتخاب کنید:",
            reply_markup=_channel_picker(channels, selected),
        )


@router.message(IsChannelAdmin(), Command("posts"))
async def cmd_posts(
    message: Message,
    command: CommandObject,
    db: aiosqlite.Connection,
    settings,
) -> None:
    """List posts of a channel: /posts [channel_tg_id]."""
    channels = await _accessible_channels(db, message.from_user.id, settings)
    if not channels:
        await message.answer("❌ هیچ کانالی در دسترس شما نیست.")
        return
    raw = (command.args or "").strip()
    if raw:
        if not raw.lstrip("-").isdigit():
            await message.answer("📝 کاربرد: <code>/posts -1001234567890</code>")
            return
        ch = await store.get_channel_by_tg_id(db, int(raw))
        if not ch or ch.id not in {c.id for c in channels}:
            await message.answer("❌ کانال یافت نشد یا در دسترس شما نیست.")
            return
        text, kb = await render_posts_view(db, ch)
        await message.answer(text, reply_markup=kb)
        return
    if len(channels) == 1:
        text, kb = await render_posts_view(db, channels[0])
        await message.answer(text, reply_markup=kb)
        return
    rows = [[_btn(f"📺 {_ch_title(c)}", "plist", c.id)] for c in channels]
    await message.answer(
        "📭 کانال را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


@router.message(IsChannelAdmin(), Command("checkpremium"))
async def cmd_checkpremium(message: Message, state: FSMContext) -> None:
    await state.set_state(PostWizard.check_premium)
    await message.answer(
        "🔎 یک پیام حاوی ایموجی پرمیوم بفرستید تا بات آن را با همان ایموجی‌ها "
        "بازگرداند و ببینید ارسال آن ممکن است یا نه."
    )


@router.message(StateFilter(PostWizard), Command("cancel"))
async def cmd_wizard_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("🚫 عملیات لغو شد.")


# ═══════════════════════════════════════════════════════════════════════════════
# ── Wizard navigation (callbacks) ──────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


@router.callback_query(PostsCB.filter(F.action == "cht"))
async def on_channel_toggle(
    callback: CallbackQuery,
    callback_data: PostsCB,
    state: FSMContext,
    db: aiosqlite.Connection,
    settings,
) -> None:
    channels = await _accessible_channels(db, callback.from_user.id, settings)
    if callback_data.tid not in {c.id for c in channels}:
        await callback.answer("به این کانال دسترسی ندارید.", show_alert=True)
        return
    data = await state.get_data()
    selected: list[int] = list(data.get("selected") or [])
    if callback_data.tid in selected:
        selected.remove(callback_data.tid)
    else:
        selected.append(callback_data.tid)
    await state.update_data(selected=selected)
    await callback.message.edit_text(
        "📝 <b>پست جدید</b>\n\nکانال(های) مقصد را انتخاب کنید:",
        reply_markup=_channel_picker(channels, selected),
    )
    await callback.answer()


@router.callback_query(PostsCB.filter(F.action == "next"))
async def on_wizard_next(
    callback: CallbackQuery, callback_data: PostsCB, state: FSMContext
) -> None:
    """The ➡️ ادامه button: route to the step named in `extra`."""
    step = callback_data.extra
    data = await state.get_data()

    if step == "content":
        if not data.get("selected"):
            await callback.answer("حداقل یک کانال انتخاب کنید.", show_alert=True)
            return
        await _enter_content_step(callback.message, state)
    elif step == "buttons":
        if not _has_content(data):
            await callback.answer("اول محتوا را بفرستید.", show_alert=True)
            return
        if _is_album_data(data):
            # Albums send via sendMediaGroup — Telegram allows no keyboard.
            await callback.answer("آلبوم‌ها دکمه ندارند؛ می‌رویم سراغ گزینه‌ها.")
            await state.set_state(PostWizard.menu)
            opts = data.get("opts") or _default_opts([])
            await _show(callback.message, "⚙️ <b>گزینه‌ها</b>", _options_menu(opts, []))
            return
        await state.set_state(PostWizard.menu)
        await _show(
            callback.message,
            "🎛 <b>دکمه‌ها</b>\n\n«افزودن دکمه» متن دکمه را می‌پرسد؛ "
            "ایموجی پرمیوم در ابتدای متن به‌عنوان آیکون دکمه استفاده می‌شود.",
            _buttons_menu(data.get("buttons") or []),
        )
    elif step == "layout":
        if _is_album_data(data) or not (data.get("buttons") or []):
            # Nothing to lay out — straight to options.
            await state.set_state(PostWizard.menu)
            opts = data.get("opts") or _default_opts([])
            await _show(callback.message, "⚙️ <b>گزینه‌ها</b>", _options_menu(opts, []))
            await callback.answer()
            return
        await state.set_state(PostWizard.layout)
        await _show(
            callback.message,
            "📐 <b>چیدمان دکمه‌ها</b>\n\nتعداد دکمه در هر ردیف را بفرستید، مثلاً "
            "<code>2,1</code> (دو ردیف اول، یکی ردیف دوم).\n"
            "<code>-</code> یعنی هر دکمه در یک ردیف.",
            InlineKeyboardMarkup(inline_keyboard=[[_btn("⏭ رد کردن", "next", extra="options")]]),
        )
    elif step == "options":
        await state.set_state(PostWizard.menu)
        data = await state.get_data()
        opts = data.get("opts") or _default_opts([])
        await _show(callback.message, "⚙️ <b>گزینه‌ها</b>", _options_menu(opts, []))
    elif step == "schedule":
        await state.set_state(PostWizard.menu)
        await _show(callback.message, _schedule_menu_text(data), _schedule_menu(data))
    elif step == "preview":
        await state.set_state(PostWizard.menu)
        await _send_preview_and_confirm(callback.message, callback.bot, data)
    await callback.answer()


@router.callback_query(PostsCB.filter(F.action == "newpost"))
async def on_newpost_shortcut(
    callback: CallbackQuery,
    callback_data: PostsCB,
    state: FSMContext,
    db: aiosqlite.Connection,
    settings,
) -> None:
    """➕ پست جدید from the posts list — start the wizard with this channel."""
    if not await _can_manage_channel(db, callback.from_user.id, settings, callback_data.tid):
        await callback.answer("به این کانال دسترسی ندارید.", show_alert=True)
        return
    ch = await store.get_channel(db, callback_data.tid)
    await state.update_data(
        wizard=True,
        selected=[ch.id],
        buttons=[],
        layout=None,
        opts=_default_opts([ch]),
        sched_mode="immediate",
        recurrence="none",
        recur_at=None,
        sched_at=None,
        save_template=False,
    )
    await _enter_content_step(callback.message, state)
    await callback.answer()


# ═══════════════════════════════════════════════════════════════════════════════
# ── Content input ──────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


@router.message(PostWizard.content)
async def input_content(message: Message, state: FSMContext) -> None:
    content = _extract_content(message)
    if content is None:
        await message.answer("❌ فقط متن، عکس، ویدیو یا گیف. فایل و ویس پشتیبانی نمی‌شود.")
        return
    data = await state.get_data()

    if content["media_type"] and message.media_group_id:
        # Part of a media group (album) — accumulate items; caption taken from
        # whichever group message carries one.
        items = list(data.get("media_items") or [])
        item = {
            "type": content["media_type"],
            "file_id": content["media_file_id"],
            "unique": content["media_unique_id"],
        }
        if not any(it.get("unique") == item["unique"] for it in items):
            items.append(item)
        await state.update_data(
            media_items=items,
            text=content["text"] or data.get("text") or "",
            entities_json=(
                svc.entities_to_json(content["entities"] or None)
                if content["entities"]
                else data.get("entities_json")
            ),
            media_type=None,
            media_file_id=None,
        )
        await message.answer(
            f"✅ آلبوم: {len(items)} آیتم ذخیره شد "
            "(آیتم‌های بعدی گروه را هم بفرستید، بعد «➡️ ادامه»).",
            reply_markup=_content_menu(),
        )
        return

    if content["media_type"]:
        # A single media message replaces everything (albums are built only
        # from real media groups).
        await state.update_data(
            text=content["text"],
            entities_json=svc.entities_to_json(content["entities"] or None),
            media_type=content["media_type"],
            media_file_id=content["media_file_id"],
            media_items=None,
        )
        await message.answer(
            "✅ محتوا ذخیره شد (برای عوض کردن، دوباره بفرستید).",
            reply_markup=_content_menu(),
        )
        return

    # Text message: caption when an album is being assembled, else a text post.
    if data.get("media_items"):
        await state.update_data(
            text=content["text"],
            entities_json=svc.entities_to_json(content["entities"] or None),
        )
        await message.answer("✅ کپشن آلبوم ذخیره شد.", reply_markup=_content_menu())
        return
    await state.update_data(
        text=content["text"],
        entities_json=svc.entities_to_json(content["entities"] or None),
        media_type=None,
        media_file_id=None,
        media_items=None,
    )
    await message.answer(
        "✅ محتوا ذخیره شد (برای عوض کردن، دوباره بفرستید).", reply_markup=_content_menu()
    )


# ═══════════════════════════════════════════════════════════════════════════════
# ── Templates (start from a saved template) ────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


def _template_line(tpl: store.PostTemplate) -> str:
    preview = html_escape((tpl.text or tpl.media_type or "بدون متن")[:40].replace("\n", " "))
    media = "🖼 " if tpl.media_type else ""
    return f"{media}#{tpl.id} {html_escape(tpl.name or 'بدون نام')} — {preview}"


def _templates_menu(templates: list[store.PostTemplate]) -> InlineKeyboardMarkup:
    rows = []
    for tpl in templates:
        rows.append(
            [
                _btn(f"✅ #{tpl.id}", "tpl", tpl.id),
                _btn(f"🗑 #{tpl.id}", "tpldel", tpl.id),
            ]
        )
    rows.append([_btn("↩️ بازگشت به محتوا", "contentmenu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(PostsCB.filter(F.action == "tpllist"))
async def on_templates_list(
    callback: CallbackQuery, state: FSMContext, db: aiosqlite.Connection
) -> None:
    data = await state.get_data()
    if not data.get("wizard"):
        await callback.answer("ویزارد فعال نیست؛ /newpost بزنید.", show_alert=True)
        return
    templates = await store.list_post_templates(db, limit=20)
    if not templates:
        await callback.answer(
            "قالبی ذخیره نشده — با کلید «💾 قالب» در زمان‌بندی ذخیره کنید.", show_alert=True
        )
        return
    text = (
        "📚 <b>قالب‌ها</b>\n\n"
        + "\n".join(_template_line(t) for t in templates)
        + "\n\n✅ بارگذاری · 🗑 حذف"
    )
    await _show(callback.message, text, _templates_menu(templates))
    await callback.answer()


@router.callback_query(PostsCB.filter(F.action == "tpl"))
async def on_template_pick(
    callback: CallbackQuery, callback_data: PostsCB, state: FSMContext, db: aiosqlite.Connection
) -> None:
    data = await state.get_data()
    if not data.get("wizard"):
        await callback.answer("ویزارد فعال نیست.", show_alert=True)
        return
    tpl = await store.get_post_template(db, callback_data.tid)
    if not tpl:
        await callback.answer("قالب یافت نشد.", show_alert=True)
        return
    # Albums come back as media_items so the wizard can keep accumulating.
    tpl_items = svc.media_items_from_json(tpl.media_json)
    media_items = [
        {"type": it["type"], "file_id": it["file_id"], "unique": it["file_id"]} for it in tpl_items
    ] or None
    await state.update_data(
        text=tpl.text,
        entities_json=tpl.entities_json,
        media_type=tpl.media_type if not media_items else None,
        media_file_id=tpl.media_file_id if not media_items else None,
        media_items=media_items,
        buttons=svc.buttons_from_json(tpl.buttons_json),
        layout=None,
        opts={
            "delete_previous": tpl.delete_previous,
            "pin": tpl.pin,
            "silent": tpl.silent,
            "link_preview": tpl.link_preview,
            "ephemeral_hours": tpl.ephemeral_hours,
        },
    )
    await state.set_state(PostWizard.content)
    await _show(
        callback.message,
        f"✅ قالب #{tpl.id} بارگذاری شد — می‌توانید محتوا را عوض کنید یا «➡️ ادامه» را بزنید.",
        _content_menu(),
    )
    await callback.answer()


@router.callback_query(PostsCB.filter(F.action == "tpldel"))
async def on_template_delete(
    callback: CallbackQuery, callback_data: PostsCB, state: FSMContext, db: aiosqlite.Connection
) -> None:
    data = await state.get_data()
    if not data.get("wizard"):
        await callback.answer("ویزارد فعال نیست.", show_alert=True)
        return
    await store.delete_post_template(db, callback_data.tid)
    templates = await store.list_post_templates(db, limit=20)
    if not templates:
        await state.set_state(PostWizard.content)
        await _show(callback.message, "🗑 قالب حذف شد. قالب دیگری نیست.", _content_menu())
    else:
        text = (
            "📚 <b>قالب‌ها</b>\n\n"
            + "\n".join(_template_line(t) for t in templates)
            + "\n\n✅ بارگذاری · 🗑 حذف"
        )
        await _show(callback.message, text, _templates_menu(templates))
    await callback.answer("حذف شد.")


@router.callback_query(PostsCB.filter(F.action == "contentmenu"))
async def on_content_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PostWizard.content)
    await _show(
        callback.message,
        "✍️ <b>محتوا</b>\n\nمتن، عکس، ویدیو یا گیف پست را بفرستید "
        "(فوروارد هم قبول است — متن، فونت و ایموجی‌ها حفظ می‌شوند).",
        _content_menu(),
    )
    await callback.answer()


# ═══════════════════════════════════════════════════════════════════════════════
# ── Buttons loop ───────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


@router.callback_query(PostsCB.filter(F.action == "badd"))
async def on_add_button(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if len(data.get("buttons") or []) >= MAX_BUTTONS:
        await callback.answer("حداکثر تعداد دکمه‌ها رسیده است.", show_alert=True)
        return
    await state.set_state(PostWizard.button_label)
    await _show(callback.message, "🏷 متن دکمه را بفرستید:")
    await callback.answer()


@router.callback_query(PostsCB.filter(F.action == "bdel"))
async def on_del_button(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    buttons: list[dict] = list(data.get("buttons") or [])
    if buttons:
        buttons.pop()
        await state.update_data(buttons=buttons)
    await state.set_state(PostWizard.menu)
    await callback.message.edit_text("🎛 <b>دکمه‌ها</b>", reply_markup=_buttons_menu(buttons))
    await callback.answer()


@router.message(PostWizard.button_label)
async def input_button_label(message: Message, state: FSMContext) -> None:
    if not message.text:
        await message.answer("❌ متن دکمه را بفرستید.")
        return
    entities = [e.model_dump(exclude_none=True) for e in (message.entities or [])]
    pending: dict = {"label": message.text}
    icon = svc.extract_button_icon(message.text, entities)
    if icon:
        custom_id, clean, char = icon
        pending.update(icon=custom_id, label_clean=clean, label_fallback=f"{char} {clean}")
    await state.update_data(pending_button=pending)
    await state.set_state(PostWizard.menu)
    await message.answer("⚡️ عملکرد دکمه را انتخاب کنید:", reply_markup=_action_menu())


@router.callback_query(PostsCB.filter(F.action == "bact"))
async def on_button_action(
    callback: CallbackQuery, callback_data: PostsCB, state: FSMContext
) -> None:
    action = callback_data.extra
    if action == "url":
        await state.set_state(PostWizard.button_url)
        await _show(callback.message, "🔗 لینک دکمه را بفرستید (https:// یا tg://):")
    elif action == "copy":
        await state.set_state(PostWizard.button_copy)
        await _show(callback.message, "📋 متنی که با کلیک روی دکمه کپی می‌شود را بفرستید:")
    else:
        data = await state.get_data()
        pending: dict = data.get("pending_button") or {}
        pending["action"] = {"type": "disabled"}
        await state.update_data(pending_button=pending)
        await state.set_state(PostWizard.menu)
        await _ask_style(callback.message)
    await callback.answer()


@router.message(PostWizard.button_url)
async def input_button_url(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not (raw.startswith(("http://", "https://", "tg://"))):
        prefix_msg = "❌ لینک باید با http:// یا https:// یا tg:// شروع شود. دوباره بفرستید:"
        await message.answer(prefix_msg)
        return
    data = await state.get_data()
    pending: dict = data.get("pending_button") or {}
    pending["action"] = {"type": "url", "url": raw}
    await state.update_data(pending_button=pending)
    await state.set_state(PostWizard.menu)
    await _ask_style(message)


@router.message(PostWizard.button_copy)
async def input_button_copy(message: Message, state: FSMContext) -> None:
    if not message.text:
        await message.answer("❌ متن را بفرستید.")
        return
    data = await state.get_data()
    pending: dict = data.get("pending_button") or {}
    pending["action"] = {"type": "copy", "text": message.text}
    await state.update_data(pending_button=pending)
    await state.set_state(PostWizard.menu)
    await _ask_style(message)


async def _ask_style(message: Message) -> None:
    await message.answer("🎨 رنگ دکمه را انتخاب کنید:", reply_markup=_style_menu())


@router.callback_query(PostsCB.filter(F.action == "bstyle"))
async def on_button_style(
    callback: CallbackQuery, callback_data: PostsCB, state: FSMContext
) -> None:
    style = callback_data.extra
    data = await state.get_data()
    pending: dict = data.get("pending_button") or {}
    if not pending:
        await callback.answer("دکمه‌ای در حال ساخت نیست.", show_alert=True)
        return
    if style != "none":
        pending["style"] = style
    pending.setdefault("action", {"type": "disabled"})
    buttons: list[dict] = list(data.get("buttons") or [])
    buttons.append(pending)
    await state.update_data(buttons=buttons, pending_button=None)
    await state.set_state(PostWizard.menu)
    await callback.message.edit_text(
        f"✅ دکمهٔ «{html_escape(pending['label'])}» اضافه شد ({len(buttons)} دکمه).",
        reply_markup=_buttons_menu(buttons),
    )
    await callback.answer()


# ═══════════════════════════════════════════════════════════════════════════════
# ── Layout input ───────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


@router.message(PostWizard.layout)
async def input_layout(message: Message, state: FSMContext) -> None:
    try:
        layout = parse_layout(message.text)
    except ValueError:
        await message.answer(
            "❌ فرمت نامعتبر. مثال: <code>2,1</code> یا <code>-</code>. دوباره بفرستید:"
        )
        return
    await state.update_data(layout=layout)
    data = await state.get_data()
    opts = data.get("opts") or _default_opts([])
    await state.set_state(PostWizard.menu)
    await message.answer("⚙️ <b>گزینه‌ها</b>", reply_markup=_options_menu(opts, []))


# ═══════════════════════════════════════════════════════════════════════════════
# ── Options toggles ────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


@router.callback_query(PostsCB.filter(F.action == "otgl"))
async def on_option_toggle(
    callback: CallbackQuery, callback_data: PostsCB, state: FSMContext
) -> None:
    data = await state.get_data()
    opts: dict = dict(data.get("opts") or _default_opts([]))
    which = callback_data.extra
    if which == "ephemeral":
        current = opts.get("ephemeral_hours")
        try:
            idx = EPHEMERAL_CHOICES.index(current)
        except ValueError:
            idx = 0
        opts["ephemeral_hours"] = EPHEMERAL_CHOICES[(idx + 1) % len(EPHEMERAL_CHOICES)]
    elif which in ("del_prev", "pin", "silent", "preview"):
        key = {"del_prev": "delete_previous", "preview": "link_preview"}.get(which, which)
        opts[key] = not opts.get(key, key == "link_preview")
    else:
        await callback.answer()
        return
    await state.update_data(opts=opts)
    await callback.message.edit_text("⚙️ <b>گزینه‌ها</b>", reply_markup=_options_menu(opts, []))
    await callback.answer()


# ═══════════════════════════════════════════════════════════════════════════════
# ── Schedule choices ───────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


@router.callback_query(PostsCB.filter(F.action == "sched"))
async def on_schedule_choice(
    callback: CallbackQuery, callback_data: PostsCB, state: FSMContext
) -> None:
    which = callback_data.extra
    data = await state.get_data()
    if which == "now":
        await state.update_data(
            sched_mode="immediate", sched_at=None, recurrence="none", recur_at=None
        )
    elif which == "once":
        await state.update_data(sched_mode="once")
        await state.set_state(PostWizard.schedule_time)
        await _show(
            callback.message,
            "⏰ زمان ارسال را بفرستید (به وقت تهران):\n"
            "<code>2026-08-28 14:30</code> یا فقط <code>14:30</code>",
        )
    elif which == "recur":
        await state.update_data(sched_mode="recurring")
        await state.set_state(PostWizard.schedule_time)
        await _show(
            callback.message,
            "🔁 ساعت تکرار را بفرستید (به وقت تهران)، مثلاً <code>09:00</code>:",
        )
    elif which in ("daily", "weekly"):
        if not data.get("recur_at"):
            await callback.answer("اول ساعت تکرار را بفرستید.", show_alert=True)
            return
        await state.update_data(recurrence=which, sched_mode="recurring")
    elif which == "template":
        await state.update_data(save_template=not data.get("save_template"))
    data = await state.get_data()
    await state.set_state(PostWizard.menu)
    await _show(callback.message, _schedule_menu_text(data), _schedule_menu(data))
    await callback.answer()


@router.message(PostWizard.schedule_time)
async def input_schedule_time(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    mode = data.get("sched_mode", "once")
    now = datetime.now(UTC)
    try:
        if mode == "recurring":
            hh, mm = svc.parse_hhmm(message.text)
            recur_at = f"{hh:02d}:{mm:02d}"
            probe = _empty_post(recurrence=data.get("recurrence", "daily"), recur_at=recur_at)
            nxt = svc.next_occurrence(probe, now)
            await state.update_data(recur_at=recur_at, scheduled_at=nxt.isoformat())
        else:
            when = svc.parse_schedule_input(message.text or "", now)
            if when <= now:
                await message.answer("❌ زمان باید در آینده باشد. دوباره بفرستید:")
                return
            await state.update_data(sched_mode="once", scheduled_at=when.isoformat())
    except ValueError:
        await message.answer(
            "❌ فرمت زمان نامعتبر است. مثال: <code>2026-08-28 14:30</code> یا <code>14:30</code>:"
        )
        return
    data = await state.get_data()
    await state.set_state(PostWizard.menu)
    await message.answer(_schedule_menu_text(data), reply_markup=_schedule_menu(data))


# ═══════════════════════════════════════════════════════════════════════════════
# ── Preview + confirm ──────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


@router.callback_query(PostsCB.filter(F.action == "confirm"))
async def on_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    db: aiosqlite.Connection,
    settings,
) -> None:
    """Create the post(s) and send/schedule them."""
    data = await state.get_data()
    selected: list[int] = data.get("selected") or []
    if not selected:
        await callback.answer("کانالی انتخاب نشده است.", show_alert=True)
        return
    mode = data.get("sched_mode", "immediate")
    if mode == "once" and not data.get("sched_at"):
        await callback.answer("زمان ارسال را تنظیم کنید.", show_alert=True)
        return
    if mode == "recurring" and not data.get("sched_at"):
        await callback.answer("ساعت تکرار را تنظیم کنید.", show_alert=True)
        return

    uid = callback.from_user.id
    group_id = uuid.uuid4().hex[:8] if len(selected) > 1 else None
    fallback = settings.owner_tg_ids[0] if settings.owner_tg_ids else None
    now = datetime.now(UTC)
    opts = data.get("opts") or {}
    buttons_json = svc.buttons_to_json(_apply_layout(data))
    content = _content_fields(data)

    results: list[str] = []
    for ch_id in selected:
        ch = await store.get_channel(db, ch_id)
        if not ch:
            results.append(f"❌ کانال #{ch_id} یافت نشد")
            continue
        post = await store.create_channel_post(
            db,
            channel_id=ch.id,
            created_by=uid,
            status="draft",
            group_id=group_id,
            text=content["text"],
            entities_json=content["entities_json"],
            media_type=content["media_type"],
            media_file_id=content["media_file_id"],
            media_json=content["media_json"],
            buttons_json=buttons_json,
            delete_previous=bool(opts.get("delete_previous")),
            pin=bool(opts.get("pin")),
            silent=bool(opts.get("silent")),
            link_preview=opts.get("link_preview", True),
            ephemeral_hours=opts.get("ephemeral_hours"),
        )
        if mode == "immediate":
            try:
                result = await svc.send_post(
                    callback.bot, db, post, ch, now=now, fallback_notify_chat_id=fallback
                )
            except TelegramAPIError as exc:
                await store.update_channel_post(db, post.id, status="failed", error=str(exc)[:500])
                results.append(f"❌ {_ch_title(ch)}: {html_escape(str(exc)[:120])}")
                continue
            await store.update_channel_post(
                db,
                post.id,
                tg_message_id=result.message_id,
                tg_message_ids_json=(
                    json.dumps(result.message_ids) if result.message_ids else None
                ),
                sent_at=now.isoformat(),
                last_sent_at=now.isoformat(),
                expires_at=result.expires_at,
                status="sent",
            )
            results.append(f"✅ {_ch_title(ch)} — ارسال شد")
        else:
            await store.update_channel_post(
                db,
                post.id,
                status="recurring" if mode == "recurring" else "scheduled",
                scheduled_at=data.get("sched_at"),
                recurrence=data.get("recurrence", "none"),
                recur_at=data.get("recur_at"),
            )
            label = "زمان‌بندی شد" if mode == "once" else "تکرارشونده تنظیم شد"
            results.append(f"✅ {_ch_title(ch)} — {label}")

    if data.get("save_template"):
        await store.create_post_template(
            db,
            created_by=uid,
            name=datetime.now().strftime("%Y-%m-%d %H:%M"),
            text=content["text"],
            entities_json=content["entities_json"],
            media_type=content["media_type"],
            media_file_id=content["media_file_id"],
            media_json=content["media_json"],
            buttons_json=buttons_json,
            delete_previous=bool(opts.get("delete_previous")),
            pin=bool(opts.get("pin")),
            silent=bool(opts.get("silent")),
            link_preview=opts.get("link_preview", True),
            ephemeral_hours=opts.get("ephemeral_hours"),
        )
        results.append("💾 قالب ذخیره شد")

    await state.clear()
    summary = "\n".join(results) if results else "—"
    await _show(callback.message, f"📤 <b>نتیجه:</b>\n{summary}")
    await callback.answer()


@router.callback_query(PostsCB.filter(F.action == "cancel"))
async def on_wizard_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _show(callback.message, "🚫 لغو شد.")
    await callback.answer()


# ═══════════════════════════════════════════════════════════════════════════════
# ── /posts management views ────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


def _post_menu(post: ChannelPost) -> InlineKeyboardMarkup:
    rows = []
    if post.status in ("scheduled", "recurring", "sent", "failed"):
        rows.append([_btn("📤 ارسال الان", "pact", post.id, "send")])
    if post.status in ("scheduled", "recurring"):
        rows.append(
            [
                _btn("⏰ زمان‌بندی مجدد", "pact", post.id, "resched"),
                _btn("🚫 لغو", "pact", post.id, "cancel"),
            ]
        )
    if post.tg_message_id:
        rows.append([_btn("✏️ ویرایش متن منتشرشده", "pact", post.id, "edit")])
    if post.status in ("scheduled", "recurring", "sent", "failed"):
        rows.append([_btn("🔄 تعویض رسانه", "pact", post.id, "swapmedia")])
    rows.append(
        [
            _btn("📋 کپی به‌عنوان جدید", "pact", post.id, "copy"),
            _btn("🗑 حذف", "pact", post.id, "del"),
        ]
    )
    rows.append([_btn("↩️ بازگشت به لیست", "plist", post.channel_id)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _post_detail_text(post: ChannelPost, ch: Channel | None) -> str:
    lines = [svc.post_summary_line(post)]
    if ch:
        lines.append(f"📺 {_ch_title(ch)}")
    if svc.is_album(post):
        lines.append(f"🖼 آلبوم ({len(svc.media_items_from_json(post.media_json))} آیتم)")
    if post.scheduled_at and post.status in ("scheduled", "recurring"):
        when = svc.format_tehran(svc.parse_dt(post.scheduled_at))
        lines.append(f"⏰ اجرای بعدی: {when} (تهران)")
    if post.expires_at:
        lines.append(f"💨 حذف خودکار: {svc.format_tehran(svc.parse_dt(post.expires_at))} (تهران)")
    if post.error:
        lines.append(f"❌ خطا: {html_escape(post.error[:200])}")
    opts = []
    if post.delete_previous:
        opts.append("حذف قبلی")
    if post.pin:
        opts.append("پین")
    if post.silent:
        opts.append("بی‌صدا")
    if not post.link_preview:
        opts.append("بدون پیش‌نمایش لینک")
    if post.ephemeral_hours:
        opts.append(f"موقت {post.ephemeral_hours:g}h")
    if opts:
        lines.append("⚙️ " + "، ".join(opts))
    lines.append(f"💬 {html_escape((post.text or 'بدون متن')[:300])}")
    return "\n".join(lines)


async def render_posts_view(
    db: aiosqlite.Connection, ch: Channel
) -> tuple[str, InlineKeyboardMarkup]:
    """Posts list of one channel — shared by /posts and the /panel view."""
    posts = await store.list_channel_posts(db, ch.id, limit=20)
    header = f"📭 <b>پست‌ها — {_ch_title(ch)}</b>"
    if not posts:
        text = f"{header}\n\nپستی نیست."
    else:
        text = header + "\n\n" + "\n".join(svc.post_summary_line(p) for p in posts)
    rows = [[_btn("➕ پست جدید", "newpost", ch.id)]]
    for p in posts[:10]:
        rows.append([_btn(svc.post_summary_line(p).replace(" — ", " "), "pview", p.id)])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(PostsCB.filter(F.action == "plist"))
async def on_posts_list(
    callback: CallbackQuery, callback_data: PostsCB, db: aiosqlite.Connection, settings
) -> None:
    if not await _can_manage_channel(db, callback.from_user.id, settings, callback_data.tid):
        await callback.answer("به این کانال دسترسی ندارید.", show_alert=True)
        return
    ch = await store.get_channel(db, callback_data.tid)
    if not ch:
        await callback.answer("کانال یافت نشد.", show_alert=True)
        return
    text, kb = await render_posts_view(db, ch)
    await _show(callback.message, text, kb)
    await callback.answer()


@router.callback_query(PostsCB.filter(F.action == "pview"))
async def on_post_view(
    callback: CallbackQuery, callback_data: PostsCB, db: aiosqlite.Connection, settings
) -> None:
    post = await store.get_channel_post(db, callback_data.tid)
    if not post:
        await callback.answer("پست یافت نشد.", show_alert=True)
        return
    if not await _can_manage_channel(db, callback.from_user.id, settings, post.channel_id):
        await callback.answer("به این کانال دسترسی ندارید.", show_alert=True)
        return
    ch = await store.get_channel(db, post.channel_id)
    await _show(callback.message, _post_detail_text(post, ch), _post_menu(post))
    await callback.answer()


@router.callback_query(PostsCB.filter(F.action == "pact"))
async def on_post_action(
    callback: CallbackQuery,
    callback_data: PostsCB,
    state: FSMContext,
    db: aiosqlite.Connection,
    settings,
) -> None:
    action = callback_data.extra
    post = await store.get_channel_post(db, callback_data.tid)
    if not post:
        await callback.answer("پست یافت نشد.", show_alert=True)
        return
    if not await _can_manage_channel(db, callback.from_user.id, settings, post.channel_id):
        await callback.answer("به این کانال دسترسی ندارید.", show_alert=True)
        return
    ch = await store.get_channel(db, post.channel_id)
    if not ch:
        await callback.answer("کانال یافت نشد.", show_alert=True)
        return
    fallback = settings.owner_tg_ids[0] if settings.owner_tg_ids else None

    if action == "send":
        await svc.send_and_record(callback.bot, db, post, ch, datetime.now(UTC), fallback)
        post = await store.get_channel_post(db, post.id)
        await _show(callback.message, _post_detail_text(post, ch), _post_menu(post))
        await callback.answer("ارسال شد.")

    elif action == "cancel":
        if post.status not in ("scheduled", "recurring"):
            await callback.answer("فقط پست‌های زمان‌بندی‌شده قابل لغو هستند.", show_alert=True)
            return
        await store.update_channel_post(db, post.id, status="cancelled", scheduled_at=None)
        post = await store.get_channel_post(db, post.id)
        await _show(callback.message, _post_detail_text(post, ch), _post_menu(post))
        await callback.answer("لغو شد.")

    elif action == "resched":
        await state.set_state(PostWizard.reschedule)
        await state.update_data(resched_post_id=post.id)
        if post.status == "recurring":
            await _show(
                callback.message,
                f"🔁 ساعت تکرار جدید را بفرستید (تهران — فعلی: {post.recur_at or '—'}):",
            )
        else:
            await _show(
                callback.message,
                "⏰ زمان جدید را بفرستید (تهران):\n"
                "<code>2026-08-28 14:30</code> یا <code>14:30</code>:",
            )
        await callback.answer()

    elif action == "edit":
        if not post.tg_message_id:
            await callback.answer("این پست منتشر نشده است.", show_alert=True)
            return
        await state.set_state(PostWizard.edit_text)
        await state.update_data(edit_post_id=post.id)
        await _show(callback.message, "✏️ متن جدید را بفرستید (رسانه دست نمی‌خورد):")
        await callback.answer()

    elif action == "del":
        if post.tg_message_id:
            try:
                await callback.bot.delete_message(ch.tg_channel_id, post.tg_message_id)
            except TelegramAPIError as exc:
                logger.warning("Could not delete post message %s: %s", post.id, exc)
        await store.delete_channel_post(db, post.id)
        text, kb = await render_posts_view(db, ch)
        await _show(callback.message, f"🗑 حذف شد.\n\n{text}", kb)
        await callback.answer("حذف شد.")

    elif action == "swapmedia":
        await state.set_state(PostWizard.edit_media)
        await state.update_data(swap_post_id=post.id)
        await _show(
            callback.message,
            "🔄 رسانهٔ جدید را بفرستید (عکس، ویدیو یا گیف — تکی)."
            " متن/کپشن فعلی حفظ می‌شود مگر اینکه کپشن جدید بفرستید.",
        )
        await callback.answer()

    elif action == "copy":
        post_items = svc.media_items_from_json(post.media_json)
        await state.set_state(PostWizard.menu)
        await state.update_data(
            wizard=True,
            selected=[ch.id],
            text=post.text,
            entities_json=post.entities_json,
            media_type=post.media_type if not post_items else None,
            media_file_id=post.media_file_id if not post_items else None,
            media_items=[
                {"type": it["type"], "file_id": it["file_id"], "unique": it["file_id"]}
                for it in post_items
            ]
            or None,
            buttons=svc.buttons_from_json(post.buttons_json),
            layout=None,
            opts={
                "delete_previous": post.delete_previous,
                "pin": post.pin,
                "silent": post.silent,
                "link_preview": post.link_preview,
                "ephemeral_hours": post.ephemeral_hours,
            },
            sched_mode="immediate",
            recurrence="none",
            recur_at=None,
            sched_at=None,
            save_template=False,
        )
        data = await state.get_data()
        await _send_preview_and_confirm(callback.message, callback.bot, data)
        await callback.answer()
    else:
        await callback.answer()


@router.message(PostWizard.edit_media)
async def input_edit_media(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    """Swap a post's media: update fields; when published, delete + resend in place."""
    data = await state.get_data()
    post = await store.get_channel_post(db, data.get("swap_post_id") or 0)
    if not post:
        await state.clear()
        await message.answer("❌ پست یافت نشد؛ عملیات لغو شد.")
        return
    content = _extract_content(message)
    if content is None or not content["media_type"]:
        await message.answer("❌ فقط یک عکس، ویدیو یا گیف تکی بفرستید.")
        return
    ch = await store.get_channel(db, post.channel_id)
    if not ch:
        await state.clear()
        await message.answer("❌ کانال یافت نشد؛ عملیات لغو شد.")
        return

    updates: dict = {
        "media_type": content["media_type"],
        "media_file_id": content["media_file_id"],
        "media_json": None,
        "tg_message_ids_json": None,
        "error": None,
    }
    if content["text"]:
        updates["text"] = content["text"]
        updates["entities_json"] = svc.entities_to_json(content["entities"] or None)
    await store.update_channel_post(db, post.id, **updates)
    post = await store.get_channel_post(db, post.id)

    if post.tg_message_id:
        # Published: remove the old message(s) and send the new media in place.
        # The schedule of recurring posts is NOT advanced by a swap.
        await svc.delete_post_messages(message.bot, ch, post)
        try:
            result = await svc.send_post(message.bot, db, post, ch)
        except TelegramAPIError as exc:
            await store.update_channel_post(db, post.id, status="failed", error=str(exc)[:500])
            await message.answer(f"❌ ارسال رسانهٔ جدید ناموفق بود: {html_escape(str(exc)[:150])}")
            await state.clear()
            return
        now = datetime.now(UTC)
        await store.update_channel_post(
            db,
            post.id,
            tg_message_id=result.message_id,
            tg_message_ids_json=(json.dumps(result.message_ids) if result.message_ids else None),
            sent_at=now.isoformat(),
            last_sent_at=now.isoformat(),
            expires_at=result.expires_at,
        )
        await message.answer("✅ رسانه تعویض شد و پست جدید ارسال شد.")
    else:
        await message.answer("✅ رسانهٔ پست به‌روزرسانی شد (هنوز ارسال نشده).")
    await state.clear()


@router.message(PostWizard.reschedule)
async def input_reschedule(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    data = await state.get_data()
    post = await store.get_channel_post(db, data.get("resched_post_id") or 0)
    if not post:
        await state.clear()
        await message.answer("❌ پست یافت نشد؛ عملیات لغو شد.")
        return
    now = datetime.now(UTC)
    if post.status == "recurring":
        try:
            hh, mm = svc.parse_hhmm(message.text)
        except ValueError:
            await message.answer("❌ فرمت ساعت نامعتبر. مثال: <code>09:00</code>. دوباره:")
            return
        recur_at = f"{hh:02d}:{mm:02d}"
        nxt = svc.next_occurrence(replace(post, recur_at=recur_at), now)
        await store.update_channel_post(
            db, post.id, recur_at=recur_at, scheduled_at=nxt.isoformat()
        )
        await message.answer(f"✅ تکرار جدید: {recur_at} (تهران).")
    else:
        try:
            when = svc.parse_schedule_input(message.text or "", now)
        except ValueError:
            await message.answer(
                "❌ فرمت زمان نامعتبر. مثال: <code>2026-08-28 14:30</code>. دوباره:"
            )
            return
        if when <= now:
            await message.answer("❌ زمان باید در آینده باشد. دوباره بفرستید:")
            return
        await store.update_channel_post(
            db, post.id, scheduled_at=when.isoformat(), status="scheduled", error=None
        )
        await message.answer(f"✅ زمان‌بندی جدید: {svc.format_tehran(when)} (تهران).")
    await state.clear()


@router.message(PostWizard.edit_text)
async def input_edit_text(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    data = await state.get_data()
    post = await store.get_channel_post(db, data.get("edit_post_id") or 0)
    if not post:
        await state.clear()
        await message.answer("❌ پست یافت نشد؛ عملیات لغو شد.")
        return
    if message.text is None:
        await message.answer("❌ متن جدید را بفرستید (رسانه عوض نمی‌شود).")
        return
    ch = await store.get_channel(db, post.channel_id)
    await store.update_channel_post(
        db,
        post.id,
        text=message.text,
        entities_json=svc.entities_to_json(message.entities or None),
    )
    post = await store.get_channel_post(db, post.id)
    ok = await svc.edit_published_post(message.bot, post, ch)
    await message.answer(
        "✅ پست منتشرشده ویرایش شد." if ok else "❌ ویرایش ممکن نشد (پیام در کانال نیست)."
    )
    await state.clear()


# ═══════════════════════════════════════════════════════════════════════════════
# ── /checkpremium echo ─────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


@router.message(PostWizard.check_premium)
async def input_check_premium(message: Message, state: FSMContext) -> None:
    entities = message.entities or []
    custom = [e for e in entities if e.type == "custom_emoji"]
    if not custom:
        await message.answer(
            "❌ ایموجی پرمیومی در پیام شما پیدا نشد. یک ایموجی پرمیوم بفرستید یا /cancel"
        )
        return
    try:
        await message.bot.send_message(message.chat.id, message.text or "", entities=entities)
    except TelegramBadRequest as exc:
        if "emoji" in str(exc).lower():
            await message.answer(
                "❌ بات نمی‌تواند ایموجی پرمیوم ارسال کند. برای ارسال ایموجی پرمیوم، "
                "نام کاربری بات باید از Fragment خریداری شده باشد."
            )
        else:
            await message.answer(f"❌ خطای تلگرام: {html_escape(str(exc)[:200])}")
        return
    await message.answer("✅ بات توانست ایموجی پرمیوم را ارسال کند — در کانال هم کار می‌کند.")
    await state.clear()


# ═══════════════════════════════════════════════════════════════════════════════
# ── Catch-all: stray text while a wizard menu is open ──────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


@router.message(StateFilter(PostWizard))
async def wizard_stray_text(message: Message) -> None:
    await message.answer("ℹ️ در حالت ویزارد هستید. از دکمه‌ها استفاده کنید یا /cancel بزنید.")
