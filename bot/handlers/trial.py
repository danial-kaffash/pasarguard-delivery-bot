"""The 5 GB trial flow (M4):

/start → eligibility check → multi-select group keyboard → confirm →
panel create_user (on-hold) → deliver subscription URL → record grant.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from html import escape as html_escape

import aiosqlite
from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from panel.client import PasarGuardApiClient
from panel.exceptions import PanelError
from services import trial as trial_service
from storage import db as store

from .. import texts
from ..keyboards import GroupCB, build_selection_keyboard
from ..pause import is_paused

logger = logging.getLogger(__name__)

router = Router(name="trial")


class TrialForm(StatesGroup):
    selecting = State()


# ── helpers ──────────────────────────────────────────────────────────────────


def _display_name(message_or_user) -> str:
    user = getattr(message_or_user, "from_user", message_or_user)
    # Names are user-controlled input — escape before placing into HTML text.
    return html_escape(user.first_name or "دوست عزیز")


def _labels_for(ids: list[int], offers: list[store.OfferGroup]) -> str:
    labels = {o.id: o.label for o in offers}
    return "، ".join(labels.get(i, f"#{i}") for i in ids)


# ── /start — eligibility + show the group picker ────────────────────────────


@router.message(CommandStart())
async def on_start(
    message: Message,
    state: FSMContext,
    bot: Bot,
    db: aiosqlite.Connection,
    settings,
    panel: PasarGuardApiClient,
) -> None:
    if message.chat.type != "private":
        await message.answer(texts.PRIVATE_ONLY)
        return

    if await is_paused(db):
        await message.answer(texts.PAUSED)
        return

    user = message.from_user
    name = _display_name(message)

    # "New members only" gate (0 = disabled): the user's recorded channel
    # join must be within the configured window; unknown age fails the gate.
    max_age = await trial_service.get_max_member_age_days(
        db, settings.trial_max_member_age_days
    )
    if max_age > 0:
        join_at = await store.get_first_join_at(db, settings.channel_id, user.id)
        if not trial_service.is_membership_recent_enough(join_at, max_age):
            await message.answer(texts.NOT_NEW_MEMBER.format(days=f"{max_age:g}"))
            return

    grant = await store.get_latest_grant(db, user.id)
    eligibility = trial_service.check_eligibility(grant, settings)

    if not eligibility.eligible and eligibility.reason == "active":
        sub_url = None
        try:
            panel_user = await panel.get_user(grant.panel_username)
            sub_url = panel_user.subscription_url or None
        except PanelError as exc:
            logger.warning("Could not re-fetch existing trial %s: %s", grant.panel_username, exc)
        if sub_url:
            await message.answer(texts.ALREADY_GRANTED.format(name=name, sub_url=sub_url))
        else:
            await message.answer(texts.ALREADY_GRANTED_NO_URL.format(name=name))
        return

    if not eligibility.eligible and eligibility.reason == "cooldown":
        await message.answer(
            texts.COOLDOWN.format(name=name, days=eligibility.retry_after_days or 0)
        )
        return

    offers, _stale = await trial_service.get_offered_groups(panel, db)
    if not offers:
        await message.answer(texts.NO_GROUPS_AVAILABLE)
        return

    await state.set_state(TrialForm.selecting)
    await state.update_data(selected=[])
    await message.answer(
        texts.GREETING.format(name=name, gb=settings.trial_data_limit_gb),
        reply_markup=build_selection_keyboard(offers, selected=set()),
    )


# ── group selection callbacks ────────────────────────────────────────────────


@router.callback_query(GroupCB.filter(F.action == "toggle"), TrialForm.selecting)
async def toggle_group(
    callback: CallbackQuery,
    state: FSMContext,
    db: aiosqlite.Connection,
    panel: PasarGuardApiClient,
    callback_data: GroupCB,
) -> None:
    data = await state.get_data()
    selected = set(data.get("selected", []))
    selected ^= {callback_data.gid}
    await state.update_data(selected=sorted(selected))

    offers, _stale = await trial_service.get_offered_groups(panel, db)
    await callback.message.edit_reply_markup(
        reply_markup=build_selection_keyboard(offers, selected=selected)
    )
    await callback.answer()


@router.callback_query(GroupCB.filter(F.action == "cancel"), TrialForm.selecting)
async def cancel_selection(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(texts.CANCELLED)
    await callback.answer()


@router.callback_query(GroupCB.filter(F.action == "confirm"), TrialForm.selecting)
async def confirm_selection(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    db: aiosqlite.Connection,
    settings,
    panel: PasarGuardApiClient,
) -> None:
    user = callback.from_user
    data = await state.get_data()
    selected: list[int] = sorted(data.get("selected", []))

    if not selected:
        await callback.answer(texts.SELECT_HINT, show_alert=True)
        return

    if await is_paused(db):
        await state.clear()
        await callback.message.edit_text(texts.PAUSED)
        await callback.answer()
        return

    # Re-check eligibility at confirm time (guards against races/double taps).
    grant = await store.get_latest_grant(db, user.id)
    if not trial_service.check_eligibility(grant, settings).eligible:
        await state.clear()
        await callback.message.edit_text(
            texts.ALREADY_GRANTED_NO_URL.format(name=html_escape(user.first_name or ""))
        )
        await callback.answer()
        return

    await callback.answer()
    await callback.message.edit_text(texts.CREATING)

    try:
        panel_user, username = await trial_service.create_trial(
            panel, settings=settings, tg_user_id=user.id, group_ids=selected
        )
    except PanelError:
        logger.exception("Trial creation failed for tg_id=%s", user.id)
        await state.clear()
        await callback.message.answer(texts.ERROR_TRY_AGAIN)
        return

    expire_at = datetime.now(UTC) + timedelta(
        days=settings.on_hold_grace_days + settings.trial_days
    )
    await store.record_grant(
        db,
        tg_user_id=user.id,
        tg_username=user.username,
        panel_username=username,
        panel_user_id=panel_user.id,
        group_ids=selected,
        data_limit=settings.trial_data_limit_bytes,
        expire_at=expire_at,
        source_chat_id=None,
    )
    await state.clear()

    offers, _stale = await trial_service.get_offered_groups(panel, db)
    if panel_user.subscription_url:
        await callback.message.answer(
            texts.DELIVERY.format(
                gb=settings.trial_data_limit_gb,
                sub_url=panel_user.subscription_url,
                trial_days=settings.trial_days,
                grace_days=settings.on_hold_grace_days,
                group_labels=_labels_for(selected, offers),
            )
        )
    else:
        logger.warning("Panel returned no subscription_url for %s", username)
        await callback.message.answer(texts.DELIVERY_NO_SUB_URL.format(username=username))
    logger.info("Granted trial %s to tg_id=%s (groups=%s)", username, user.id, selected)
