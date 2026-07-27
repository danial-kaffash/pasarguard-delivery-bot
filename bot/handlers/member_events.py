"""Join/leave tracking for the watched channel (powers /stats, M5).

Silent bookkeeping only — never posts anything.
"""

from __future__ import annotations

import logging

import aiosqlite
from aiogram import Router
from aiogram.types import ChatMemberUpdated

from storage import db as store

logger = logging.getLogger(__name__)

router = Router(name="member_events")

JOINED = {"member", "administrator"}
LEFT = {"left", "kicked"}


@router.chat_member()
async def on_chat_member(event: ChatMemberUpdated, db: aiosqlite.Connection, settings) -> None:
    if event.chat.id != settings.channel_id:
        return  # only track our own channel

    old = event.old_chat_member.status
    new = event.new_chat_member.status
    user_id = event.new_chat_member.user.id

    await store.upsert_chat_member(db, event.chat.id, user_id, new)

    if old in LEFT and new in JOINED:
        await store.record_member_event(db, event.chat.id, user_id, "join")
        logger.debug("Join: tg_id=%s", user_id)
    elif old in JOINED and new in LEFT:
        await store.record_member_event(db, event.chat.id, user_id, "leave")
        logger.debug("Leave: tg_id=%s", user_id)
