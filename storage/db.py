"""SQLite storage layer (aiosqlite).

Tables:
  settings      — runtime-editable owner settings (promo text, interval, …)
  promo_state   — last promo message id + next run time (restart-safe scheduler)
  offer_groups  — owner-curated panel groups offered to users (M4 keyboard)

(trial_grants / chat_members arrive with M4/M5.)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS promo_state (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    next_run_at REAL    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS offer_groups (
    id         INTEGER PRIMARY KEY,   -- panel group id
    label      TEXT    NOT NULL,      -- Persian button label (emoji OK)
    sort_order INTEGER NOT NULL,
    updated_at TEXT    NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def connect(db_path: Path | str) -> aiosqlite.Connection:
    """Open (creating if needed) the database and ensure the schema exists."""
    db_path = Path(db_path)
    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    await db.commit()
    return db


# ── settings ────────────────────────────────────────────────────────────────


async def get_setting(db: aiosqlite.Connection, key: str, default: str | None = None) -> str | None:
    row = await db.execute_fetchall("SELECT value FROM settings WHERE key = ?", (key,))
    return row[0]["value"] if row else default


async def set_setting(db: aiosqlite.Connection, key: str, value: str) -> None:
    await db.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, str(value), _now()),
    )
    await db.commit()


# ── promo state ─────────────────────────────────────────────────────────────


@dataclass(slots=True)
class PromoState:
    channel_id: int
    message_id: int
    next_run_at: float  # unix timestamp


async def get_promo_state(db: aiosqlite.Connection) -> PromoState | None:
    rows = await db.execute_fetchall(
        "SELECT channel_id, message_id, next_run_at FROM promo_state WHERE id = 1"
    )
    if not rows:
        return None
    r = rows[0]
    return PromoState(
        channel_id=r["channel_id"], message_id=r["message_id"], next_run_at=r["next_run_at"]
    )


async def set_promo_state(
    db: aiosqlite.Connection, channel_id: int, message_id: int, next_run_at: float
) -> None:
    await db.execute(
        "INSERT INTO promo_state (id, channel_id, message_id, next_run_at, updated_at) "
        "VALUES (1, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET channel_id = excluded.channel_id, "
        "message_id = excluded.message_id, next_run_at = excluded.next_run_at, "
        "updated_at = excluded.updated_at",
        (channel_id, message_id, next_run_at, _now()),
    )
    await db.commit()


# ── offer groups ────────────────────────────────────────────────────────────


@dataclass(slots=True)
class OfferGroup:
    id: int  # panel group id
    label: str
    sort_order: int


async def list_offer_groups(db: aiosqlite.Connection) -> list[OfferGroup]:
    rows = await db.execute_fetchall(
        "SELECT id, label, sort_order FROM offer_groups ORDER BY sort_order, id"
    )
    return [OfferGroup(id=r["id"], label=r["label"], sort_order=r["sort_order"]) for r in rows]


async def upsert_offer_group(db: aiosqlite.Connection, group_id: int, label: str) -> None:
    """Add or update an offered group; new entries are appended at the end."""
    rows = await db.execute_fetchall("SELECT COALESCE(MAX(sort_order), -1) AS m FROM offer_groups")
    next_order = rows[0]["m"] + 1
    await db.execute(
        "INSERT INTO offer_groups (id, label, sort_order, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET label = excluded.label, updated_at = excluded.updated_at",
        (group_id, label, next_order, _now()),
    )
    await db.commit()


async def delete_offer_group(db: aiosqlite.Connection, group_id: int) -> bool:
    cursor = await db.execute("DELETE FROM offer_groups WHERE id = ?", (group_id,))
    await db.commit()
    return cursor.rowcount > 0


async def clear_offer_groups(db: aiosqlite.Connection) -> int:
    cursor = await db.execute("DELETE FROM offer_groups")
    await db.commit()
    return cursor.rowcount


async def reorder_offer_groups(db: aiosqlite.Connection, ordered_ids: list[int]) -> None:
    """Set the display order to exactly the given id sequence."""
    for order, group_id in enumerate(ordered_ids):
        await db.execute(
            "UPDATE offer_groups SET sort_order = ?, updated_at = ? WHERE id = ?",
            (order, _now(), group_id),
        )
    await db.commit()


async def seed_offer_groups_from_file(db: aiosqlite.Connection, path: Path | str) -> int:
    """Seed the offer list from a JSON file — only when the table is empty.

    File format: [{"id": 2, "label": "🇳🇱 هلند"}, ...]
    Returns how many entries were inserted.
    """
    path = Path(path)
    existing = await db.execute_fetchall("SELECT COUNT(*) AS c FROM offer_groups")
    if existing[0]["c"] > 0 or not path.exists():
        return 0
    try:
        entries = json.loads(path.read_text(encoding="utf-8") or "[]")
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read offer groups seed file %s: %s", path, exc)
        return 0
    count = 0
    for order, entry in enumerate(entries):
        try:
            group_id, label = int(entry["id"]), str(entry["label"])
        except (KeyError, TypeError, ValueError):
            logger.warning("Skipping malformed offer group entry: %r", entry)
            continue
        await db.execute(
            "INSERT OR IGNORE INTO offer_groups (id, label, sort_order, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (group_id, label, order, _now()),
        )
        count += 1
    await db.commit()
    return count
