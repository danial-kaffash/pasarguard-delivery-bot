"""SQLite storage layer (aiosqlite).

Tables (original — single-tenant):
  settings, promo_state, offer_groups, trial_grants, chat_members, member_events

Tables (multi-tenant):
  panels, channels, users, channel_admins, channel_offer_groups
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from . import crypto

logger = logging.getLogger(__name__)

# ── Original schema (single-tenant, preserved for backward compatibility) ────

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS promo_state (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
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
    revoked        INTEGER NOT NULL DEFAULT 0,
    source         TEXT NOT NULL DEFAULT 'start',  -- 'start' | 'join_request'
    channel_id     INTEGER                          -- multi-tenant: which channel this grant is for
);

CREATE TABLE IF NOT EXISTS chat_members (
    chat_id    INTEGER NOT NULL,
    tg_user_id INTEGER NOT NULL,
    status     TEXT    NOT NULL,
    updated_at TEXT    NOT NULL,
    PRIMARY KEY (chat_id, tg_user_id)
);

CREATE TABLE IF NOT EXISTS member_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    tg_user_id INTEGER NOT NULL,
    kind       TEXT    NOT NULL,      -- 'join' | 'leave' | 'join_request'
    at         TEXT    NOT NULL
);

-- ── Multi-tenant tables ────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS panels (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    base_url        TEXT NOT NULL,
    admin_username  TEXT NOT NULL,
    admin_password  TEXT NOT NULL,          -- Fernet-encrypted when DB_ENCRYPTION_KEY is set
    verify_ssl      INTEGER NOT NULL DEFAULT 1,
    timeout_seconds REAL NOT NULL DEFAULT 15.0,
    protocols       TEXT NOT NULL DEFAULT 'vless',
    auto_delete_days INTEGER NOT NULL DEFAULT 11,
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channels (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_channel_id   INTEGER NOT NULL UNIQUE,
    title           TEXT NOT NULL DEFAULT '',
    trial_data_limit_gb       REAL NOT NULL DEFAULT 5.0,
    trial_days                INTEGER NOT NULL DEFAULT 3,
    on_hold_grace_days        INTEGER NOT NULL DEFAULT 7,
    allow_regrant_after_days  INTEGER NOT NULL DEFAULT 30,
    trial_max_member_age_days REAL NOT NULL DEFAULT 0,
    join_approval_delay_seconds INTEGER NOT NULL DEFAULT 10,
    promo_interval_hours      REAL NOT NULL DEFAULT 6.0,
    promo_pin                 INTEGER NOT NULL DEFAULT 1,
    promo_silent              INTEGER NOT NULL DEFAULT 1,
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    tg_user_id  INTEGER PRIMARY KEY,
    username    TEXT,
    role        TEXT NOT NULL DEFAULT 'user',   -- 'superadmin' | 'admin' | 'user'
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_admins (
    tg_user_id  INTEGER NOT NULL REFERENCES users(tg_user_id),
    channel_id  INTEGER NOT NULL REFERENCES channels(id),
    created_at  TEXT NOT NULL,
    PRIMARY KEY (tg_user_id, channel_id)
);

CREATE TABLE IF NOT EXISTS channel_offer_groups (
    channel_id  INTEGER NOT NULL REFERENCES channels(id),
    panel_id    INTEGER NOT NULL REFERENCES panels(id),
    group_id    INTEGER NOT NULL,
    label       TEXT NOT NULL,
    sort_order  INTEGER NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (channel_id, panel_id, group_id)
);

CREATE TABLE IF NOT EXISTS channel_posts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id      INTEGER NOT NULL REFERENCES channels(id),
    group_id        TEXT,                 -- multi-channel fan-out grouping
    created_by      INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,

    -- content (text or media caption + preserved entities)
    text            TEXT NOT NULL DEFAULT '',
    entities_json   TEXT,
    media_type      TEXT,                 -- photo | video | animation | album | NULL
    media_file_id   TEXT,
    media_json      TEXT,                 -- album: [{type, file_id}, ...] (2..10 items)
    buttons_json    TEXT NOT NULL DEFAULT '[]',

    -- options
    delete_previous INTEGER NOT NULL DEFAULT 0,
    pin             INTEGER NOT NULL DEFAULT 0,
    silent          INTEGER NOT NULL DEFAULT 0,
    link_preview    INTEGER NOT NULL DEFAULT 1,
    ephemeral_hours REAL,                 -- NULL = permanent
    expires_at      TEXT,                 -- UTC, set at send time for ephemeral

    -- scheduling
    status          TEXT NOT NULL DEFAULT 'draft',
                                      -- draft|scheduled|recurring|sent|failed|cancelled|expired
    scheduled_at    TEXT,                 -- UTC; first occurrence for recurring
    recurrence      TEXT NOT NULL DEFAULT 'none',  -- none | daily | weekly
    recur_at        TEXT,                 -- 'HH:MM' Tehran local
    last_sent_at    TEXT,

    -- delivery
    sent_at         TEXT,
    tg_message_id   INTEGER,
    tg_message_ids_json TEXT,             -- album: all message ids of the group
    error           TEXT
);

CREATE TABLE IF NOT EXISTS post_templates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL DEFAULT '',
    created_by      INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    text            TEXT NOT NULL DEFAULT '',
    entities_json   TEXT,
    media_type      TEXT,
    media_file_id   TEXT,
    media_json      TEXT,
    buttons_json    TEXT NOT NULL DEFAULT '[]',
    delete_previous INTEGER NOT NULL DEFAULT 0,
    pin             INTEGER NOT NULL DEFAULT 0,
    silent          INTEGER NOT NULL DEFAULT 0,
    link_preview    INTEGER NOT NULL DEFAULT 1,
    ephemeral_hours REAL
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
    await _migrate(db)
    await db.commit()
    return db


async def _migrate(db: aiosqlite.Connection) -> None:
    """Idempotent migrations for columns added after the initial schema."""
    _migrations = [
        # (column, table, definition)
        (
            "source",
            "trial_grants",
            "ALTER TABLE trial_grants ADD COLUMN source TEXT NOT NULL DEFAULT 'start'",
        ),
        ("channel_id", "trial_grants", "ALTER TABLE trial_grants ADD COLUMN channel_id INTEGER"),
        (
            "post_delete_previous",
            "channels",
            "ALTER TABLE channels ADD COLUMN post_delete_previous INTEGER NOT NULL DEFAULT 0",
        ),
        ("media_json", "channel_posts", "ALTER TABLE channel_posts ADD COLUMN media_json TEXT"),
        (
            "tg_message_ids_json",
            "channel_posts",
            "ALTER TABLE channel_posts ADD COLUMN tg_message_ids_json TEXT",
        ),
        ("media_json", "post_templates", "ALTER TABLE post_templates ADD COLUMN media_json TEXT"),
    ]
    for _col, _table, sql in _migrations:
        try:
            await db.execute(sql)
        except Exception:
            pass  # column already exists

    # Migrate promo_state: remove CHECK (id=1) to allow multiple rows (one per channel).
    await _migrate_promo_state(db)


# ── settings ────────────────────────────────────────────────────────────────


async def _migrate_promo_state(db: aiosqlite.Connection) -> None:
    """Recreate promo_state without the CHECK (id=1) constraint if needed."""
    try:
        # Try inserting a row with id=2 — if CHECK exists, this fails.
        await db.execute(
            "INSERT INTO promo_state (id, channel_id, message_id, next_run_at, updated_at) "
            "VALUES (2, 0, 0, 0, ?)",
            (_now(),),
        )
        await db.execute("DELETE FROM promo_state WHERE id = 2 AND channel_id = 0")
        await db.commit()
        # Success — constraint is already gone (new schema).
        return
    except Exception:
        pass  # CHECK constraint exists — need migration.

    logger.info("Migrating promo_state table to remove CHECK constraint...")
    await db.execute("ALTER TABLE promo_state RENAME TO promo_state_old")
    await db.execute("""
        CREATE TABLE promo_state (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id  INTEGER NOT NULL,
            message_id  INTEGER NOT NULL,
            next_run_at REAL    NOT NULL,
            updated_at  TEXT    NOT NULL
        )
    """)
    await db.execute(
        "INSERT INTO promo_state (channel_id, message_id, next_run_at, updated_at) "
        "SELECT channel_id, message_id, next_run_at, updated_at FROM promo_state_old"
    )
    await db.execute("DROP TABLE promo_state_old")
    await db.commit()
    logger.info("promo_state migration complete.")


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
    """Legacy single-channel promo state (id=1)."""
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
    """Legacy single-channel promo state (id=1)."""
    await db.execute(
        "INSERT INTO promo_state (id, channel_id, message_id, next_run_at, updated_at) "
        "VALUES (1, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET channel_id = excluded.channel_id, "
        "message_id = excluded.message_id, next_run_at = excluded.next_run_at, "
        "updated_at = excluded.updated_at",
        (channel_id, message_id, next_run_at, _now()),
    )
    await db.commit()


# ── offer groups (legacy single-tenant) ─────────────────────────────────────


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
    """Seed the offer list from a JSON file — only when the table is empty."""
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
    source: str = "start"
    channel_id: int | None = None


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


async def get_latest_grant(db: aiosqlite.Connection, tg_user_id: int) -> TrialGrant | None:
    """The single grant row for a user (one trial per Telegram user)."""
    rows = await db.execute_fetchall(
        "SELECT * FROM trial_grants WHERE tg_user_id = ?", (tg_user_id,)
    )
    if not rows:
        return None
    return _row_to_grant(rows[0])


def _row_to_grant(r: aiosqlite.Row) -> TrialGrant:
    """Convert a database row to a TrialGrant dataclass."""
    keys = r.keys()
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
        source=r["source"] if "source" in keys else "start",
        channel_id=r["channel_id"] if "channel_id" in keys else None,
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
    source: str = "start",
    channel_id: int | None = None,
) -> None:
    """Insert or replace the grant row for this Telegram user."""
    await db.execute(
        "INSERT INTO trial_grants (tg_user_id, tg_username, panel_username, panel_user_id, "
        "group_ids, data_limit, expire_at, created_at, source_chat_id, revoked, source, "
        "channel_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?) "
        "ON CONFLICT(tg_user_id) DO UPDATE SET "
        "tg_username = excluded.tg_username, "
        "panel_username = excluded.panel_username, panel_user_id = excluded.panel_user_id, "
        "group_ids = excluded.group_ids, data_limit = excluded.data_limit, "
        "expire_at = excluded.expire_at, created_at = excluded.created_at, "
        "source_chat_id = excluded.source_chat_id, revoked = 0, source = excluded.source, "
        "channel_id = excluded.channel_id",
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
            source,
            channel_id,
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
    return [_row_to_grant(r) for r in rows]


async def count_grants_by_source(db: aiosqlite.Connection, source: str) -> int:
    """Count grants by source ('start' or 'join_request')."""
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) AS c FROM trial_grants WHERE source = ?", (source,)
    )
    return rows[0]["c"]


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


async def get_first_join_at(
    db: aiosqlite.Connection, chat_id: int, tg_user_id: int
) -> datetime | None:
    """When we first saw this user join the chat (None = never tracked)."""
    rows = await db.execute_fetchall(
        "SELECT MIN(at) AS first_at FROM member_events "
        "WHERE chat_id = ? AND tg_user_id = ? AND kind = 'join'",
        (chat_id, tg_user_id),
    )
    value = rows[0]["first_at"] if rows else None
    return datetime.fromisoformat(value) if value else None


# ═══════════════════════════════════════════════════════════════════════════════
# ── Multi-tenant CRUD ──────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


# ── panels ───────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Panel:
    id: int
    name: str
    base_url: str
    admin_username: str
    admin_password: str  # plaintext (decrypted on read)
    verify_ssl: bool = True
    timeout_seconds: float = 15.0
    protocols: str = "vless"
    auto_delete_days: int = 11
    active: bool = True


async def create_panel(
    db: aiosqlite.Connection,
    *,
    name: str,
    base_url: str,
    admin_username: str,
    admin_password: str,
    verify_ssl: bool = True,
    timeout_seconds: float = 15.0,
    protocols: str = "vless",
    auto_delete_days: int = 11,
) -> Panel:
    """Create a new panel and return it."""
    encrypted_pw = crypto.encrypt(admin_password)
    now = _now()
    cursor = await db.execute(
        "INSERT INTO panels (name, base_url, admin_username, admin_password, "
        "verify_ssl, timeout_seconds, protocols, auto_delete_days, active, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
        (
            name,
            base_url,
            admin_username,
            encrypted_pw,
            int(verify_ssl),
            timeout_seconds,
            protocols,
            auto_delete_days,
            now,
            now,
        ),
    )
    await db.commit()
    panel_id = cursor.lastrowid
    return Panel(
        id=panel_id,
        name=name,
        base_url=base_url,
        admin_username=admin_username,
        admin_password=admin_password,
        verify_ssl=verify_ssl,
        timeout_seconds=timeout_seconds,
        protocols=protocols,
        auto_delete_days=auto_delete_days,
        active=True,
    )


async def get_panel(db: aiosqlite.Connection, panel_id: int) -> Panel | None:
    """Fetch a panel by id (password is decrypted)."""
    rows = await db.execute_fetchall("SELECT * FROM panels WHERE id = ?", (panel_id,))
    if not rows:
        return None
    return _row_to_panel(rows[0])


async def list_panels(db: aiosqlite.Connection, *, active_only: bool = True) -> list[Panel]:
    """List all panels (passwords decrypted)."""
    if active_only:
        rows = await db.execute_fetchall("SELECT * FROM panels WHERE active = 1 ORDER BY id")
    else:
        rows = await db.execute_fetchall("SELECT * FROM panels ORDER BY id")
    return [_row_to_panel(r) for r in rows]


async def update_panel(db: aiosqlite.Connection, panel_id: int, **fields) -> bool:
    """Update specific fields on a panel. Returns True if a row was changed.

    Accepted fields: name, base_url, admin_username, admin_password,
    verify_ssl, timeout_seconds, protocols, auto_delete_days, active.
    Password is encrypted automatically.
    """
    allowed = {
        "name",
        "base_url",
        "admin_username",
        "admin_password",
        "verify_ssl",
        "timeout_seconds",
        "protocols",
        "auto_delete_days",
        "active",
    }
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return False
    if "admin_password" in updates:
        updates["admin_password"] = crypto.encrypt(updates["admin_password"])
    if "verify_ssl" in updates:
        updates["verify_ssl"] = int(updates["verify_ssl"])
    if "active" in updates:
        updates["active"] = int(updates["active"])
    updates["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [panel_id]
    cursor = await db.execute(f"UPDATE panels SET {set_clause} WHERE id = ?", values)
    await db.commit()
    return cursor.rowcount > 0


async def soft_delete_panel(db: aiosqlite.Connection, panel_id: int) -> bool:
    """Soft-delete a panel (set active=0)."""
    return await update_panel(db, panel_id, active=False)


def _row_to_panel(r: aiosqlite.Row) -> Panel:
    keys = r.keys()
    return Panel(
        id=r["id"],
        name=r["name"],
        base_url=r["base_url"],
        admin_username=r["admin_username"],
        admin_password=crypto.decrypt(r["admin_password"]),
        verify_ssl=bool(r["verify_ssl"]) if "verify_ssl" in keys else True,
        timeout_seconds=r["timeout_seconds"] if "timeout_seconds" in keys else 15.0,
        protocols=r["protocols"] if "protocols" in keys else "vless",
        auto_delete_days=r["auto_delete_days"] if "auto_delete_days" in keys else 11,
        active=bool(r["active"]) if "active" in keys else True,
    )


# ── channels ────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Channel:
    id: int
    tg_channel_id: int
    title: str
    trial_data_limit_gb: float = 5.0
    trial_days: int = 3
    on_hold_grace_days: int = 7
    allow_regrant_after_days: int = 30
    trial_max_member_age_days: float = 0.0
    join_approval_delay_seconds: int = 10
    promo_interval_hours: float = 6.0
    promo_pin: bool = True
    promo_silent: bool = True
    post_delete_previous: bool = False
    active: bool = True


async def create_channel(
    db: aiosqlite.Connection,
    *,
    tg_channel_id: int,
    title: str = "",
    trial_data_limit_gb: float = 5.0,
    trial_days: int = 3,
    on_hold_grace_days: int = 7,
    allow_regrant_after_days: int = 30,
    trial_max_member_age_days: float = 0.0,
    join_approval_delay_seconds: int = 10,
    promo_interval_hours: float = 6.0,
    promo_pin: bool = True,
    promo_silent: bool = True,
    post_delete_previous: bool = False,
) -> Channel:
    """Create a new channel and return it."""
    now = _now()
    cursor = await db.execute(
        "INSERT INTO channels (tg_channel_id, title, trial_data_limit_gb, trial_days, "
        "on_hold_grace_days, allow_regrant_after_days, trial_max_member_age_days, "
        "join_approval_delay_seconds, promo_interval_hours, promo_pin, promo_silent, "
        "post_delete_previous, active, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
        (
            tg_channel_id,
            title,
            trial_data_limit_gb,
            trial_days,
            on_hold_grace_days,
            allow_regrant_after_days,
            trial_max_member_age_days,
            join_approval_delay_seconds,
            promo_interval_hours,
            int(promo_pin),
            int(promo_silent),
            int(post_delete_previous),
            now,
            now,
        ),
    )
    await db.commit()
    ch_id = cursor.lastrowid
    return Channel(
        id=ch_id,
        tg_channel_id=tg_channel_id,
        title=title,
        trial_data_limit_gb=trial_data_limit_gb,
        trial_days=trial_days,
        on_hold_grace_days=on_hold_grace_days,
        allow_regrant_after_days=allow_regrant_after_days,
        trial_max_member_age_days=trial_max_member_age_days,
        join_approval_delay_seconds=join_approval_delay_seconds,
        promo_interval_hours=promo_interval_hours,
        promo_pin=promo_pin,
        promo_silent=promo_silent,
        post_delete_previous=post_delete_previous,
        active=True,
    )


async def get_channel(db: aiosqlite.Connection, channel_db_id: int) -> Channel | None:
    """Fetch a channel by its internal DB id."""
    rows = await db.execute_fetchall("SELECT * FROM channels WHERE id = ?", (channel_db_id,))
    if not rows:
        return None
    return _row_to_channel(rows[0])


async def get_channel_by_tg_id(db: aiosqlite.Connection, tg_channel_id: int) -> Channel | None:
    """Fetch a channel by its Telegram channel id."""
    rows = await db.execute_fetchall(
        "SELECT * FROM channels WHERE tg_channel_id = ?", (tg_channel_id,)
    )
    if not rows:
        return None
    return _row_to_channel(rows[0])


async def list_channels(db: aiosqlite.Connection, *, active_only: bool = True) -> list[Channel]:
    """List all channels."""
    if active_only:
        rows = await db.execute_fetchall("SELECT * FROM channels WHERE active = 1 ORDER BY id")
    else:
        rows = await db.execute_fetchall("SELECT * FROM channels ORDER BY id")
    return [_row_to_channel(r) for r in rows]


async def update_channel(db: aiosqlite.Connection, channel_id: int, **fields) -> bool:
    """Update specific fields on a channel. Returns True if a row was changed."""
    allowed = {
        "title",
        "trial_data_limit_gb",
        "trial_days",
        "on_hold_grace_days",
        "allow_regrant_after_days",
        "trial_max_member_age_days",
        "join_approval_delay_seconds",
        "promo_interval_hours",
        "promo_pin",
        "promo_silent",
        "post_delete_previous",
        "active",
    }
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return False
    if "promo_pin" in updates:
        updates["promo_pin"] = int(updates["promo_pin"])
    if "promo_silent" in updates:
        updates["promo_silent"] = int(updates["promo_silent"])
    if "post_delete_previous" in updates:
        updates["post_delete_previous"] = int(updates["post_delete_previous"])
    if "active" in updates:
        updates["active"] = int(updates["active"])
    updates["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [channel_id]
    cursor = await db.execute(f"UPDATE channels SET {set_clause} WHERE id = ?", values)
    await db.commit()
    return cursor.rowcount > 0


async def soft_delete_channel(db: aiosqlite.Connection, channel_id: int) -> bool:
    """Soft-delete a channel (set active=0)."""
    return await update_channel(db, channel_id, active=False)


def _row_to_channel(r: aiosqlite.Row) -> Channel:
    keys = r.keys()
    return Channel(
        id=r["id"],
        tg_channel_id=r["tg_channel_id"],
        title=r["title"] if "title" in keys else "",
        trial_data_limit_gb=r["trial_data_limit_gb"],
        trial_days=r["trial_days"],
        on_hold_grace_days=r["on_hold_grace_days"],
        allow_regrant_after_days=r["allow_regrant_after_days"],
        trial_max_member_age_days=r["trial_max_member_age_days"],
        join_approval_delay_seconds=r["join_approval_delay_seconds"],
        promo_interval_hours=r["promo_interval_hours"],
        promo_pin=bool(r["promo_pin"]),
        promo_silent=bool(r["promo_silent"]),
        post_delete_previous=bool(r["post_delete_previous"])
        if "post_delete_previous" in keys
        else False,
        active=bool(r["active"]),
    )


# ── users (role-based access) ───────────────────────────────────────────────


VALID_ROLES = {"superadmin", "admin", "user"}


@dataclass(slots=True)
class User:
    tg_user_id: int
    role: str  # 'superadmin' | 'admin' | 'user'
    username: str | None = None


async def upsert_user(
    db: aiosqlite.Connection,
    *,
    tg_user_id: int,
    role: str = "user",
    username: str | None = None,
) -> User:
    """Create or update a user's role. Returns the user."""
    if role not in VALID_ROLES:
        raise ValueError(f"Invalid role {role!r}; must be one of {VALID_ROLES}")
    now = _now()
    await db.execute(
        "INSERT INTO users (tg_user_id, username, role, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(tg_user_id) DO UPDATE SET username = excluded.username, "
        "role = excluded.role, updated_at = excluded.updated_at",
        (tg_user_id, username, role, now, now),
    )
    await db.commit()
    return User(tg_user_id=tg_user_id, role=role, username=username)


async def get_user(db: aiosqlite.Connection, tg_user_id: int) -> User | None:
    """Fetch a user by Telegram id."""
    rows = await db.execute_fetchall("SELECT * FROM users WHERE tg_user_id = ?", (tg_user_id,))
    if not rows:
        return None
    r = rows[0]
    return User(
        tg_user_id=r["tg_user_id"],
        role=r["role"],
        username=r["username"],
    )


async def list_users(db: aiosqlite.Connection, *, role: str | None = None) -> list[User]:
    """List all users, optionally filtered by role."""
    if role:
        rows = await db.execute_fetchall(
            "SELECT * FROM users WHERE role = ? ORDER BY tg_user_id", (role,)
        )
    else:
        rows = await db.execute_fetchall("SELECT * FROM users ORDER BY tg_user_id")
    return [User(tg_user_id=r["tg_user_id"], role=r["role"], username=r["username"]) for r in rows]


async def delete_user(db: aiosqlite.Connection, tg_user_id: int) -> bool:
    """Remove a user entirely (also removes their channel assignments)."""
    await db.execute("DELETE FROM channel_admins WHERE tg_user_id = ?", (tg_user_id,))
    cursor = await db.execute("DELETE FROM users WHERE tg_user_id = ?", (tg_user_id,))
    await db.commit()
    return cursor.rowcount > 0


# ── channel_admins ──────────────────────────────────────────────────────────


async def assign_channel_admin(db: aiosqlite.Connection, tg_user_id: int, channel_id: int) -> None:
    """Assign a user as admin of a channel. No-op if already assigned."""
    await db.execute(
        "INSERT OR IGNORE INTO channel_admins (tg_user_id, channel_id, created_at) "
        "VALUES (?, ?, ?)",
        (tg_user_id, channel_id, _now()),
    )
    await db.commit()


async def unassign_channel_admin(
    db: aiosqlite.Connection, tg_user_id: int, channel_id: int
) -> bool:
    """Remove a user's channel assignment. Returns True if a row was deleted."""
    cursor = await db.execute(
        "DELETE FROM channel_admins WHERE tg_user_id = ? AND channel_id = ?",
        (tg_user_id, channel_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def list_user_channels(db: aiosqlite.Connection, tg_user_id: int) -> list[Channel]:
    """List channels a user is assigned to as admin."""
    rows = await db.execute_fetchall(
        "SELECT c.* FROM channels c "
        "JOIN channel_admins ca ON c.id = ca.channel_id "
        "WHERE ca.tg_user_id = ? AND c.active = 1 "
        "ORDER BY c.id",
        (tg_user_id,),
    )
    return [_row_to_channel(r) for r in rows]


async def list_channel_admins(db: aiosqlite.Connection, channel_id: int) -> list[User]:
    """List users who are admins of a channel."""
    rows = await db.execute_fetchall(
        "SELECT u.* FROM users u "
        "JOIN channel_admins ca ON u.tg_user_id = ca.tg_user_id "
        "WHERE ca.channel_id = ? "
        "ORDER BY u.tg_user_id",
        (channel_id,),
    )
    return [User(tg_user_id=r["tg_user_id"], role=r["role"], username=r["username"]) for r in rows]


async def is_channel_admin(db: aiosqlite.Connection, tg_user_id: int, channel_db_id: int) -> bool:
    """Check if a user is assigned as admin of a channel."""
    rows = await db.execute_fetchall(
        "SELECT 1 FROM channel_admins WHERE tg_user_id = ? AND channel_id = ?",
        (tg_user_id, channel_db_id),
    )
    return len(rows) > 0


# ── channel_offer_groups ────────────────────────────────────────────────────


@dataclass(slots=True)
class ChannelOfferGroup:
    channel_id: int
    panel_id: int
    group_id: int  # panel group id
    label: str
    sort_order: int


async def list_channel_offer_groups(
    db: aiosqlite.Connection, channel_id: int
) -> list[ChannelOfferGroup]:
    """List offer groups for a channel, ordered by sort_order."""
    rows = await db.execute_fetchall(
        "SELECT channel_id, panel_id, group_id, label, sort_order "
        "FROM channel_offer_groups WHERE channel_id = ? "
        "ORDER BY sort_order, group_id",
        (channel_id,),
    )
    return [_row_to_channel_offer_group(r) for r in rows]


async def upsert_channel_offer_group(
    db: aiosqlite.Connection,
    *,
    channel_id: int,
    panel_id: int,
    group_id: int,
    label: str,
) -> ChannelOfferGroup:
    """Add or update an offer group for a channel. Appends to the end if new."""
    existing = await db.execute_fetchall(
        "SELECT sort_order FROM channel_offer_groups "
        "WHERE channel_id = ? AND panel_id = ? AND group_id = ?",
        (channel_id, panel_id, group_id),
    )
    if existing:
        sort_order = existing[0]["sort_order"]
    else:
        max_row = await db.execute_fetchall(
            "SELECT COALESCE(MAX(sort_order), -1) AS m FROM channel_offer_groups "
            "WHERE channel_id = ?",
            (channel_id,),
        )
        sort_order = max_row[0]["m"] + 1

    await db.execute(
        "INSERT INTO channel_offer_groups "
        "(channel_id, panel_id, group_id, label, sort_order, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(channel_id, panel_id, group_id) DO UPDATE SET "
        "label = excluded.label, sort_order = excluded.sort_order, "
        "updated_at = excluded.updated_at",
        (channel_id, panel_id, group_id, label, sort_order, _now()),
    )
    await db.commit()
    return ChannelOfferGroup(
        channel_id=channel_id,
        panel_id=panel_id,
        group_id=group_id,
        label=label,
        sort_order=sort_order,
    )


async def delete_channel_offer_group(
    db: aiosqlite.Connection,
    *,
    channel_id: int,
    panel_id: int,
    group_id: int,
) -> bool:
    """Remove an offer group from a channel."""
    cursor = await db.execute(
        "DELETE FROM channel_offer_groups WHERE channel_id = ? AND panel_id = ? AND group_id = ?",
        (channel_id, panel_id, group_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def clear_channel_offer_groups(db: aiosqlite.Connection, channel_id: int) -> int:
    """Remove all offer groups for a channel."""
    cursor = await db.execute(
        "DELETE FROM channel_offer_groups WHERE channel_id = ?", (channel_id,)
    )
    await db.commit()
    return cursor.rowcount


async def reorder_channel_offer_groups(
    db: aiosqlite.Connection,
    channel_id: int,
    ordered_keys: list[tuple[int, int]],  # list of (panel_id, group_id)
) -> None:
    """Set the display order to exactly the given (panel_id, group_id) sequence."""
    for order, (panel_id, group_id) in enumerate(ordered_keys):
        await db.execute(
            "UPDATE channel_offer_groups SET sort_order = ?, updated_at = ? "
            "WHERE channel_id = ? AND panel_id = ? AND group_id = ?",
            (order, _now(), channel_id, panel_id, group_id),
        )
    await db.commit()


def _row_to_channel_offer_group(r: aiosqlite.Row) -> ChannelOfferGroup:
    return ChannelOfferGroup(
        channel_id=r["channel_id"],
        panel_id=r["panel_id"],
        group_id=r["group_id"],
        label=r["label"],
        sort_order=r["sort_order"],
    )


# ╀─ channel posts (manual & scheduled posting) ──────────────────────────────


POST_CONTENT_FIELDS = (
    "text",
    "entities_json",
    "media_type",
    "media_file_id",
    "buttons_json",
    "delete_previous",
    "pin",
    "silent",
    "link_preview",
    "ephemeral_hours",
)


@dataclass(slots=True)
class ChannelPost:
    id: int
    channel_id: int
    group_id: str | None
    created_by: int
    created_at: str
    updated_at: str
    text: str
    entities_json: str | None
    media_type: str | None
    media_file_id: str | None
    media_json: str | None
    buttons_json: str
    delete_previous: bool
    pin: bool
    silent: bool
    link_preview: bool
    ephemeral_hours: float | None
    expires_at: str | None
    status: str
    scheduled_at: str | None
    recurrence: str
    recur_at: str | None
    last_sent_at: str | None
    sent_at: str | None
    tg_message_id: int | None
    tg_message_ids_json: str | None
    error: str | None


@dataclass(slots=True)
class PostTemplate:
    id: int
    name: str
    created_by: int
    created_at: str
    text: str
    entities_json: str | None
    media_type: str | None
    media_file_id: str | None
    media_json: str | None
    buttons_json: str
    delete_previous: bool
    pin: bool
    silent: bool
    link_preview: bool
    ephemeral_hours: float | None


def _row_to_channel_post(r: aiosqlite.Row) -> ChannelPost:
    return ChannelPost(
        id=r["id"],
        channel_id=r["channel_id"],
        group_id=r["group_id"],
        created_by=r["created_by"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
        text=r["text"],
        entities_json=r["entities_json"],
        media_type=r["media_type"],
        media_file_id=r["media_file_id"],
        media_json=r["media_json"] if "media_json" in r.keys() else None,
        buttons_json=r["buttons_json"],
        delete_previous=bool(r["delete_previous"]),
        pin=bool(r["pin"]),
        silent=bool(r["silent"]),
        link_preview=bool(r["link_preview"]),
        ephemeral_hours=r["ephemeral_hours"],
        expires_at=r["expires_at"],
        status=r["status"],
        scheduled_at=r["scheduled_at"],
        recurrence=r["recurrence"],
        recur_at=r["recur_at"],
        last_sent_at=r["last_sent_at"],
        sent_at=r["sent_at"],
        tg_message_id=r["tg_message_id"],
        tg_message_ids_json=(
            r["tg_message_ids_json"] if "tg_message_ids_json" in r.keys() else None
        ),
        error=r["error"],
    )


def _row_to_post_template(r: aiosqlite.Row) -> PostTemplate:
    return PostTemplate(
        id=r["id"],
        name=r["name"],
        created_by=r["created_by"],
        created_at=r["created_at"],
        text=r["text"],
        entities_json=r["entities_json"],
        media_type=r["media_type"],
        media_file_id=r["media_file_id"],
        media_json=r["media_json"] if "media_json" in r.keys() else None,
        buttons_json=r["buttons_json"],
        delete_previous=bool(r["delete_previous"]),
        pin=bool(r["pin"]),
        silent=bool(r["silent"]),
        link_preview=bool(r["link_preview"]),
        ephemeral_hours=r["ephemeral_hours"],
    )


async def create_channel_post(
    db: aiosqlite.Connection,
    *,
    channel_id: int,
    created_by: int,
    status: str = "draft",
    group_id: str | None = None,
    text: str = "",
    entities_json: str | None = None,
    media_type: str | None = None,
    media_file_id: str | None = None,
    media_json: str | None = None,
    buttons_json: str = "[]",
    delete_previous: bool = False,
    pin: bool = False,
    silent: bool = False,
    link_preview: bool = True,
    ephemeral_hours: float | None = None,
    scheduled_at: str | None = None,
    recurrence: str = "none",
    recur_at: str | None = None,
) -> ChannelPost:
    now = _now()
    cursor = await db.execute(
        "INSERT INTO channel_posts (channel_id, group_id, created_by, created_at, updated_at, "
        "text, entities_json, media_type, media_file_id, media_json, buttons_json, "
        "delete_previous, pin, silent, link_preview, ephemeral_hours, status, scheduled_at, "
        "recurrence, recur_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            channel_id,
            group_id,
            created_by,
            now,
            now,
            text,
            entities_json,
            media_type,
            media_file_id,
            media_json,
            buttons_json,
            int(delete_previous),
            int(pin),
            int(silent),
            int(link_preview),
            ephemeral_hours,
            status,
            scheduled_at,
            recurrence,
            recur_at,
        ),
    )
    await db.commit()
    return await get_channel_post(db, cursor.lastrowid)


async def get_channel_post(db: aiosqlite.Connection, post_id: int) -> ChannelPost | None:
    rows = await db.execute_fetchall("SELECT * FROM channel_posts WHERE id = ?", (post_id,))
    return _row_to_channel_post(rows[0]) if rows else None


async def update_channel_post(db: aiosqlite.Connection, post_id: int, **fields) -> bool:
    """Update specific fields on a channel post. Returns True if a row was changed."""
    allowed = set(ChannelPost.__dataclass_fields__) - {
        "id",
        "created_at",
        "created_by",
        "channel_id",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    for key in ("delete_previous", "pin", "silent", "link_preview"):
        if key in updates:
            updates[key] = int(updates[key])
    updates["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    cursor = await db.execute(
        f"UPDATE channel_posts SET {set_clause} WHERE id = ?",  # noqa: S608
        (*updates.values(), post_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def list_channel_posts(
    db: aiosqlite.Connection,
    channel_id: int,
    *,
    statuses: list[str] | None = None,
    limit: int = 10,
) -> list[ChannelPost]:
    """Most-recent-first posts of a channel, optionally filtered by status."""
    if statuses:
        placeholders = ", ".join("?" for _ in statuses)
        rows = await db.execute_fetchall(
            f"SELECT * FROM channel_posts WHERE channel_id = ? AND status IN ({placeholders}) "
            f"ORDER BY id DESC LIMIT ?",  # noqa: S608
            (channel_id, *statuses, limit),
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT * FROM channel_posts WHERE channel_id = ? ORDER BY id DESC LIMIT ?",
            (channel_id, limit),
        )
    return [_row_to_channel_post(r) for r in rows]


async def last_published_post(db: aiosqlite.Connection, channel_id: int) -> ChannelPost | None:
    """The newest post of this channel that has a live message id."""
    rows = await db.execute_fetchall(
        "SELECT * FROM channel_posts WHERE channel_id = ? AND tg_message_id IS NOT NULL "
        "ORDER BY sent_at DESC, id DESC LIMIT 1",
        (channel_id,),
    )
    return _row_to_channel_post(rows[0]) if rows else None


async def list_due_scheduled_posts(db: aiosqlite.Connection, now_iso: str) -> list[ChannelPost]:
    """One-shot scheduled posts whose time has arrived."""
    rows = await db.execute_fetchall(
        "SELECT * FROM channel_posts WHERE status = 'scheduled' AND scheduled_at IS NOT NULL "
        "AND scheduled_at <= ? ORDER BY scheduled_at ASC",
        (now_iso,),
    )
    return [_row_to_channel_post(r) for r in rows]


async def list_recurring_posts(db: aiosqlite.Connection) -> list[ChannelPost]:
    rows = await db.execute_fetchall(
        "SELECT * FROM channel_posts WHERE status = 'recurring' ORDER BY id ASC"
    )
    return [_row_to_channel_post(r) for r in rows]


async def list_expired_posts(db: aiosqlite.Connection, now_iso: str) -> list[ChannelPost]:
    """Ephemeral posts whose message should be deleted by now."""
    rows = await db.execute_fetchall(
        "SELECT * FROM channel_posts WHERE expires_at IS NOT NULL AND expires_at <= ? "
        "AND tg_message_id IS NOT NULL",
        (now_iso,),
    )
    return [_row_to_channel_post(r) for r in rows]


async def delete_channel_post(db: aiosqlite.Connection, post_id: int) -> bool:
    """Hard-delete a post row (the Telegram message, if any, is removed by the caller)."""
    cursor = await db.execute("DELETE FROM channel_posts WHERE id = ?", (post_id,))
    await db.commit()
    return cursor.rowcount > 0


# ── post templates ──────────────────────────────────────────────────────────


async def create_post_template(
    db: aiosqlite.Connection,
    *,
    created_by: int,
    name: str = "",
    text: str = "",
    entities_json: str | None = None,
    media_type: str | None = None,
    media_file_id: str | None = None,
    media_json: str | None = None,
    buttons_json: str = "[]",
    delete_previous: bool = False,
    pin: bool = False,
    silent: bool = False,
    link_preview: bool = True,
    ephemeral_hours: float | None = None,
) -> PostTemplate:
    cursor = await db.execute(
        "INSERT INTO post_templates (name, created_by, created_at, text, entities_json, "
        "media_type, media_file_id, media_json, buttons_json, delete_previous, pin, silent, "
        "link_preview, ephemeral_hours) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            name,
            created_by,
            _now(),
            text,
            entities_json,
            media_type,
            media_file_id,
            media_json,
            buttons_json,
            int(delete_previous),
            int(pin),
            int(silent),
            int(link_preview),
            ephemeral_hours,
        ),
    )
    await db.commit()
    rows = await db.execute_fetchall(
        "SELECT * FROM post_templates WHERE id = ?", (cursor.lastrowid,)
    )
    return _row_to_post_template(rows[0])


async def get_post_template(db: aiosqlite.Connection, template_id: int) -> PostTemplate | None:
    rows = await db.execute_fetchall("SELECT * FROM post_templates WHERE id = ?", (template_id,))
    return _row_to_post_template(rows[0]) if rows else None


async def list_post_templates(db: aiosqlite.Connection, limit: int = 20) -> list[PostTemplate]:
    rows = await db.execute_fetchall(
        "SELECT * FROM post_templates ORDER BY id DESC LIMIT ?", (limit,)
    )
    return [_row_to_post_template(r) for r in rows]


async def delete_post_template(db: aiosqlite.Connection, template_id: int) -> bool:
    cursor = await db.execute("DELETE FROM post_templates WHERE id = ?", (template_id,))
    await db.commit()
    return cursor.rowcount > 0
