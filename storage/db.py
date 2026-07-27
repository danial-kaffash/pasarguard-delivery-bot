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

CREATE TABLE IF NOT EXISTS trial_grants (
    tg_user_id     INTEGER PRIMARY KEY,
    tg_username    TEXT,
    panel_username TEXT NOT NULL,
    panel_user_id  INTEGER,
    group_ids      TEXT,              -- JSON list of panel group ids
    data_limit     INTEGER,
    expire_at      TEXT,
    created_at     TEXT NOT NULL,
    source_chat_id INTEGER,
    revoked        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chat_members (
    chat_id    INTEGER NOT NULL,
    tg_user_id INTEGER NOT NULL,
    status     TEXT    NOT NULL,      -- latest known membership status
    updated_at TEXT    NOT NULL,
    PRIMARY KEY (chat_id, tg_user_id)
);

CREATE TABLE IF NOT EXISTS member_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    tg_user_id INTEGER NOT NULL,
    kind       TEXT    NOT NULL,      -- 'join' | 'leave'
    at         TEXT    NOT NULL
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


# ── trial grants ────────────────────────────────────────────────────────────


@dataclass(slots=True)
class TrialGrant:
    tg_user_id: int
    panel_username: str
    created_at: datetime
    tg_username: str | None = None
    panel_user_id: int | None = None
    group_ids: list[int] | None = None
    data_limit: int | None = None
    expire_at: datetime | None = None
    source_chat_id: int | None = None
    revoked: bool = False


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


async def get_latest_grant(db: aiosqlite.Connection, tg_user_id: int) -> TrialGrant | None:
    """The single grant row for a user (one trial per Telegram user)."""
    rows = await db.execute_fetchall(
        "SELECT * FROM trial_grants WHERE tg_user_id = ?", (tg_user_id,)
    )
    if not rows:
        return None
    r = rows[0]
    return TrialGrant(
        tg_user_id=r["tg_user_id"],
        tg_username=r["tg_username"],
        panel_username=r["panel_username"],
        panel_user_id=r["panel_user_id"],
        group_ids=json.loads(r["group_ids"]) if r["group_ids"] else None,
        data_limit=r["data_limit"],
        expire_at=_parse_dt(r["expire_at"]),
        created_at=datetime.fromisoformat(r["created_at"]),
        source_chat_id=r["source_chat_id"],
        revoked=bool(r["revoked"]),
    )


async def record_grant(
    db: aiosqlite.Connection,
    *,
    tg_user_id: int,
    panel_username: str,
    tg_username: str | None = None,
    panel_user_id: int | None = None,
    group_ids: list[int] | None = None,
    data_limit: int | None = None,
    expire_at: datetime | None = None,
    source_chat_id: int | None = None,
) -> None:
    """Insert or replace the grant row for this Telegram user."""
    await db.execute(
        "INSERT INTO trial_grants (tg_user_id, tg_username, panel_username, panel_user_id, "
        "group_ids, data_limit, expire_at, created_at, source_chat_id, revoked) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0) "
        "ON CONFLICT(tg_user_id) DO UPDATE SET tg_username = excluded.tg_username, "
        "panel_username = excluded.panel_username, panel_user_id = excluded.panel_user_id, "
        "group_ids = excluded.group_ids, data_limit = excluded.data_limit, "
        "expire_at = excluded.expire_at, created_at = excluded.created_at, "
        "source_chat_id = excluded.source_chat_id, revoked = 0",
        (
            tg_user_id,
            tg_username,
            panel_username,
            panel_user_id,
            json.dumps(group_ids or []),
            data_limit,
            expire_at.isoformat() if expire_at else None,
            _now(),
            source_chat_id,
        ),
    )
    await db.commit()


async def revoke_grant(db: aiosqlite.Connection, tg_user_id: int) -> bool:
    cursor = await db.execute(
        "UPDATE trial_grants SET revoked = 1 WHERE tg_user_id = ?", (tg_user_id,)
    )
    await db.commit()
    return cursor.rowcount > 0


async def list_grants(db: aiosqlite.Connection) -> list[TrialGrant]:
    """All grant rows (for /stats)."""
    rows = await db.execute_fetchall("SELECT * FROM trial_grants ORDER BY created_at DESC")
    return [
        TrialGrant(
            tg_user_id=r["tg_user_id"],
            tg_username=r["tg_username"],
            panel_username=r["panel_username"],
            panel_user_id=r["panel_user_id"],
            group_ids=json.loads(r["group_ids"]) if r["group_ids"] else None,
            data_limit=r["data_limit"],
            expire_at=_parse_dt(r["expire_at"]),
            created_at=datetime.fromisoformat(r["created_at"]),
            source_chat_id=r["source_chat_id"],
            revoked=bool(r["revoked"]),
        )
        for r in rows
    ]


# ── chat members & join/leave events ────────────────────────────────────────


async def upsert_chat_member(
    db: aiosqlite.Connection, chat_id: int, tg_user_id: int, status: str
) -> None:
    await db.execute(
        "INSERT INTO chat_members (chat_id, tg_user_id, status, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(chat_id, tg_user_id) DO UPDATE SET status = excluded.status, "
        "updated_at = excluded.updated_at",
        (chat_id, tg_user_id, status, _now()),
    )
    await db.commit()


async def count_chat_members(
    db: aiosqlite.Connection, chat_id: int, statuses: tuple[str, ...] = ("member", "administrator")
) -> int:
    placeholders = ",".join("?" * len(statuses))
    rows = await db.execute_fetchall(
        f"SELECT COUNT(*) AS c FROM chat_members WHERE chat_id = ? AND status IN ({placeholders})",
        (chat_id, *statuses),
    )
    return rows[0]["c"]


async def record_member_event(
    db: aiosqlite.Connection, chat_id: int, tg_user_id: int, kind: str
) -> None:
    await db.execute(
        "INSERT INTO member_events (chat_id, tg_user_id, kind, at) VALUES (?, ?, ?, ?)",
        (chat_id, tg_user_id, kind, _now()),
    )
    await db.commit()


async def count_member_events(db: aiosqlite.Connection, kind: str, since: datetime) -> int:
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) AS c FROM member_events WHERE kind = ? AND at >= ?",
        (kind, since.isoformat()),
    )
    return rows[0]["c"]
