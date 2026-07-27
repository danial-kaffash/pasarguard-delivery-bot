"""Master pause switch - stops promo posting and trial delivery until resumed."""

from __future__ import annotations

import aiosqlite

from storage import db as store

PAUSED_KEY = "paused"

_TRUE_VALUES = {"1", "true", "yes", "on"}


async def is_paused(db: aiosqlite.Connection) -> bool:
    raw = await store.get_setting(db, PAUSED_KEY, "false")
    return (raw or "").strip().lower() in _TRUE_VALUES


async def set_paused(db: aiosqlite.Connection, value: bool) -> None:
    await store.set_setting(db, PAUSED_KEY, "true" if value else "false")
