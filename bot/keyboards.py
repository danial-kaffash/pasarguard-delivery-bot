"""Inline keyboards for the trial flow."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from storage.db import ChannelOfferGroup, OfferGroup


class GroupCB(CallbackData, prefix="grp"):
    """Callback data for group-selection buttons.

    action: "toggle" (flip gid), "confirm", or "cancel".
    """

    action: str
    gid: int = 0


def _offer_id(offer) -> int:
    """Extract the group id from either OfferGroup or ChannelOfferGroup."""
    return offer.group_id if hasattr(offer, "group_id") else offer.id


def build_selection_keyboard(
    offers: list[OfferGroup] | list[ChannelOfferGroup],
    selected: set[int],
) -> InlineKeyboardMarkup:
    """Two-column group buttons (✅ when selected) + confirm/cancel row."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for offer in offers:
        gid = _offer_id(offer)
        mark = "✅ " if gid in selected else ""
        row.append(
            InlineKeyboardButton(
                text=f"{mark}{offer.label}",
                callback_data=GroupCB(action="toggle", gid=gid).pack(),
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton(
                text="✅ تأیید انتخاب", callback_data=GroupCB(action="confirm").pack()
            ),
            InlineKeyboardButton(text="❌ انصراف", callback_data=GroupCB(action="cancel").pack()),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
