"""Inline management panel — button-based admin UI.

Opened via /panel.  Navigable menu trees with FSM text inputs.
The bot edits the same message as the user navigates (no message spam).

Superadmin sees: Panels + Channels
Admin sees: their assigned channels
"""

from __future__ import annotations

import logging
from html import escape as html_escape

import aiosqlite
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from panel.manager import PanelManager
from services import trial as trial_service
from storage import db as store

from .. import texts
from ..handlers.join_request import get_join_delay
from ..pause import (
    is_channel_joins_paused,
    is_channel_paused,
    set_channel_joins_paused,
    set_channel_paused,
)

logger = logging.getLogger(__name__)

router = Router(name="panel")


# ═══════════════════════════════════════════════════════════════════════════════
# ── Callback data ─────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


class PanelCB(CallbackData, prefix="pnl"):
    """Type-safe callback data for the management panel."""

    action: str   # view, toggle, edit, confirm, back
    target: str   # main, ch, promo, trial, join, offer, stats, panels, pnl_detail, pnl_*
    tid: int = 0
    extra: str = ""


# ═══════════════════════════════════════════════════════════════════════════════
# ── FSM states ────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


class PanelInput(StatesGroup):
    """States for text input flows within the management panel."""
    # Channel settings
    waiting_promo_text = State()
    waiting_promo_int = State()
    waiting_data_limit = State()
    waiting_trial_days = State()
    waiting_grace_days = State()
    waiting_regrant_days = State()
    waiting_max_age = State()
    waiting_join_delay = State()
    waiting_reset_user = State()
    waiting_offer_panel_id = State()
    waiting_offer_group_id = State()
    waiting_offer_label = State()
    # Panel settings
    waiting_panel_name = State()
    waiting_panel_url = State()
    waiting_panel_user = State()
    waiting_panel_pass = State()
    waiting_panel_timeout = State()
    waiting_panel_protocols = State()
    waiting_panel_autodel = State()


# ═══════════════════════════════════════════════════════════════════════════════
# ── Helpers ───────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


def _btn(text: str, action: str, target: str, tid: int = 0, extra: str = "") -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        callback_data=PanelCB(action=action, target=target, tid=tid, extra=extra).pack(),
    )


def _ch_label(ch: store.Channel) -> str:
    """Human-readable channel label: title + numeric ID."""
    title = ch.title or ""
    tid = str(ch.tg_channel_id)
    return f"{title} ({tid})" if title else tid


def _ch_header(ch: store.Channel) -> str:
    return f"📺 <b>{html_escape(_ch_label(ch))}</b>"


def _panel_label(p: store.Panel) -> str:
    status = "✅" if p.active else "🗑"
    return f"{status} {p.name} ({p.base_url})"


def _panel_detail_text(p: store.Panel) -> str:
    status = "✅ فعال" if p.active else "🗑 غیرفعال"
    return (
        f"🖥 <b>{html_escape(p.name)}</b>\n\n"
        f"🔗 {p.base_url}\n"
        f"👤 {p.admin_username}\n"
        f"🔒 SSL: {'✅' if p.verify_ssl else '❌'}\n"
        f"⏱ Timeout: {p.timeout_seconds:g}s\n"
        f"🌐 Protocols: {p.protocols}\n"
        f"🗑 Auto-delete: {p.auto_delete_days}d\n"
        f"📊 وضعیت: {status}"
    )


def _back_btn(target: str, tid: int = 0) -> InlineKeyboardButton:
    return _btn("↩️ بازگشت", "back", target, tid)


def _cancel_btn() -> InlineKeyboardButton:
    return _btn("❌ لغو", "back", "main")


def _back_to_ch_kb(ch_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_back_btn("ch", ch_id)]])


def _back_to_panel_kb(panel_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_back_btn("pnl_detail", panel_id)]])


async def _is_super(db: aiosqlite.Connection, uid: int, settings) -> bool:
    if uid in settings.owner_tg_ids:
        return True
    user = await store.get_user(db, uid)
    return user is not None and user.role == "superadmin"


# ═══════════════════════════════════════════════════════════════════════════════
# ── Keyboard builders ─────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


def build_panel_list_keyboard(panels: list[store.Panel]) -> InlineKeyboardMarkup:
    buttons = [[_btn(_panel_label(p), "view", "pnl_detail", p.id)] for p in panels]
    buttons.append([_back_btn("main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_panel_detail_menu(p: store.Panel) -> InlineKeyboardMarkup:
    ssl_text = "🔒 SSL: ✅" if p.verify_ssl else "🔒 SSL: ❌"
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn(f"📝 {p.name}", "edit", "pnl_name", p.id)],
        [_btn(f"🔗 {p.base_url}", "edit", "pnl_url", p.id)],
        [_btn(f"👤 {p.admin_username}", "edit", "pnl_user", p.id)],
        [_btn("🔑 تغییر رمز", "edit", "pnl_pass", p.id)],
        [_btn(ssl_text, "toggle", "pnl_ssl", p.id)],
        [_btn(f"⏱ Timeout: {p.timeout_seconds:g}s", "edit", "pnl_timeout", p.id)],
        [_btn(f"🌐 Protocols: {p.protocols}", "edit", "pnl_protocols", p.id)],
        [_btn(f"🗑 Auto-delete: {p.auto_delete_days}d", "edit", "pnl_autodel", p.id)],
        [_btn("🗑 غیرفعال‌سازی", "confirm", "pnl_remove", p.id) if p.active else
         _btn("✅ فعال‌سازی", "toggle", "pnl_activate", p.id)],
        [_back_btn("panels")],
    ])


def build_channel_list_keyboard(channels: list[store.Channel]) -> InlineKeyboardMarkup:
    buttons = [[_btn(f"📺 {_ch_label(ch)}", "view", "ch", ch.id)] for ch in channels]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_channel_menu(ch: store.Channel, *, paused: bool, joins_paused: bool) -> InlineKeyboardMarkup:
    pause_text = "▶️ فعال‌سازی" if paused else "⏸ توقف"
    pin_text = "📌 Pin: ✅" if ch.promo_pin else "📌 Pin: ❌"
    silent_text = "🔇 Silent: ✅" if ch.promo_silent else "🔇 Silent: ❌"
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn(pause_text, "toggle", "pause", ch.id)],
        [_btn("📢 پرومو", "view", "promo", ch.id), _btn("🎁 تست‌ها", "view", "trial", ch.id)],
        [_btn("🔗 درخواست‌ها", "view", "join", ch.id), _btn("🌐 گروه‌ها", "view", "offer", ch.id)],
        [_btn("📊 آمار", "view", "stats", ch.id)],
    ])


def build_promo_menu(ch: store.Channel) -> InlineKeyboardMarkup:
    pin_text = "📌 Pin: ✅" if ch.promo_pin else "📌 Pin: ❌"
    silent_text = "🔇 Silent: ✅" if ch.promo_silent else "🔇 Silent: ❌"
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("📄 مشاهده متن", "view", "promo_text", ch.id)],
        [_btn("✏️ تغییر متن", "edit", "promo_text", ch.id)],
        [_btn(f"⏱ فاصله: {ch.promo_interval_hours:g}h", "edit", "promo_int", ch.id)],
        [_btn(pin_text, "toggle", "promo_pin", ch.id), _btn(silent_text, "toggle", "promo_silent", ch.id)],
        [_btn("📤 ارسال الان", "confirm", "promonow", ch.id)],
        [_back_btn("ch", ch.id)],
    ])


def build_trial_menu(ch: store.Channel) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn(f"📦 حجم: {ch.trial_data_limit_gb:g} GB", "edit", "trial_dl", ch.id)],
        [_btn(f"📅 روزها: {ch.trial_days}", "edit", "trial_days", ch.id)],
        [_btn(f"⏳ مهلت: {ch.on_hold_grace_days}d", "edit", "trial_grace", ch.id)],
        [_btn(f"🔄 Cooldown: {ch.allow_regrant_after_days}d", "edit", "trial_regrant", ch.id)],
        [_btn(f"🆕 Max age: {ch.trial_max_member_age_days:g}d", "edit", "trial_maxage", ch.id)],
        [_btn("🗑 ریست کاربر", "edit", "trial_reset", ch.id)],
        [_back_btn("ch", ch.id)],
    ])


def build_join_menu(ch: store.Channel, joins_paused: bool) -> InlineKeyboardMarkup:
    pause_text = "▶️ فعال‌سازی" if joins_paused else "⏸ توقف"
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn(pause_text, "toggle", "join_pause", ch.id)],
        [_btn(f"⏱ تأخیر: {ch.join_approval_delay_seconds}s", "edit", "join_delay", ch.id)],
        [_back_btn("ch", ch.id)],
    ])


def build_offer_menu(ch: store.Channel, offers: list[store.ChannelOfferGroup]) -> InlineKeyboardMarkup:
    rows = [[_btn(f"❌ {o.label} (p{o.panel_id}:g{o.group_id})", "confirm", "offer_del", ch.id, f"{o.panel_id}_{o.group_id}")] for o in offers]
    rows.append([_btn("➕ افزودن گروه", "edit", "offer_add", ch.id)])
    if offers:
        rows.append([_btn("🧹 حذف همه", "confirm", "offer_clear", ch.id)])
    rows.append([_back_btn("ch", ch.id)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _main_menu(db_channels: list[store.Channel], *, is_super: bool) -> InlineKeyboardMarkup:
    kb = build_channel_list_keyboard(db_channels)
    if is_super:
        kb.inline_keyboard.insert(0, [_btn("🖥 مدیریت پنل‌ها", "view", "panels")])
    return kb


# ═══════════════════════════════════════════════════════════════════════════════
# ── /panel entry point ────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


@router.message(Command("panel"))
async def cmd_panel(message: Message, db: aiosqlite.Connection, settings) -> None:
    uid = message.from_user.id
    is_sup = await _is_super(db, uid, settings)
    channels = await store.list_channels(db, active_only=True) if is_sup else await store.list_user_channels(db, uid)
    if not channels:
        await message.answer("📺 هیچ کانالی برای مدیریت ندارید.")
        return
    await message.answer("📺 <b>کانال مورد نظر را انتخاب کنید:</b>", reply_markup=_main_menu(channels, is_super=is_sup))


# ═══════════════════════════════════════════════════════════════════════════════
# ── View callbacks ────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


@router.callback_query(PanelCB.filter(F.action == "view"))
async def on_view(callback: CallbackQuery, callback_data: PanelCB, db: aiosqlite.Connection, state: FSMContext, settings) -> None:
    target, tid = callback_data.target, callback_data.tid

    if target == "ch":
        ch = await store.get_channel(db, tid)
        if not ch:
            await callback.answer("کانال یافت نشد.", show_alert=True)
            return
        paused = await is_channel_paused(db, ch.id)
        joins_paused = await is_channel_joins_paused(db, ch.id)
        status = "⏸ متوقف" if paused else "▶️ فعال"
        await callback.message.edit_text(f"{_ch_header(ch)}\nوضعیت: {status}", reply_markup=build_channel_menu(ch, paused=paused, joins_paused=joins_paused))

    elif target == "promo":
        ch = await store.get_channel(db, tid)
        if ch:
            await callback.message.edit_text(f"📢 <b>پرومو — {_ch_label(ch)}</b>", reply_markup=build_promo_menu(ch))

    elif target == "promo_text":
        ch = await store.get_channel(db, tid)
        if ch:
            from ..promo import get_channel_promo_text
            text = await get_channel_promo_text(db, ch.id)
            await callback.message.edit_text(f"📄 <b>متن فعلی پرومو:</b>\n\n{text}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_back_btn("promo", ch.id)]]), parse_mode=None)

    elif target == "trial":
        ch = await store.get_channel(db, tid)
        if ch:
            await callback.message.edit_text(f"🎁 <b>تنظیمات تست — {_ch_label(ch)}</b>", reply_markup=build_trial_menu(ch))

    elif target == "join":
        ch = await store.get_channel(db, tid)
        if ch:
            jp = await is_channel_joins_paused(db, ch.id)
            await callback.message.edit_text(f"🔗 <b>درخواست عضویت — {_ch_label(ch)}</b>", reply_markup=build_join_menu(ch, jp))

    elif target == "offer":
        ch = await store.get_channel(db, tid)
        if ch:
            offers = await store.list_channel_offer_groups(db, ch.id)
            lines = [f"{i}. {o.label} (p{o.panel_id}:g{o.group_id})" for i, o in enumerate(offers, 1)]
            text = f"🌐 <b>گروه‌ها — {_ch_label(ch)}:</b>\n" + ("\n".join(lines) if lines else "خالی")
            await callback.message.edit_text(text, reply_markup=build_offer_menu(ch, offers))

    elif target == "stats":
        ch = await store.get_channel(db, tid)
        if ch:
            from datetime import datetime, timedelta, UTC
            now = datetime.now(UTC)
            members = await store.count_chat_members(db, ch.tg_channel_id)
            grants = await store.list_grants(db)
            ch_grants = [g for g in grants if g.channel_id == ch.id]
            paused = await is_channel_paused(db, ch.id)
            await callback.message.edit_text(
                f"📊 <b>آمار — {_ch_label(ch)}</b>\n\n👥 اعضا: <b>{members}</b>\n🎁 تست‌ها: <b>{len(ch_grants)}</b>\n⏸ وضعیت: {'متوقف' if paused else 'فعال'}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_back_btn("ch", ch.id)]]),
            )

    elif target == "panels":
        panels = await store.list_panels(db, active_only=False)
        if not panels:
            await callback.message.edit_text("🖥 هیچ پنلی ثبت نشده.\nاز /addpanel استفاده کنید.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_back_btn("main")]]))
        else:
            await callback.message.edit_text("🖥 <b>پنل‌ها:</b>", reply_markup=build_panel_list_keyboard(panels))

    elif target == "pnl_detail":
        p = await store.get_panel(db, tid)
        if p:
            await callback.message.edit_text(_panel_detail_text(p), reply_markup=build_panel_detail_menu(p))

    elif target == "main":
        uid = callback.from_user.id
        is_sup = await _is_super(db, uid, settings)
        channels = await store.list_channels(db, active_only=True) if is_sup else await store.list_user_channels(db, uid)
        await callback.message.edit_text("📺 <b>کانال مورد نظر را انتخاب کنید:</b>", reply_markup=_main_menu(channels, is_super=is_sup))

    await callback.answer()


# ═══════════════════════════════════════════════════════════════════════════════
# ── Back callbacks ────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


@router.callback_query(PanelCB.filter(F.action == "back"))
async def on_back(callback: CallbackQuery, callback_data: PanelCB, db: aiosqlite.Connection, state: FSMContext, settings) -> None:
    await state.clear()
    target, tid = callback_data.target, callback_data.tid

    if target == "main":
        uid = callback.from_user.id
        is_sup = await _is_super(db, uid, settings)
        channels = await store.list_channels(db, active_only=True) if is_sup else await store.list_user_channels(db, uid)
        await callback.message.edit_text("📺 <b>کانال مورد نظر را انتخاب کنید:</b>", reply_markup=_main_menu(channels, is_super=is_sup))
    elif target == "panels":
        panels = await store.list_panels(db, active_only=False)
        await callback.message.edit_text("🖥 <b>پنل‌ها:</b>", reply_markup=build_panel_list_keyboard(panels) if panels else InlineKeyboardMarkup(inline_keyboard=[[_back_btn("main")]]))
    elif target == "pnl_detail":
        p = await store.get_panel(db, tid)
        if p:
            await callback.message.edit_text(_panel_detail_text(p), reply_markup=build_panel_detail_menu(p))
    elif target == "ch":
        ch = await store.get_channel(db, tid)
        if ch:
            paused = await is_channel_paused(db, ch.id)
            joins_paused = await is_channel_joins_paused(db, ch.id)
            status = "⏸ متوقف" if paused else "▶️ فعال"
            await callback.message.edit_text(f"{_ch_header(ch)}\nوضعیت: {status}", reply_markup=build_channel_menu(ch, paused=paused, joins_paused=joins_paused))
    elif target == "promo":
        ch = await store.get_channel(db, tid)
        if ch:
            await callback.message.edit_text(f"📢 <b>پرومو — {_ch_label(ch)}</b>", reply_markup=build_promo_menu(ch))
    elif target == "trial":
        ch = await store.get_channel(db, tid)
        if ch:
            await callback.message.edit_text(f"🎁 <b>تنظیمات تست — {_ch_label(ch)}</b>", reply_markup=build_trial_menu(ch))
    elif target == "join":
        ch = await store.get_channel(db, tid)
        if ch:
            jp = await is_channel_joins_paused(db, ch.id)
            await callback.message.edit_text(f"🔗 <b>درخواست عضویت — {_ch_label(ch)}</b>", reply_markup=build_join_menu(ch, jp))
    elif target == "offer":
        ch = await store.get_channel(db, tid)
        if ch:
            offers = await store.list_channel_offer_groups(db, ch.id)
            lines = [f"{i}. {o.label} (p{o.panel_id}:g{o.group_id})" for i, o in enumerate(offers, 1)]
            text = f"🌐 <b>گروه‌ها — {_ch_label(ch)}:</b>\n" + ("\n".join(lines) if lines else "خالی")
            await callback.message.edit_text(text, reply_markup=build_offer_menu(ch, offers))

    await callback.answer()


# ═══════════════════════════════════════════════════════════════════════════════
# ── Toggle callbacks ──────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


@router.callback_query(PanelCB.filter(F.action == "toggle"))
async def on_toggle(callback: CallbackQuery, callback_data: PanelCB, db: aiosqlite.Connection) -> None:
    target, tid = callback_data.target, callback_data.tid

    if target == "pause":
        ch = await store.get_channel(db, tid)
        if ch:
            await set_channel_paused(db, ch.id, not await is_channel_paused(db, ch.id))
            paused = await is_channel_paused(db, ch.id)
            joins_paused = await is_channel_joins_paused(db, ch.id)
            status = "⏸ متوقف" if paused else "▶️ فعال"
            await callback.message.edit_text(f"{_ch_header(ch)}\nوضعیت: {status}", reply_markup=build_channel_menu(ch, paused=paused, joins_paused=joins_paused))

    elif target == "promo_pin":
        ch = await store.get_channel(db, tid)
        if ch:
            await store.update_channel(db, ch.id, promo_pin=not ch.promo_pin)
            ch = await store.get_channel(db, ch.id)
            await callback.message.edit_text(f"📢 <b>پرومو — {_ch_label(ch)}</b>", reply_markup=build_promo_menu(ch))

    elif target == "promo_silent":
        ch = await store.get_channel(db, tid)
        if ch:
            await store.update_channel(db, ch.id, promo_silent=not ch.promo_silent)
            ch = await store.get_channel(db, ch.id)
            await callback.message.edit_text(f"📢 <b>پرومو — {_ch_label(ch)}</b>", reply_markup=build_promo_menu(ch))

    elif target == "join_pause":
        ch = await store.get_channel(db, tid)
        if ch:
            await set_channel_joins_paused(db, ch.id, not await is_channel_joins_paused(db, ch.id))
            jp = await is_channel_joins_paused(db, ch.id)
            await callback.message.edit_text(f"🔗 <b>درخواست عضویت — {_ch_label(ch)}</b>", reply_markup=build_join_menu(ch, jp))

    elif target == "pnl_ssl":
        p = await store.get_panel(db, tid)
        if p:
            await store.update_panel(db, p.id, verify_ssl=not p.verify_ssl)
            p = await store.get_panel(db, p.id)
            await callback.message.edit_text(_panel_detail_text(p), reply_markup=build_panel_detail_menu(p))

    elif target == "pnl_activate":
        p = await store.get_panel(db, tid)
        if p:
            await store.update_panel(db, p.id, active=True)
            p = await store.get_panel(db, p.id)
            await callback.message.edit_text(_panel_detail_text(p), reply_markup=build_panel_detail_menu(p))

    await callback.answer()


# ═══════════════════════════════════════════════════════════════════════════════
# ── Confirm callbacks ─────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


@router.callback_query(PanelCB.filter(F.action == "confirm"))
async def on_confirm(callback: CallbackQuery, callback_data: PanelCB, bot: Bot, db: aiosqlite.Connection, panel_manager: PanelManager) -> None:
    target, tid, extra = callback_data.target, callback_data.tid, callback_data.extra

    if target == "promonow":
        ch = await store.get_channel(db, tid)
        if ch:
            try:
                from ..promo import publish_promo
                await publish_promo(bot, db, channel_id=ch.tg_channel_id, pin=ch.promo_pin, silent=ch.promo_silent, channel_db_id=ch.id)
                await callback.answer("✅ پست ارسال شد.", show_alert=True)
            except Exception as exc:
                logger.exception("Panel promonow failed")
                await callback.answer(f"❌ خطا: {exc}", show_alert=True)

    elif target == "offer_del":
        ch = await store.get_channel(db, tid)
        if ch and extra:
            parts = extra.split("_")
            if len(parts) == 2:
                pid, gid = int(parts[0]), int(parts[1])
                await store.delete_channel_offer_group(db, channel_id=ch.id, panel_id=pid, group_id=gid)
                offers = await store.list_channel_offer_groups(db, ch.id)
                lines = [f"{i}. {o.label} (p{o.panel_id}:g{o.group_id})" for i, o in enumerate(offers, 1)]
                text = f"🌐 <b>گروه‌ها — {_ch_label(ch)}:</b>\n" + ("\n".join(lines) if lines else "خالی")
                await callback.message.edit_text(text, reply_markup=build_offer_menu(ch, offers))
                await callback.answer("✅ حذف شد.")
                return
        await callback.answer()

    elif target == "offer_clear":
        ch = await store.get_channel(db, tid)
        if ch:
            await store.clear_channel_offer_groups(db, ch.id)
            await callback.message.edit_text(f"🌐 <b>گروه‌ها — {_ch_label(ch)}:</b>\nخالی", reply_markup=build_offer_menu(ch, []))
            await callback.answer("✅ همه حذف شدند.")

    elif target == "pnl_remove":
        p = await store.get_panel(db, tid)
        if p:
            await store.soft_delete_panel(db, p.id)
            p = await store.get_panel(db, p.id)
            await callback.message.edit_text(_panel_detail_text(p), reply_markup=build_panel_detail_menu(p))
            await callback.answer("✅ غیرفعال شد.")
            return

    else:
        await callback.answer()


# ═══════════════════════════════════════════════════════════════════════════════
# ── Edit callbacks (enter FSM) ────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


@router.callback_query(PanelCB.filter(F.action == "edit"))
async def on_edit(callback: CallbackQuery, callback_data: PanelCB, state: FSMContext) -> None:
    target, tid = callback_data.target, callback_data.tid

    prompts = {
        "promo_text": ("📝 متن جدید پرومو:", PanelInput.waiting_promo_text),
        "promo_int": ("⏱ فاصلهٔ ارسال (ساعت):", PanelInput.waiting_promo_int),
        "trial_dl": ("📦 حجم تست (گیگابایت):", PanelInput.waiting_data_limit),
        "trial_days": ("📅 تعداد روزها:", PanelInput.waiting_trial_days),
        "trial_grace": ("⏳ مهلت اتصال (روز):", PanelInput.waiting_grace_days),
        "trial_regrant": ("🔄 Cooldown (روز):", PanelInput.waiting_regrant_days),
        "trial_max_age": ("🆕 حداکثر سن عضویت (روز، ۰=غیرفعال):", PanelInput.waiting_max_age),
        "join_delay": ("⏱ تأخیر تأیید (ثانیه):", PanelInput.waiting_join_delay),
        "trial_reset": ("🗑 آیدی عددی کاربر:", PanelInput.waiting_reset_user),
        "offer_add": ("➕ آیدی پنل:", PanelInput.waiting_offer_panel_id),
        "pnl_name": ("📝 نام جدید پنل:", PanelInput.waiting_panel_name),
        "pnl_url": ("🔗 آدرس جدید پنل:", PanelInput.waiting_panel_url),
        "pnl_user": ("👤 نام کاربری جدید:", PanelInput.waiting_panel_user),
        "pnl_pass": ("🔑 رمز جدید:", PanelInput.waiting_panel_pass),
        "pnl_timeout": ("⏱ Timeout (ثانیه):", PanelInput.waiting_panel_timeout),
        "pnl_protocols": ("🌐 پروتکل‌ها (جدا شده با کاما):", PanelInput.waiting_panel_protocols),
        "pnl_autodel": ("🗑 Auto-delete (روز):", PanelInput.waiting_panel_autodel),
    }

    if target in prompts:
        prompt, fsm_state = prompts[target]
        await state.set_state(fsm_state)
        if target.startswith("pnl_"):
            await state.update_data(panel_id=tid, target=target)
        else:
            await state.update_data(channel_id=tid, target=target)
        await callback.message.edit_text(prompt, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_cancel_btn()]]))
    await callback.answer()


# ═══════════════════════════════════════════════════════════════════════════════
# ── Channel FSM inputs ────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


@router.message(PanelInput.waiting_promo_text)
async def input_promo_text(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    data = await state.get_data(); ch_id = data.get("channel_id")
    await store.set_setting(db, f"channel:{ch_id}:promo_text", message.text or "")
    await state.clear()
    await message.answer("✅ متن پرومو ذخیره شد.", reply_markup=_back_to_ch_kb(ch_id))


@router.message(PanelInput.waiting_promo_int)
async def input_promo_int(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    data = await state.get_data(); ch_id = data.get("channel_id")
    try:
        hours = float(message.text)
        if hours <= 0: raise ValueError
    except (ValueError, TypeError):
        await message.answer("❌ لطفاً یک عدد مثبت وارد کنید.")
        return
    await store.update_channel(db, ch_id, promo_interval_hours=hours)
    await state.clear()
    await message.answer(f"✅ فاصلهٔ ارسال: {hours:g} ساعت.", reply_markup=_back_to_ch_kb(ch_id))


@router.message(PanelInput.waiting_data_limit)
async def input_data_limit(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    data = await state.get_data(); ch_id = data.get("channel_id")
    try:
        gb = float(message.text)
        if gb <= 0: raise ValueError
    except (ValueError, TypeError):
        await message.answer("❌ لطفاً یک عدد مثبت وارد کنید.")
        return
    await store.update_channel(db, ch_id, trial_data_limit_gb=gb)
    await state.clear()
    await message.answer(f"✅ حجم تست: {gb:g} GB.", reply_markup=_back_to_ch_kb(ch_id))


@router.message(PanelInput.waiting_trial_days)
async def input_trial_days(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    data = await state.get_data(); ch_id = data.get("channel_id")
    try:
        days = int(message.text)
        if days <= 0: raise ValueError
    except (ValueError, TypeError):
        await message.answer("❌ لطفاً یک عدد صحیح مثبت وارد کنید.")
        return
    await store.update_channel(db, ch_id, trial_days=days)
    await state.clear()
    await message.answer(f"✅ مدت تست: {days} روز.", reply_markup=_back_to_ch_kb(ch_id))


@router.message(PanelInput.waiting_grace_days)
async def input_grace_days(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    data = await state.get_data(); ch_id = data.get("channel_id")
    try:
        days = int(message.text)
        if days <= 0: raise ValueError
    except (ValueError, TypeError):
        await message.answer("❌ لطفاً یک عدد صحیح مثبت وارد کنید.")
        return
    await store.update_channel(db, ch_id, on_hold_grace_days=days)
    await state.clear()
    await message.answer(f"✅ مهلت اتصال: {days} روز.", reply_markup=_back_to_ch_kb(ch_id))


@router.message(PanelInput.waiting_regrant_days)
async def input_regrant_days(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    data = await state.get_data(); ch_id = data.get("channel_id")
    try:
        days = int(message.text)
        if days <= 0: raise ValueError
    except (ValueError, TypeError):
        await message.answer("❌ لطفاً یک عدد صحیح مثبت وارد کنید.")
        return
    await store.update_channel(db, ch_id, allow_regrant_after_days=days)
    await state.clear()
    await message.answer(f"✅ Cooldown: {days} روز.", reply_markup=_back_to_ch_kb(ch_id))


@router.message(PanelInput.waiting_max_age)
async def input_max_age(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    data = await state.get_data(); ch_id = data.get("channel_id")
    try:
        days = float(message.text)
        if days < 0: raise ValueError
    except (ValueError, TypeError):
        await message.answer("❌ لطفاً یک عدد معتبر وارد کنید (۰ = غیرفعال).")
        return
    await store.update_channel(db, ch_id, trial_max_member_age_days=days)
    await state.clear()
    text = "✅ محدودیت سن عضویت حذف شد." if days == 0 else f"✅ حداکثر سن عضویت: {days:g} روز."
    await message.answer(text, reply_markup=_back_to_ch_kb(ch_id))


@router.message(PanelInput.waiting_join_delay)
async def input_join_delay(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    data = await state.get_data(); ch_id = data.get("channel_id")
    try:
        seconds = int(message.text)
        if seconds < 0: raise ValueError
    except (ValueError, TypeError):
        await message.answer("❌ لطفاً یک عدد صحیح معتبر وارد کنید.")
        return
    await store.update_channel(db, ch_id, join_approval_delay_seconds=seconds)
    await state.clear()
    await message.answer(f"✅ تأخیر تأیید: {seconds} ثانیه.", reply_markup=_back_to_ch_kb(ch_id))


@router.message(PanelInput.waiting_reset_user)
async def input_reset_user(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    data = await state.get_data(); ch_id = data.get("channel_id")
    try:
        uid = int((message.text or "").strip().lstrip("@"))
    except (ValueError, TypeError):
        await message.answer("❌ آیدی عددی کاربر را وارد کنید.")
        return
    if await store.revoke_grant(db, uid):
        await message.answer(f"✅ تست کاربر {uid} ریست شد.", reply_markup=_back_to_ch_kb(ch_id))
    else:
        await message.answer(f"تستی برای کاربر {uid} یافت نشد.", reply_markup=_back_to_ch_kb(ch_id))
    await state.clear()


@router.message(PanelInput.waiting_offer_panel_id)
async def input_offer_panel_id(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    try:
        pid = int((message.text or "").strip())
    except (ValueError, TypeError):
        await message.answer("❌ آیدی عددی پنل را وارد کنید.")
        return
    if not await store.get_panel(db, pid):
        await message.answer(f"❌ پنل #{pid} یافت نشد.")
        return
    await state.update_data(offer_panel_id=pid)
    await state.set_state(PanelInput.waiting_offer_group_id)
    await message.answer("آیدی گروه در پنل:")


@router.message(PanelInput.waiting_offer_group_id)
async def input_offer_group_id(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    try:
        gid = int((message.text or "").strip())
    except (ValueError, TypeError):
        await message.answer("❌ آیدی عددی گروه را وارد کنید.")
        return
    await state.update_data(offer_group_id=gid)
    await state.set_state(PanelInput.waiting_offer_label)
    await message.answer("برچسب نمایشی (مثال: 🇳🇱 هلند):")


@router.message(PanelInput.waiting_offer_label)
async def input_offer_label(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    data = await state.get_data()
    ch_id, pid, gid = data.get("channel_id"), data.get("offer_panel_id"), data.get("offer_group_id")
    label = (message.text or "").strip()
    if not label:
        await message.answer("❌ برچسب نمی‌تواند خالی باشد.")
        return
    await store.upsert_channel_offer_group(db, channel_id=ch_id, panel_id=pid, group_id=gid, label=label)
    await state.clear()
    await message.answer(f"✅ «{label}» اضافه شد.", reply_markup=_back_to_ch_kb(ch_id))


# ═══════════════════════════════════════════════════════════════════════════════
# ── Panel FSM inputs ──────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


@router.message(PanelInput.waiting_panel_name)
async def input_panel_name(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    data = await state.get_data(); pid = data.get("panel_id")
    name = (message.text or "").strip()
    if not name:
        await message.answer("❌ نام نمی‌تواند خالی باشد.")
        return
    await store.update_panel(db, pid, name=name)
    await state.clear()
    await message.answer(f"✅ نام پنل به «{name}» تغییر کرد.", reply_markup=_back_to_panel_kb(pid))


@router.message(PanelInput.waiting_panel_url)
async def input_panel_url(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    data = await state.get_data(); pid = data.get("panel_id")
    url = (message.text or "").strip()
    if not url:
        await message.answer("❌ آدرس نمی‌تواند خالی باشد.")
        return
    await store.update_panel(db, pid, base_url=url)
    await state.clear()
    await message.answer("✅ آدرس پنل به‌روزرسانی شد.", reply_markup=_back_to_panel_kb(pid))


@router.message(PanelInput.waiting_panel_user)
async def input_panel_user(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    data = await state.get_data(); pid = data.get("panel_id")
    username = (message.text or "").strip()
    if not username:
        await message.answer("❌ نام کاربری نمی‌تواند خالی باشد.")
        return
    await store.update_panel(db, pid, admin_username=username)
    await state.clear()
    await message.answer("✅ نام کاربری به‌روزرسانی شد.", reply_markup=_back_to_panel_kb(pid))


@router.message(PanelInput.waiting_panel_pass)
async def input_panel_pass(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    data = await state.get_data(); pid = data.get("panel_id")
    password = (message.text or "").strip()
    if not password:
        await message.answer("❌ رمز نمی‌تواند خالی باشد.")
        return
    await store.update_panel(db, pid, admin_password=password)
    await state.clear()
    await message.answer("✅ رمز پنل به‌روزرسانی شد.", reply_markup=_back_to_panel_kb(pid))


@router.message(PanelInput.waiting_panel_timeout)
async def input_panel_timeout(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    data = await state.get_data(); pid = data.get("panel_id")
    try:
        timeout = float(message.text)
        if timeout <= 0: raise ValueError
    except (ValueError, TypeError):
        await message.answer("❌ لطفاً یک عدد مثبت وارد کنید.")
        return
    await store.update_panel(db, pid, timeout_seconds=timeout)
    await state.clear()
    await message.answer(f"✅ Timeout: {timeout:g}s.", reply_markup=_back_to_panel_kb(pid))


@router.message(PanelInput.waiting_panel_protocols)
async def input_panel_protocols(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    data = await state.get_data(); pid = data.get("panel_id")
    protocols = (message.text or "").strip()
    if not protocols:
        await message.answer("❌ پروتکل‌ها نمی‌تواند خالی باشد.")
        return
    await store.update_panel(db, pid, protocols=protocols)
    await state.clear()
    await message.answer(f"✅ پروتکل‌ها: {protocols}", reply_markup=_back_to_panel_kb(pid))


@router.message(PanelInput.waiting_panel_autodel)
async def input_panel_autodel(message: Message, state: FSMContext, db: aiosqlite.Connection) -> None:
    data = await state.get_data(); pid = data.get("panel_id")
    try:
        days = int(message.text)
        if days <= 0: raise ValueError
    except (ValueError, TypeError):
        await message.answer("❌ لطفاً یک عدد صحیح مثبت وارد کنید.")
        return
    await store.update_panel(db, pid, auto_delete_days=days)
    await state.clear()
    await message.answer(f"✅ Auto-delete: {days} روز.", reply_markup=_back_to_panel_kb(pid))
