"""Pause switches — two independent toggles, now with per-channel support.

Global (legacy):
- ``paused``      (PAUSED_KEY)  : stops promo posting and /start trial delivery.
- ``joins_paused`` (JOINS_PAUSED_KEY): join requests approved immediately without trial.

Per-channel:
- ``channel:{id}:paused``       — pauses this channel's promo and trials.
- ``channel:{id}:joins_paused`` — pauses join-request trials for this channel.

Both global and per-channel flags are checked — if either is on, the feature
is paused for that channel.
"""

from __future__ import annotations

import aiosqlite

from storage import db as store

# ── global keys (legacy) ────────────────────────────────────────────────────

PAUSED_KEY = "paused"
JOINS_PAUSED_KEY = "joins_paused"

# ── per-channel key patterns ────────────────────────────────────────────────

_CHANNEL_PAUSED_FMT = "channel:{channel_id}:paused"
_CHANNEL_JOINS_PAUSED_FMT = "channel:{channel_id}:joins_paused"

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _is_true(raw: str | None) -> bool:
    return (raw or "").strip().lower() in _TRUE_VALUES


# ── global (legacy) ─────────────────────────────────────────────────────────


async def is_paused(db: aiosqlite.Connection) -> bool:
    """Check the global pause flag."""
    return _is_true(await store.get_setting(db, PAUSED_KEY, "false"))


async def set_paused(db: aiosqlite.Connection, value: bool) -> None:
    await store.set_setting(db, PAUSED_KEY, "true" if value else "false")


async def is_joins_paused(db: aiosqlite.Connection) -> bool:
    """Check the global join-request pause flag."""
    return _is_true(await store.get_setting(db, JOINS_PAUSED_KEY, "false"))


async def set_joins_paused(db: aiosqlite.Connection, value: bool) -> None:
    await store.set_setting(db, JOINS_PAUSED_KEY, "true" if value else "false")


# ── per-channel ─────────────────────────────────────────────────────────────


async def is_channel_paused(db: aiosqlite.Connection, channel_db_id: int) -> bool:
    """True if the channel is paused (or the global flag is on)."""
    if await is_paused(db):
        return True
    key = _CHANNEL_PAUSED_FMT.format(channel_id=channel_db_id)
    return _is_true(await store.get_setting(db, key))


async def set_channel_paused(db: aiosqlite.Connection, channel_db_id: int, value: bool) -> None:
    key = _CHANNEL_PAUSED_FMT.format(channel_id=channel_db_id)
    await store.set_setting(db, key, "true" if value else "false")


async def is_channel_joins_paused(db: aiosqlite.Connection, channel_db_id: int) -> bool:
    """True if join requests are paused for this channel (or the global flag is on)."""
    if await is_joins_paused(db):
        return True
    key = _CHANNEL_JOINS_PAUSED_FMT.format(channel_id=channel_db_id)
    return _is_true(await store.get_setting(db, key))


async def set_channel_joins_paused(db: aiosqlite.Connection, channel_db_id: int, value: bool) -> None:
    key = _CHANNEL_JOINS_PAUSED_FMT.format(channel_id=channel_db_id)
    await store.set_setting(db, key, "true" if value else "false")
